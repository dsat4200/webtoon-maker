"""Authenticated loopback notification server and validated inbox processor."""
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import threading
from typing import Any, Callable, Mapping
import uuid

from .protocol import (
    BUNDLE_MANIFEST,
    ConflictDescriptor,
    ConflictResolution,
    MAX_FILE_COUNT,
    MAX_JSON_BYTES,
    SyncBundle,
    SyncProtocolError,
    SyncReceipt,
    SyncStatus,
    ValidatedBundle,
    canonical_json_bytes,
    resolve_conflicts as resolve_frame_conflicts,
    strict_json_loads,
    three_way_merge_frame_state,
    validate_bundle_directory,
)


MAX_RPC_BYTES = 64 * 1024
RPC_METHOD = "sync.notify"


@dataclass(frozen=True)
class SyncNotification:
    transaction_id: str
    bundle_sha256: str

    def __post_init__(self) -> None:
        try:
            normalized = str(uuid.UUID(self.transaction_id))
        except (ValueError, AttributeError) as exc:
            raise SyncProtocolError("Notification transaction_id must be a UUID", code="invalid_identity") from exc
        object.__setattr__(self, "transaction_id", normalized)
        if (
            not isinstance(self.bundle_sha256, str)
            or len(self.bundle_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.bundle_sha256)
        ):
            raise SyncProtocolError("Notification bundle_sha256 must be lowercase SHA-256", code="hash_mismatch")

    @classmethod
    def from_params(cls, value: Any) -> "SyncNotification":
        if not isinstance(value, Mapping) or set(value) != {"transaction_id", "bundle_sha256"}:
            raise SyncProtocolError(
                "sync.notify params must contain transaction_id and bundle_sha256",
                code="malformed_rpc",
            )
        return cls(
            transaction_id=value["transaction_id"],
            bundle_sha256=value["bundle_sha256"],
        )


class ConcurrentRevisionError(RuntimeError):
    """Raised by a publisher when its final compare-and-swap loses a race."""

    def __init__(self, current_revision: int) -> None:
        super().__init__(f"Chapter revision changed concurrently to {current_revision}")
        self.current_revision = current_revision


CurrentRevision = Callable[[], int]
CurrentState = Callable[[str], Mapping[str, Any]]
BaseState = Callable[[str, int], Mapping[str, Any]]
CurrentSourceRevision = Callable[[], int]
CurrentSourceDigest = Callable[[], str | None]
PublishBundle = Callable[[ValidatedBundle, Mapping[str, Any], int], int]
PublishResolvedBundle = Callable[
    [
        ValidatedBundle,
        Mapping[str, Any],
        int,
        tuple[ConflictDescriptor, ...],
        Mapping[str, ConflictResolution | str],
    ],
    int,
]


def _revision_conflict(
    bundle: SyncBundle, current_revision: int, *, message: str | None = None,
) -> SyncReceipt:
    descriptor = ConflictDescriptor(
        category="revision",
        path="/$revision",
        base_value=bundle.base_revision,
        webtoon_value=current_revision,
        blender_value=bundle.source_revision,
    )
    warnings = (message,) if message else ()
    return SyncReceipt(
        transaction_id=bundle.transaction_id,
        status=SyncStatus.CONFLICTS,
        conflicts=(descriptor,),
        warnings=warnings,
    )


