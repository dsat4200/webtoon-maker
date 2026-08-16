"""Standalone Blender-side bootstrap. This file must not import the editor."""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import socket
import sys
import threading

import bpy


def _arguments():
    raw = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    return parser.parse_args(raw)


ARGS = _arguments()
SOCKET = socket.create_connection(("127.0.0.1", ARGS.port), timeout=20.0)
SOCKET.settimeout(None)
SEND_LOCK = threading.Lock()
COMMANDS = queue.Queue()
RUNNING = True
READY_SENT = False
LAST_STATE_KEY = None


def _send(message):
    message["token"] = ARGS.token
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
    with SEND_LOCK:
        SOCKET.sendall(payload)


def _reader():
    global RUNNING
    pending = bytearray()
    try:
        while RUNNING:
            chunk = SOCKET.recv(65536)
            if not chunk:
                break
            pending.extend(chunk)
            while b"\n" in pending:
                line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if message.get("token") == ARGS.token:
                    COMMANDS.put(message)
    finally:
        RUNNING = False


threading.Thread(target=_reader, name="WebtoonBlenderIPC", daemon=True).start()


def _view_context():
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((item for item in area.regions if item.type == "WINDOW"), None)
            space = area.spaces.active
            if region is not None and space.region_3d is not None:
                return window, area, region, space, space.region_3d
    window = bpy.context.window
    if window and window.screen and window.screen.areas:
        area = max(window.screen.areas, key=lambda item: item.width * item.height)
        area.type = "VIEW_3D"
        region = next((item for item in area.regions if item.type == "WINDOW"), None)
        space = area.spaces.active
        if region is not None:
            return window, area, region, space, space.region_3d
    return None


def _state():
    context = _view_context()
    if context is None:
        raise RuntimeError("No Blender VIEW_3D is available")
    _window, _area, _region, space, view = context
    return {
        "rotation": list(view.view_rotation),
        "location": list(view.view_location),
        "distance": float(view.view_distance),
        "perspective": str(view.view_perspective),
        "lens": float(space.lens),
        "camera_zoom": float(view.view_camera_zoom),
        "camera_offset": list(view.view_camera_offset),
    }


def _state_key(state):
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def _finite_sequence(value, length):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError("Invalid viewport vector")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError("Viewport values must be finite")
    return result


def _apply_state(payload):
    context = _view_context()
    if context is None:
        raise RuntimeError("No Blender VIEW_3D is available")
    _window, _area, _region, space, view = context
    rotation = _finite_sequence(payload.get("rotation"), 4)
    magnitude = math.sqrt(sum(value * value for value in rotation))
    rotation = (
        (1.0, 0.0, 0.0, 0.0) if magnitude <= 1e-12
        else tuple(value / magnitude for value in rotation)
    )
    location = _finite_sequence(payload.get("location"), 3)
    offset = _finite_sequence(payload.get("camera_offset", [0.0, 0.0]), 2)
    distance = max(0.0, float(payload.get("distance", 10.0)))
    lens = max(1.0, min(250.0, float(payload.get("lens", 50.0))))
    zoom = max(-30.0, min(600.0, float(payload.get("camera_zoom", 0.0))))
    perspective = str(payload.get("perspective", "PERSP"))
    if perspective not in {"PERSP", "ORTHO", "CAMERA"}:
        raise ValueError("Unknown viewport projection")
    if not all(math.isfinite(value) for value in (distance, lens, zoom)):
        raise ValueError("Viewport values must be finite")
    view.view_rotation = rotation
    view.view_location = location
    view.view_distance = distance
    view.view_perspective = perspective
    view.view_camera_zoom = zoom
    view.view_camera_offset = offset
    space.lens = lens


def _configure_view():
    context = _view_context()
    if context is None:
        return False
    window, area, region, space, _view = context
    if len(window.screen.areas) > 1:
        try:
            with bpy.context.temp_override(
                window=window, area=area, region=region
            ):
                bpy.ops.screen.screen_full_area(use_hide_panels=True)
            return False
        except RuntimeError:
            pass
    space.show_region_toolbar = False
    space.show_region_ui = False
    space.overlay.show_text = False
    return area.type == "VIEW_3D"


def _handle(message):
    global LAST_STATE_KEY
    request_id = int(message.get("id", 0))
    command = str(message.get("command", ""))
    try:
        if command == "PING":
            payload = {"pong": True}
        elif command == "GET_VIEW_STATE":
            payload = _state()
        elif command == "SET_VIEW_STATE":
            _apply_state(message.get("payload") or {})
            payload = _state()
            LAST_STATE_KEY = _state_key(payload)
        else:
            raise ValueError(f"Unknown command: {command}")
        _send({"id": request_id, "ok": True, "payload": payload})
    except Exception as error:
        _send({"id": request_id, "ok": False, "payload": str(error)})


def _pump():
    global READY_SENT, LAST_STATE_KEY
    if not RUNNING:
        return None
    try:
        configured = _configure_view()
        if configured and not READY_SENT:
            _send({"event": "READY", "pid": os.getpid()})
            READY_SENT = True
        while True:
            try:
                message = COMMANDS.get_nowait()
            except queue.Empty:
                break
            _handle(message)
        if READY_SENT:
            current = _state()
            key = _state_key(current)
            if LAST_STATE_KEY is None:
                LAST_STATE_KEY = key
            elif key != LAST_STATE_KEY:
                LAST_STATE_KEY = key
                _send({"event": "VIEW_STATE_CHANGED", "payload": current})
    except Exception:
        pass
    return 0.1


bpy.app.timers.register(_pump, first_interval=0.05, persistent=True)
