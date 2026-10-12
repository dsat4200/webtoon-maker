"""Small detached workspace updates to the ordinary encoded preferences file.

The worker never receives an EditorSettings, brush library, or Qt object.
Full settings saves and workspace merges share one publication lock per path.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
from threading import Lock, RLock

from .viewport_state import MAX_SAVED_VIEWPORTS, normalize_viewport


@dataclass
class _PathOwner:
    publication: RLock = field(default_factory=RLock)
    full_generation: int = 0
    encoded: str | bytes | None = None


_owners = {}
_owners_lock = Lock()


def _owner(path):
    key = os.path.normcase(os.path.abspath(path))
    with _owners_lock:
        owner = _owners.get(key)
        if owner is None:
            owner = _owners[key] = _PathOwner()
        return owner


@contextmanager
def settings_read(path):
    with _owner(path).publication:
        yield


def remember_settings_encoding(path, encoded):
    """Retain only immutable JSON already validated or successfully written."""
    owner = _owner(path)
    with owner.publication:
        owner.encoded = encoded


@contextmanager
def full_settings_write(path):
    owner = _owner(path)
    # Invalidate already queued workspace packets before waiting for IO.
    # This full save includes the caller's current workspace fields itself.
    with _owners_lock:
        owner.full_generation += 1
    with owner.publication:
        yield


def workspace_snapshot(settings):
    """Copy and validate only bounded, scalar workspace fields on the GUI."""
    splitters = {}
    for key in ('sidebar_workspace', 'tools_colors', 'ribbon_canvas',
                'tool_canvas', 'outliner_settings'):
        values = settings.ui_splitter_sizes.get(key)
        if isinstance(values, (list, tuple)) and len(values) == 2:
            try:
                values = [max(0, min(100_000, int(value))) for value in values]
            except (TypeError, ValueError, OverflowError):
                continue
            if sum(values):
                splitters[key] = values
    views = {}
    for key, value in settings.document_viewports.items():
        camera = normalize_viewport(value)
        if isinstance(key, str) and camera is not None:
            views[key] = camera
            if len(views) > MAX_SAVED_VIEWPORTS:
                views.pop(next(iter(views)))
    return {'ui_splitter_sizes': splitters,
            'navigator_expanded': bool(settings.navigator_expanded),
            'document_viewports': views}


def _write_workspace(path, packet, owner, generation):
    from .brush_storage import atomic_write
    with owner.publication:
        with _owners_lock:
            if generation != owner.full_generation:
                return
        # Preserve the encoded brush references and every unrelated field.
        # Do not decode, clamp, hash or restat the complete brush library.
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                raise ValueError('Settings must be an object')
        except (OSError, ValueError):
            if owner.encoded is None:
                raise
            # Repair from the last complete encoded representation. The
            # worker cannot borrow or serialize the live mutable brush model.
            raw = json.loads(owner.encoded)
        raw.update(packet)
        encoded = json.dumps(raw, indent=2).encode('utf-8')
        atomic_write(path, lambda stream: stream.write(encoded))
        owner.encoded = encoded


class WorkspacePreferencesWriter:
    """One active write and one latest detached packet; close drains both."""
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='workspace-preferences')
        self._lock = Lock()
        self._pending = None
        self._running = self._closed = False
        self._future = None
        self.error = None

    @property
    def closed(self):
        return self._closed

    def submit(self, path, settings):
        packet = workspace_snapshot(settings)
        path = Path(path)
        owner = _owner(path)
        with _owners_lock:
            generation = owner.full_generation
        with self._lock:
            if self._closed:
                raise RuntimeError('Workspace preferences writer is closed')
            self._pending = path, packet, owner, generation
            if not self._running:
                self._running = True
                self._future = self._executor.submit(self._write_pending)

    def _write_pending(self):
        while True:
            with self._lock:
                packet, self._pending = self._pending, None
                if packet is None:
                    self._running = False
                    return
            try:
                _write_workspace(*packet)
            except Exception as error:
                self.error = error
                logging.getLogger(__name__).exception('Workspace preferences save failed')
            else:
                self.error = None

    def flush(self):
        while True:
            with self._lock:
                future, running = self._future, self._running
            if future is not None:
                future.result()
            if not running:
                break
        if self.error is not None:
            raise self.error

    def shutdown(self):
        with self._lock:
            self._closed = True
        try:
            self.flush()
        finally:
            self._executor.shutdown(wait=True)
