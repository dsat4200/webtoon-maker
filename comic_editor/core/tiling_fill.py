"""Finite flood filling and morphology on the tile's periodic pixel graph."""
import math
import numpy as np
from scipy.ndimage import label, distance_transform_edt
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from comic_editor.core.tiling import map_arrays


def periodic_fill(store, object_id, point, color, profile, before, *, region_policy,
                  reference_tile, selection_tile, cancel_check):
    cancelled = cancel_check or (lambda: False)
    if cancelled():
        return QRectF()
    geometry, mapping = profile["_tiling_context"]
    inverse = mapping.inverted()[0]
    frame = inverse.map(geometry.path()).boundingRect().toAlignedRect()
    left, top, width, height = frame.x(), frame.y(), frame.width(), frame.height()
    if width*height > 16*1024*1024:
        raise ValueError("The tile is too large to fill. Reduce its side length.")
    if width <= 0 or height <= 0:
        return QRectF()
    x, y = np.meshgrid(left+np.arange(width)+.5, top+np.arange(height)+.5)
    def folded(px, py):
        return map_arrays(inverse, *geometry.map(*map_arrays(mapping, px, py)))
    valid = geometry.pixel_mask(QRectF(frame), width, height, mapping)
    if not valid.any():
        return QRectF()
    nearest = distance_transform_edt(~valid, return_distances=False, return_indices=True)
    def indices(px, py):
        fx, fy = folded(px, py)
        ix = np.clip(np.floor(fx-left).astype(np.int64), 0, width-1)
        iy = np.clip(np.floor(fy-top).astype(np.int64), 0, height-1)
        return nearest[0, iy, ix], nearest[1, iy, ix]
    size = store.tile_size
    keys = store.keys_for_rect(QRectF(frame))
    reference = np.zeros((height, width, 4), np.uint8)
    selection = valid.copy()
    slices = {}
    for key in keys:
        if cancelled():
            return QRectF()
        ox, oy = key[0]*size, key[1]*size
        x0, y0 = max(left, ox), max(top, oy)
        x1, y1 = min(left+width, ox+size), min(top+height, oy+size)
        dst = (slice(y0-top, y1-top), slice(x0-left, x1-left))
        src = (slice(y0-oy, y1-oy), slice(x0-ox, x1-ox))
        slices[key] = (dst, src)
        image = reference_tile(key) if reference_tile else store._tiles.get(object_id, {}).get(key)
        reference[dst] = store._rgba_array(image, size)[src]
        selected = selection_tile(key) if selection_tile else None
        if selected is not None:
            selection[dst] &= selected[src].astype(bool)
    seed = indices(point.x(), point.y()) if point is not None else (0, 0)
    seed = tuple(int(v) for v in seed)
    target = reference[seed].copy()
    blocked = QColor(str(profile.get("do_not_start_color", "")))
    tolerance = int(profile.get("tolerance", 16))
    if point is not None and blocked.isValid() and np.max(np.abs(target.astype(int)-np.array(blocked.getRgb()))) <= tolerance:
        return QRectF()
    raw = store._fill_region_mask(reference, target, tolerance, region_policy)
    offsets4 = ((-1, 0), (1, 0), (0, -1), (0, 1))
    offsets8 = tuple((a, b) for a in (-1, 0, 1) for b in (-1, 0, 1) if a or b)
    # Cache only the eight nearest neighbours, bounding memory independently of
    # gap size. Wider filters obtain one offset at a time.
    neighbors = {offset: indices(x+offset[0], y+offset[1]) for offset in offsets8}
    def morphology(field, iterations, expand, square=True):
        field = field.copy()
        for _ in range(iterations):
            if cancelled():
                return field
            result = field.copy()
            for offset in offsets8 if square else offsets4:
                if expand:
                    result |= field[neighbors[offset]]
                else:
                    result &= field[neighbors[offset]]
            field = result & valid
        return field
    matching = raw & selection
    gap = min(16, max(0, round(float(profile.get("gap_threshold", 0.)))))
    if profile.get("close_gap", False) and gap:
        blockers = morphology(~raw & valid, gap, True)
        blockers = morphology(blockers, gap, False)
        matching &= ~blockers
    if not profile.get("fill_narrow_areas", True):
        matching = morphology(morphology(matching, 1, False), 1, True) & selection
    if profile.get("connected_pixels_only", True) and point is not None:
        labels, _ = label(matching)
        seed_label = int(labels[seed])
        if seed_label == 0:
            return QRectF()
        adjacency = {}
        for offset in offsets4:
            other = labels[neighbors[offset]]
            crossing = (labels > 0) & (other > 0) & (labels != other)
            for a, b in np.unique(np.stack((labels[crossing], other[crossing]), axis=1), axis=0):
                adjacency.setdefault(int(a), set()).add(int(b))
                adjacency.setdefault(int(b), set()).add(int(a))
        seen, pending = set(), [seed_label]
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(adjacency.get(current, set())-seen)
        matching = np.isin(labels, tuple(seen))
    original = matching.copy()
    amount = float(profile.get("area_amount", 0.)) if profile.get("area_scaling", False) else 0.
    if amount:
        matching = morphology(matching, min(64, round(abs(amount))), amount > 0,
                              profile.get("area_mode") == "rectangle") & selection
        if profile.get("area_mode") == "darkest_pixel" and amount > 0:
            matching &= (reference[..., 3] < 250) | original
    if profile.get("include_vector_path", False):
        matching = morphology(matching, 1, True) & selection
    coverage = matching.astype(np.float32)
    if profile.get("antialiasing", True):
        weights = np.exp(-np.arange(-2, 3, dtype=float)**2/(2*.65**2))
        weights /= weights.sum()
        blurred = np.zeros_like(coverage)
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                if cancelled():
                    return QRectF()
                iy, ix = neighbors.get((dx, dy), (None, None))
                if iy is None:
                    iy, ix = indices(x+dx, y+dy)
                blurred += coverage[iy, ix]*weights[dx+2]*weights[dy+2]
        coverage = blurred
    coverage *= selection*max(0., min(1., float(profile.get("opacity", 100))/100))
    blend = str(profile.get("blend_mode", "normal"))
    pending = {}
    dirty = QRectF()
    for key, (dst, src) in slices.items():
        if cancelled():
            return QRectF()
        mask = np.zeros((size, size), np.float32)
        mask[src] = coverage[dst]
        if not np.any(mask):
            continue
        previous = store._tiles.get(object_id, {}).get(key)
        image = QImage(previous) if previous is not None else store._empty(size)
        special = store._special_blend(image, mask, color, blend)
        if special is not None:
            image = special
        else:
            erase = blend == "erase" or (color.alpha() == 0 and blend == "normal")
            rgba = np.empty((size, size, 4), np.uint8)
            rgba[..., :3] = color.getRgb()[:3]
            rgba[..., 3] = np.rint(mask*(255 if erase else color.alpha())).astype(np.uint8)
            source = QImage(rgba.data, size, size, size*4, QImage.Format_RGBA8888).copy()
            painter = QPainter(image)
            painter.setCompositionMode(QPainter.CompositionMode_DestinationOut if erase else
                QPainter.CompositionMode_DestinationOver if blend == "background" else
                QPainter.CompositionMode_Lighten if blend == "compare_density" else store._composition_mode(blend))
            painter.drawImage(0, 0, source)
            painter.end()
        pending[key] = image
        ys, xs = np.nonzero(mask)
        rect = QRectF(key[0]*size+int(xs.min()), key[1]*size+int(ys.min()), int(xs.max()-xs.min()+1), int(ys.max()-ys.min()+1))
        dirty = rect if dirty.isEmpty() else dirty.united(rect)
    if cancelled():
        return QRectF()
    for key, image in pending.items():
        if before is not None and key not in before:
            previous = store._tiles.get(object_id, {}).get(key)
            before[key] = QImage(previous) if previous is not None else None
        store.set_tile(object_id, key, image)
    return dirty