class SyncInboxProcessor:
    """Validate complete transactions and publish them through a final CAS.

    ``publish`` is the only callback permitted to mutate visible application
    state.  It is called only after identity, path, size, hashes, JSON, and GLB
    validation have all succeeded.  It receives the current expected revision
    and must raise :class:`ConcurrentRevisionError` if that revision changed.

    A repository may pass ``expected_blender_file_uuid`` as a callable so the
    first accepted transaction can establish an association and later calls can
    enforce it without rebuilding the server.
    """

    def __init__(
        self,
        inbox_root: str | Path,
        *,
        series_id: str,
        chapter_id: str,
        expected_blender_file_uuid: str | None | Callable[[], str | None],
        current_revision: CurrentRevision,
        publish: PublishBundle,
        publish_resolved: PublishResolvedBundle | None = None,
        current_source_revision: CurrentSourceRevision | None = None,
        current_source_digest: CurrentSourceDigest | None = None,
        current_state: CurrentState | None = None,
        base_state: BaseState | None = None,
        receipt_root: str | Path | None = None,
    ) -> None:
        self.inbox_root = Path(inbox_root)
        self.series_id = series_id
        self.chapter_id = chapter_id
        self._expected_blender_file_uuid = expected_blender_file_uuid
        self._current_revision = current_revision
        self._current_source_revision = current_source_revision
        self._current_source_digest = current_source_digest
        self._current_state = current_state
        self._base_state = base_state
        self._publish = publish
        self._publish_resolved = publish_resolved
        self.receipt_root = (
            Path(receipt_root) if receipt_root is not None
            else self.inbox_root / "receipts"
        )
        self._receipts: dict[str, tuple[str, SyncReceipt]] = {}
        self._pending_conflicts: dict[
            str, tuple[str, tuple[ConflictDescriptor, ...]]
        ] = {}
        self._lock = threading.RLock()

    def _file_uuid(self) -> str | None:
        value = self._expected_blender_file_uuid
        return value() if callable(value) else value

    def _ready_directory(self, transaction_id: str) -> Path:
        try:
            normalized = str(uuid.UUID(transaction_id))
        except (ValueError, AttributeError) as exc:
            raise SyncProtocolError("transaction_id must be a UUID", code="invalid_identity") from exc
        return self.inbox_root / f"{normalized}.ready"

    def _receipt_path(self, transaction_id: str) -> Path:
        normalized = str(uuid.UUID(transaction_id))
        return self.receipt_root / f"{normalized}.json"

    def _load_persisted_receipt(
        self, validated: ValidatedBundle,
    ) -> SyncReceipt | None:
        path = self._receipt_path(validated.bundle.transaction_id)
        if not path.is_file() or path.is_symlink():
            return None
        try:
            if path.stat().st_size > MAX_RPC_BYTES:
                raise SyncProtocolError("Stored receipt exceeds size limit", code="malformed_receipt")
            value = strict_json_loads(path.read_bytes(), label="stored sync receipt")
            if not isinstance(value, Mapping) or set(value) != {
                "schema_version", "bundle_sha256", "receipt",
            }:
                raise SyncProtocolError("Stored receipt has invalid fields", code="malformed_receipt")
            if value["schema_version"] != 1:
                raise SyncProtocolError("Stored receipt has unsupported schema", code="malformed_receipt")
            if value["bundle_sha256"] != validated.bundle.bundle_sha256:
                return None
            receipt = SyncReceipt.from_dict(value["receipt"])
            if (
                receipt.transaction_id != validated.bundle.transaction_id
                or receipt.status is not SyncStatus.ACCEPTED
            ):
                raise SyncProtocolError("Stored receipt does not identify an accepted transaction", code="malformed_receipt")
            return receipt
        except OSError as exc:
            raise SyncProtocolError("Stored receipt cannot be read", code="malformed_receipt") from exc

    def _store_persisted_receipt(
        self, bundle: SyncBundle, receipt: SyncReceipt,
    ) -> None:
        self.receipt_root.mkdir(parents=True, exist_ok=True)
        if self.receipt_root.is_symlink():
            raise OSError("Receipt directory cannot be a symbolic link")
        path = self._receipt_path(bundle.transaction_id)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = canonical_json_bytes({
            "schema_version": 1,
            "bundle_sha256": bundle.bundle_sha256,
            "receipt": receipt.to_dict(),
        })
        try:
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _accepted_receipt(
        self,
        bundle: SyncBundle,
        accepted_revision: int,
        *,
        extra_warnings: tuple[str, ...] = (),
    ) -> SyncReceipt:
        receipt = SyncReceipt(
            transaction_id=bundle.transaction_id,
            status=SyncStatus.ACCEPTED,
            accepted_revision=accepted_revision,
            warnings=(*bundle.warnings, *extra_warnings),
        )
        self._pending_conflicts.pop(bundle.transaction_id, None)
        self._receipts[bundle.transaction_id] = (bundle.bundle_sha256, receipt)
        try:
            self._store_persisted_receipt(bundle, receipt)
        except (OSError, SyncProtocolError):
            # Publication has already committed. Never report rejection or
            # invoke the publisher a second time in this process.
            receipt = SyncReceipt(
                transaction_id=bundle.transaction_id,
                status=SyncStatus.ACCEPTED,
                accepted_revision=accepted_revision,
                warnings=(
                    *bundle.warnings,
                    *extra_warnings,
                    "Accepted, but the idempotence receipt could not be persisted.",
                ),
            )
            self._receipts[bundle.transaction_id] = (
                bundle.bundle_sha256, receipt,
            )
        return receipt

    def _source_replay_receipt(
        self, bundle: SyncBundle, current_revision: int,
    ) -> SyncReceipt | None:
        """Reject stale/reused Blender revisions or acknowledge exact replay."""

        if self._current_source_revision is None:
            return None
        source_revision = self._current_source_revision()
        if (
            isinstance(source_revision, bool)
            or not isinstance(source_revision, int)
            or source_revision < 0
        ):
            raise SyncProtocolError(
                "Repository returned an invalid Blender source revision",
                code="internal_error",
            )
        if bundle.source_revision < source_revision:
            raise SyncProtocolError(
                "Bundle Blender source revision is older than the accepted source",
                code="stale_source_revision",
            )
        if bundle.source_revision > source_revision:
            return None
        if source_revision == 0:
            # Incoming bundle revisions are positive, so this branch is only a
            # defensive guard for an inconsistent callback.
            raise SyncProtocolError(
                "Bundle reuses an invalid zero source revision",
                code="stale_source_revision",
            )
        accepted_digest = (
            self._current_source_digest()
            if self._current_source_digest is not None else None
        )
        if (
            isinstance(accepted_digest, str)
            and len(accepted_digest) == 64
            and all(character in "0123456789abcdef" for character in accepted_digest)
            and secrets.compare_digest(accepted_digest, bundle.source_digest())
        ):
            return self._accepted_receipt(
                bundle,
                current_revision,
                extra_warnings=(
                    "This Blender source revision was already accepted; no state was republished.",
                ),
            )
        raise SyncProtocolError(
            "Bundle reuses an accepted Blender source revision with different content",
            code="source_revision_reuse",
        )

    @staticmethod
    def _captured_state(bundle: SyncBundle) -> Mapping[str, Any]:
        captured = bundle.frame_data.get("captured_state", bundle.frame_data)
        if not isinstance(captured, Mapping):
            raise SyncProtocolError("frame_data.captured_state must be an object")
        return captured

    def process(self, notification: SyncNotification) -> SyncReceipt:
        with self._lock:
            ready = self._ready_directory(notification.transaction_id)
            if not ready.is_dir():
                return SyncReceipt(
                    transaction_id=notification.transaction_id,
                    status=SyncStatus.QUEUED,
                    warnings=("The ready bundle is not available yet; it remains queued.",),
                )
            try:
                validated = validate_bundle_directory(
                    ready,
                    expected_series_id=self.series_id,
                    expected_chapter_id=self.chapter_id,
                    expected_blender_file_uuid=self._file_uuid(),
                )
                bundle = validated.bundle
                if notification.bundle_sha256 != bundle.bundle_sha256:
                    raise SyncProtocolError(
                        "Notification digest does not match the ready bundle",
                        code="hash_mismatch",
                    )
                persisted = self._load_persisted_receipt(validated)
                if persisted is not None:
                    self._receipts[bundle.transaction_id] = (bundle.bundle_sha256, persisted)
                    return persisted
                cached = self._receipts.get(bundle.transaction_id)
                if cached is not None and cached[0] == bundle.bundle_sha256:
                    return cached[1]

                current_revision = self._current_revision()
                if isinstance(current_revision, bool) or not isinstance(current_revision, int) or current_revision < 0:
                    raise SyncProtocolError("Repository returned an invalid revision", code="internal_error")
                if bundle.base_revision > current_revision:
                    raise SyncProtocolError(
                        "Bundle base revision is newer than the chapter",
                        code="future_revision",
                    )
                replay = self._source_replay_receipt(bundle, current_revision)
                if replay is not None:
                    return replay

                incoming_state = self._captured_state(bundle)
                merged_state: Mapping[str, Any] = incoming_state
                if bundle.base_revision < current_revision:
                    if self._current_state is None:
                        receipt = _revision_conflict(
                            bundle, current_revision,
                            message="Webtoon changed after this Blender frame was captured.",
                        )
                        return receipt
                    current_state = self._current_state(bundle.comic_frame_id)
                    if self._base_state is not None:
                        old_state = self._base_state(bundle.comic_frame_id, bundle.base_revision)
                    else:
                        old_state = bundle.frame_data.get("base_state")
                        if not isinstance(old_state, Mapping):
                            receipt = _revision_conflict(
                                bundle, current_revision,
                                message="The common base state is unavailable for a safe merge.",
                            )
                            return receipt
                    merged_state, conflicts = three_way_merge_frame_state(
                        old_state, current_state, incoming_state,
                    )
                    if conflicts:
                        receipt = SyncReceipt(
                            transaction_id=bundle.transaction_id,
                            status=SyncStatus.CONFLICTS,
                            conflicts=conflicts,
                            warnings=tuple(bundle.warnings),
                        )
                        self._pending_conflicts[bundle.transaction_id] = (
                            bundle.bundle_sha256, conflicts,
                        )
                        return receipt

                try:
                    accepted_revision = self._publish(validated, merged_state, current_revision)
                except ConcurrentRevisionError as exc:
                    return _revision_conflict(
                        bundle, exc.current_revision,
                        message="The chapter changed while the sync transaction was publishing.",
                    )
                if (
                    isinstance(accepted_revision, bool)
                    or not isinstance(accepted_revision, int)
                    or accepted_revision <= current_revision
                ):
                    raise SyncProtocolError(
                        "Publisher did not return a newer monotonic revision",
                        code="internal_error",
                    )
                return self._accepted_receipt(bundle, accepted_revision)
            except SyncProtocolError as exc:
                return SyncReceipt.rejected(notification.transaction_id, exc)
            except Exception as exc:  # Keep repository failures behind a typed boundary.
                safe_error = SyncProtocolError(
                    f"Sync publication failed: {type(exc).__name__}",
                    code="internal_error",
                )
                return SyncReceipt.rejected(notification.transaction_id, safe_error)

    def resolve_conflicts(
        self,
        receipt: SyncReceipt,
        choices: Mapping[str, ConflictResolution | str],
    ) -> SyncReceipt:
        """Revalidate and publish a conflicted transaction with UI choices.

        Exact JSON-pointer choices take precedence over category choices;
        omitted choices retain the Webtoon override. The common base and live
        Webtoon state are recomputed so a dialog can never accept against stale
        state. If the conflict set changed, the new grouped conflict receipt is
        returned without publishing.
        """

        transaction_id = receipt.transaction_id
        with self._lock:
            try:
                if receipt.status is not SyncStatus.CONFLICTS:
                    raise SyncProtocolError(
                        "Only a conflict receipt can be resolved",
                        code="invalid_resolution",
                    )
                if not isinstance(choices, Mapping):
                    raise SyncProtocolError(
                        "Conflict choices must be an object",
                        code="invalid_resolution",
                    )
                ready = self._ready_directory(transaction_id)
                if not ready.is_dir():
                    return SyncReceipt(
                        transaction_id=transaction_id,
                        status=SyncStatus.QUEUED,
                        warnings=(
                            "The ready bundle is no longer available; it remains queued.",
                        ),
                    )
                validated = validate_bundle_directory(
                    ready,
                    expected_series_id=self.series_id,
                    expected_chapter_id=self.chapter_id,
                    expected_blender_file_uuid=self._file_uuid(),
                )
                bundle = validated.bundle
                if bundle.transaction_id != transaction_id:
                    raise SyncProtocolError(
                        "Conflict receipt identifies another transaction",
                        code="wrong_identity",
                    )
                persisted = self._load_persisted_receipt(validated)
                if persisted is not None:
                    self._receipts[transaction_id] = (
                        bundle.bundle_sha256, persisted,
                    )
                    return persisted
                cached = self._receipts.get(transaction_id)
                if cached is not None and cached[0] == bundle.bundle_sha256:
                    return cached[1]
                pending = self._pending_conflicts.get(transaction_id)
                if pending is None:
                    raise SyncProtocolError(
                        "The conflict dialog is no longer current; rescan the transaction",
                        code="stale_resolution",
                    )
                if pending != (bundle.bundle_sha256, receipt.conflicts):
                    raise SyncProtocolError(
                        "The ready bundle or conflict receipt changed after review",
                        code="stale_resolution",
                    )

                current_revision = self._current_revision()
                if (
                    isinstance(current_revision, bool)
                    or not isinstance(current_revision, int)
                    or current_revision < 0
                ):
                    raise SyncProtocolError(
                        "Repository returned an invalid revision",
                        code="internal_error",
                    )
                if bundle.base_revision > current_revision:
                    raise SyncProtocolError(
                        "Bundle base revision is newer than the chapter",
                        code="future_revision",
                    )
                replay = self._source_replay_receipt(bundle, current_revision)
                if replay is not None:
                    return replay

                incoming_state = self._captured_state(bundle)
                merged_state: Mapping[str, Any] = incoming_state
                conflicts: tuple[ConflictDescriptor, ...] = ()
                if bundle.base_revision < current_revision:
                    if self._current_state is None:
                        return _revision_conflict(
                            bundle,
                            current_revision,
                            message=(
                                "Webtoon changed again while the conflict dialog was open."
                            ),
                        )
                    current_state = self._current_state(bundle.comic_frame_id)
                    if self._base_state is not None:
                        old_state = self._base_state(
                            bundle.comic_frame_id, bundle.base_revision,
                        )
                    else:
                        old_state = bundle.frame_data.get("base_state")
                        if not isinstance(old_state, Mapping):
                            return _revision_conflict(
                                bundle,
                                current_revision,
                                message=(
                                    "The common base state is unavailable for a safe merge."
                                ),
                            )
                    merged_state, conflicts = three_way_merge_frame_state(
                        old_state, current_state, incoming_state,
                    )

                # The dialog may have remained open while an app edit changed
                # one of the values. Never apply its now-stale choices.
                if conflicts and conflicts != receipt.conflicts:
                    updated = SyncReceipt(
                        transaction_id=transaction_id,
                        status=SyncStatus.CONFLICTS,
                        conflicts=conflicts,
                        warnings=tuple(bundle.warnings),
                    )
                    self._pending_conflicts[transaction_id] = (
                        bundle.bundle_sha256, conflicts,
                    )
                    return updated
                resolved_state = resolve_frame_conflicts(
                    merged_state, conflicts, choices,
                )
                try:
                    if self._publish_resolved is None:
                        accepted_revision = self._publish(
                            validated, resolved_state, current_revision,
                        )
                    else:
                        accepted_revision = self._publish_resolved(
                            validated,
                            resolved_state,
                            current_revision,
                            conflicts,
                            choices,
                        )
                except ConcurrentRevisionError as exc:
                    return _revision_conflict(
                        bundle,
                        exc.current_revision,
                        message=(
                            "The chapter changed while conflict choices were publishing."
                        ),
                    )
                if (
                    isinstance(accepted_revision, bool)
                    or not isinstance(accepted_revision, int)
                    or accepted_revision <= current_revision
                ):
                    raise SyncProtocolError(
                        "Publisher did not return a newer monotonic revision",
                        code="internal_error",
                    )
                return self._accepted_receipt(bundle, accepted_revision)
            except SyncProtocolError as exc:
                return SyncReceipt.rejected(transaction_id, exc)
            except Exception as exc:
                safe_error = SyncProtocolError(
                    f"Conflict publication failed: {type(exc).__name__}",
                    code="internal_error",
                )
                return SyncReceipt.rejected(transaction_id, safe_error)

    def process_ready_transactions(self) -> list[SyncReceipt]:
        """Process complete offline transactions; incomplete staging is ignored."""

        receipts: list[SyncReceipt] = []
        if not self.inbox_root.is_dir():
            return receipts
        candidates: list[tuple[int, float, str, SyncNotification]] = []
        try:
            entries = os.scandir(self.inbox_root)
        except OSError:
            return receipts
        with entries:
            for entry in entries:
                if not entry.name.endswith(".ready"):
                    continue
                if len(candidates) >= MAX_FILE_COUNT:
                    break
                path = Path(entry.path)
                manifest = path / BUNDLE_MANIFEST
                try:
                    if manifest.stat().st_size > MAX_JSON_BYTES:
                        raise SyncProtocolError("Bundle manifest is too large", code="size_limit")
                    data = strict_json_loads(manifest.read_bytes(), label=BUNDLE_MANIFEST)
                    if not isinstance(data, Mapping):
                        raise SyncProtocolError("Bundle manifest must be an object")
                    notification = SyncNotification(
                        transaction_id=data.get("transaction_id"),
                        bundle_sha256=data.get("bundle_sha256"),
                    )
                    source_revision = data.get("source_revision")
                    created_at = data.get("created_at")
                    if (
                        isinstance(source_revision, bool)
                        or not isinstance(source_revision, int)
                        or source_revision <= 0
                        or isinstance(created_at, bool)
                        or not isinstance(created_at, (int, float))
                        or not math.isfinite(created_at)
                    ):
                        raise SyncProtocolError("Bundle ordering metadata is invalid")
                except (OSError, SyncProtocolError):
                    # There is no trustworthy transaction ordering/identity to
                    # process offline. A direct add-on notification still gets
                    # the normal typed rejection.
                    continue
                candidates.append((
                    source_revision,
                    float(created_at),
                    notification.transaction_id,
                    notification,
                ))
        for _source_revision, _created_at, _transaction_id, notification in sorted(candidates):
            receipt = self.process(notification)
            receipts.append(receipt)
            if receipt.status is SyncStatus.CONFLICTS:
                # Preserve source order: a later Blender revision must not
                # leapfrog a transaction waiting for Webtoon conflict choices.
                break
        return receipts


