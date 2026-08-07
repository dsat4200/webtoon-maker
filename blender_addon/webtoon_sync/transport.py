"""Authenticated JSON-RPC notification client for the Blender add-on."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid


RPC_METHOD = "sync.notify"


@dataclass(frozen=True)
class NotifyResult:
    state: str
    receipt: Mapping[str, Any] | None = None
    message: str = ""

    @property
    def accepted(self) -> bool:
        return self.state == "accepted"

    @property
    def queued(self) -> bool:
        return self.state == "queued"


def validate_loopback_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        raise ValueError("Webtoon endpoint must be a string")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/rpc"
        or parsed.port is None
    ):
        raise ValueError("Webtoon endpoint must be http://127.0.0.1:<port>/rpc")
    return endpoint


def notify_webtoon(
    endpoint: str,
    auth_token: str,
    *,
    transaction_id: str,
    bundle_sha256: str,
    timeout: float = 2.0,
) -> NotifyResult:
    """Notify Webtoon, returning ``queued`` on any connectivity failure."""

    endpoint = validate_loopback_endpoint(endpoint)
    if not isinstance(auth_token, str) or len(auth_token) < 32:
        raise ValueError("Webtoon auth token is missing or too short")
    transaction_id = str(uuid.UUID(transaction_id))
    if (
        not isinstance(bundle_sha256, str) or len(bundle_sha256) != 64
        or any(character not in "0123456789abcdef" for character in bundle_sha256)
    ):
        raise ValueError("bundle_sha256 must be lowercase SHA-256")
    request_id = str(uuid.uuid4())
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": RPC_METHOD,
        "params": {
            "transaction_id": transaction_id,
            "bundle_sha256": bundle_sha256,
        },
    }, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return NotifyResult("queued", message=f"Webtoon returned HTTP {response.status}")
            raw = response.read(128 * 1024 + 1)
    except HTTPError as exc:
        if 400 <= exc.code < 500:
            return NotifyResult(
                "rejected",
                message=f"Webtoon rejected the notification (HTTP {exc.code}); check the endpoint and token.",
            )
        return NotifyResult(
            "queued",
            message=f"Bundle is queued on disk; Webtoon returned HTTP {exc.code}.",
        )
    except (URLError, OSError, TimeoutError) as exc:
        return NotifyResult(
            "queued",
            message=f"Bundle is queued on disk; Webtoon was not reached ({type(exc).__name__}).",
        )
    if len(raw) > 128 * 1024:
        return NotifyResult("queued", message="Webtoon response exceeded the safe limit")
    try:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or value.get("id") != request_id:
            raise ValueError("mismatched JSON-RPC response")
        result = value.get("result")
        if not isinstance(result, dict):
            error = value.get("error")
            message = error.get("message", "Webtoon rejected the RPC") if isinstance(error, dict) else "Invalid Webtoon response"
            return NotifyResult("queued", message=message)
        state = result.get("status")
        if state not in {"accepted", "queued", "conflicts", "rejected"}:
            raise ValueError("unknown receipt status")
        return NotifyResult(state, receipt=result, message="")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return NotifyResult("queued", message=f"Invalid Webtoon response: {exc}")


__all__ = ["NotifyResult", "RPC_METHOD", "notify_webtoon", "validate_loopback_endpoint"]
