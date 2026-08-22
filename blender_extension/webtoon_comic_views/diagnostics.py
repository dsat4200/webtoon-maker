"""In-memory diagnostics that users can copy from the Blender panel."""
from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
import platform
import sys
import threading
import traceback
from typing import Any

import bpy


MAX_EVENTS = 200
_EVENTS: deque[str] = deque(maxlen=MAX_EVENTS)
_LOCK = threading.Lock()


def _text(value: object, *, limit: int = 2_000) -> str:
    rendered = str(value).replace("\r", "\\r").replace("\n", "\\n")
    return rendered if len(rendered) <= limit else rendered[:limit] + "..."


def record(level: str, event: str, **details: object) -> None:
    """Record one extension event and mirror it to Blender's system console."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    suffix = " ".join(
        f"{name}={_text(value)}" for name, value in sorted(details.items())
        if value not in (None, "")
    )
    line = f"[{timestamp}] {str(level).upper()}: {_text(event)}"
    if suffix:
        line += f" | {suffix}"
    with _LOCK:
        _EVENTS.append(line)
    print(f"Webtoon Comic Views {line}")


def record_exception(event: str, error: BaseException, **details: object) -> None:
    """Record an exception with its traceback while inside an except block."""
    trace = traceback.format_exc()
    if trace.strip() == "NoneType: None":
        trace = "".join(traceback.format_exception(error))
    record("ERROR", event, error=f"{type(error).__name__}: {error}", **details)
    with _LOCK:
        _EVENTS.append(trace[-12_000:].rstrip())


def recent_events() -> list[str]:
    with _LOCK:
        return list(_EVENTS)


def _active_view(scene: object) -> object | None:
    settings = getattr(scene, "webtoon_comic_settings", None)
    views = getattr(scene, "webtoon_comic_views", ())
    if settings is None:
        return None
    loaded_uuid = str(getattr(settings, "loaded_view_uuid", ""))
    if loaded_uuid:
        for view in views:
            if str(getattr(view, "view_uuid", "")) == loaded_uuid:
                return view
    index = int(getattr(settings, "active_index", -1))
    return views[index] if 0 <= index < len(views) else None


def build_report(
    context: object,
    runtime: object,
    *,
    extension_version: str,
    protocol_version: int,
) -> str:
    """Build a self-contained, token-free support report."""
    scene = getattr(context, "scene", None)
    settings = getattr(scene, "webtoon_comic_settings", None)
    server = getattr(runtime, "server", None)
    view = _active_view(scene) if scene is not None else None
    published_path = str(getattr(view, "published_frame_path", "") or "")
    published_state = "none"
    if published_path:
        try:
            frame_file = Path(published_path)
            published_state = (
                f"exists, {frame_file.stat().st_size} bytes"
                if frame_file.is_file() else "missing"
            )
        except OSError as error:
            published_state = f"inaccessible: {error}"

    lines = [
        "Webtoon Comic Views diagnostic report",
        f"Generated: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Extension: {extension_version}",
        f"Protocol: {protocol_version}",
        f"Blender: {bpy.app.version_string}",
        f"Python: {sys.version.split()[0]}",
        f"OS: {platform.platform()}",
        f"Bridge: {'connected' if getattr(runtime, 'connected', False) else 'listening' if getattr(server, 'running', False) else 'stopped'}",
        f"Bridge address: 127.0.0.1:{getattr(server, 'port', 'unknown')}",
        "Authentication token: [redacted]",
        f"Last error: {getattr(runtime, 'last_error', '') or 'none'}",
    ]
    if scene is not None:
        lines.extend([
            f"Blend file: {bpy.data.filepath or '[unsaved]'}",
            f"Scene: {getattr(scene, 'name', '[unknown]')}",
            f"Project UUID: {getattr(settings, 'project_uuid', '') or '[missing]'}",
            f"Comic Views: {len(getattr(scene, 'webtoon_comic_views', ()))}",
        ])
    if view is not None:
        lines.extend([
            f"Active view: {getattr(view, 'name', '[unnamed]')}",
            f"View UUID: {getattr(view, 'view_uuid', '') or '[missing]'}",
            f"Revision: {int(getattr(view, 'revision', 0))}",
            f"Timeline frame: {int(getattr(view, 'timeline_frame', 0)) or '[unassigned]'}",
            f"Bake status: {'ready' if int(getattr(view, 'timeline_frame', 0)) > 0 and getattr(view, 'bake_hash', '') else 'pending'}",
            f"Saved state: {'yes' if getattr(view, 'state_json', '') else 'no'}",
            f"Dirty: {bool(getattr(view, 'is_dirty', False))}",
            f"Published dimensions: {int(getattr(view, 'published_width', 0))}x{int(getattr(view, 'published_height', 0))}",
            f"Published frame: {published_path or '[none]'}",
            f"Published frame status: {published_state}",
        ])
    events = recent_events()
    lines.extend(["", f"Recent extension events ({len(events)}/{MAX_EVENTS}):"])
    lines.extend(events or ["[none recorded]"])
    return "\n".join(lines)