RpcCallback = Callable[[SyncNotification], SyncReceipt]


class _LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], token: str, callback: RpcCallback) -> None:
        self.auth_token = token
        self.sync_callback = callback
        super().__init__(address, _RpcRequestHandler)


class _RpcRequestHandler(BaseHTTPRequestHandler):
    server: _LoopbackHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "WebtoonSync/1"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = canonical_json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _rpc_error(self, request_id: Any, code: int, message: str, *, status: int = 200) -> None:
        self._send_json(status, {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        })

    def _discard_bounded_body(self) -> None:
        """Drain small rejected requests so Windows sends the HTTP response.

        Closing a socket with unread inbound data can produce a TCP reset on
        Windows, racing and replacing the intended 4xx response. We never
        drain an unbounded or malformed length.
        """

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if 0 < length <= MAX_RPC_BYTES:
            self.rfile.read(length)
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._rpc_error(None, -32600, "Only authenticated POST is supported", status=405)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/rpc":
            self._discard_bounded_body()
            self._rpc_error(None, -32601, "Unknown endpoint", status=404)
            return
        expected = f"Bearer {self.server.auth_token}"
        if not secrets.compare_digest(self.headers.get("Authorization", ""), expected):
            self._discard_bounded_body()
            self._rpc_error(None, -32001, "Unauthorized", status=401)
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            self._discard_bounded_body()
            self._rpc_error(None, -32600, "Content-Type must be application/json", status=415)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_RPC_BYTES:
            self._rpc_error(None, -32600, "Request size is invalid", status=413)
            return
        try:
            request = strict_json_loads(self.rfile.read(length), label="JSON-RPC request")
            if not isinstance(request, Mapping):
                raise SyncProtocolError("JSON-RPC request must be an object", code="malformed_rpc")
            if set(request) != {"jsonrpc", "id", "method", "params"}:
                raise SyncProtocolError("JSON-RPC fields are invalid", code="malformed_rpc")
            request_id = request["id"]
            if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
                raise SyncProtocolError("JSON-RPC id must be a string or integer", code="malformed_rpc")
            if request["jsonrpc"] != "2.0" or request["method"] != RPC_METHOD:
                raise SyncProtocolError("Unknown JSON-RPC method", code="malformed_rpc")
            notification = SyncNotification.from_params(request["params"])
        except SyncProtocolError as exc:
            self._rpc_error(locals().get("request_id"), -32600, str(exc))
            return
        try:
            receipt = self.server.sync_callback(notification)
            if not isinstance(receipt, SyncReceipt):
                raise TypeError("sync callback must return SyncReceipt")
        except Exception:
            self._rpc_error(request_id, -32603, "Internal sync error", status=500)
            return
        self._send_json(200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": receipt.to_dict(),
        })


class SyncNotificationServer:
    """Small authenticated RPC server bound only to IPv4 loopback."""

    def __init__(
        self,
        callback: RpcCallback,
        *,
        port: int = 0,
        auth_token: str | None = None,
    ) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.auth_token = auth_token or secrets.token_urlsafe(32)
        if len(self.auth_token) < 32:
            raise ValueError("auth_token must contain at least 32 characters")
        self._server = _LoopbackHTTPServer(("127.0.0.1", port), self.auth_token, callback)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/rpc"

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "SyncNotificationServer":
        if self.is_running:
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="webtoon-blender-sync-rpc",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is None:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._thread = None

    def __enter__(self) -> "SyncNotificationServer":
        return self.start()

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.stop()


__all__ = [
    "ConcurrentRevisionError",
    "MAX_RPC_BYTES",
    "RPC_METHOD",
    "SyncInboxProcessor",
    "SyncNotification",
    "SyncNotificationServer",
]
