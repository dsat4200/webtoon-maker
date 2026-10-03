"""Small, per-user camera records, independent of document edits."""
import math


VIEWPORT_FIELDS = ("center_x", "center_y", "scale", "rotation")
MAX_SAVED_VIEWPORTS = 200


def normalize_viewport(value):
    if not isinstance(value, dict):
        return None
    try:
        camera = {field: float(value[field]) for field in VIEWPORT_FIELDS}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(number) for number in camera.values()) or camera["scale"] <= 0:
        return None
    camera["scale"] = max(.05, min(8., camera["scale"]))
    return camera
