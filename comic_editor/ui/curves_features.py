"""Isolated input histograms and transactional on-canvas Curves pickers."""
from __future__ import annotations

import copy
import math

import numpy as np
from PySide6.QtCore import QObject, QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from comic_editor.core.curves import CURVE_CHANNELS, color_coordinates, curve_graph_values, evaluate_curve
from comic_editor.core.models import ColorFillGradientObject, CurvesModifier
from comic_editor.ui.modifier_rendering import _qimage_premultiplied, _straight
from comic_editor.render.pixels import current_contract


def _targets(canvas, modifier_id):
    if canvas.chapter is None:
        return []
    linked = canvas.chapter.modifier_target_ids(modifier_id)
    selected = [ref for ref in canvas.selected_entities if ref in linked]
    primary = (canvas.selected_kind, canvas.selected_id)
    if primary in selected:
        selected.remove(primary)
        selected.append(primary)
    return selected or linked


def _records(canvas, modifier_id):
    from comic_editor.ui.baking import visual_bounds
    records, keys = [], []
    for kind, identifier in _targets(canvas, modifier_id):
        target = canvas.chapter.modifier_target(kind, identifier)
        if target is None or modifier_id not in target.modifier_ids:
            continue
        original = target.modifier_ids
        prefix = original[:original.index(modifier_id)]
        try:
            target.modifier_ids = prefix
            signature = (canvas._modifier_layer_signature(identifier) if kind == "layer"
                         else canvas._modifier_object_signature(target))
            bounds = visual_bounds(canvas, kind, identifier)
            parent = target.parent_id if kind == "layer" else target.parent_layer_id
            mapping = canvas.layer_world_transform(parent) if parent else QTransform()
            keys.append((kind, identifier, signature, canvas._rect_signature(bounds),
                         tuple(getattr(mapping, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4))))
            records.append((kind, target, prefix, bounds, mapping))
        finally:
            target.modifier_ids = original
    return (id(canvas.chapter), tuple(keys)), records


def _capture(canvas, record, bounds=None, max_edge=512):
    kind, target, prefix, natural_bounds, mapping = record
    bounds = QRectF(natural_bounds if bounds is None else bounds)
    inverse, valid = mapping.inverted()
    if bounds.isEmpty() or not valid:
        return QImage(), 0.
    scale = min(1., max_edge / max(bounds.width(), bounds.height()))
    width, height = max(1, math.ceil(bounds.width() * scale)), max(1, math.ceil(bounds.height() * scale))
    image = QImage(width, height, current_contract().image_format)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    original = (target.modifier_ids, target.visible, target.mask_only,
                target.opacity, target.opacity_mask,
                canvas._interactive_render, canvas._rendering_compound_references,
                canvas._rendering_outward_gradient)
    try:
        target.modifier_ids, target.visible, target.mask_only = prefix, True, False
        # Owner opacity and its opacity mask are applied after the modifier
        # stack. Sampling them here would put Alpha nodes at the wrong input.
        target.opacity, target.opacity_mask = 1., None
        canvas._interactive_render = False
        canvas._rendering_compound_references = True
        canvas._rendering_outward_gradient = bool(kind == "object" and isinstance(target, ColorFillGradientObject)
                                                 and canvas._is_outward_gradient(target))
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.scale(width / bounds.width(), height / bounds.height())
        painter.translate(-bounds.left(), -bounds.top())
        painter.setTransform(mapping, True)
        with canvas.without_solo():
            if kind == "layer":
                canvas._render_layer(painter, target, 1., bounds)
            else:
                if isinstance(target, ColorFillGradientObject) and not canvas._rendering_outward_gradient and not target.ignore_parent_mask:
                    painter.setClipPath(canvas.layer_effective_path(target.parent_layer_id), Qt.IntersectClip)
                from comic_editor.ui.object_blending import suspend_object_blend
                with suspend_object_blend(canvas, target.object_id):
                    canvas._render_object(painter, target, 1., inverse.mapRect(bounds))
    finally:
        (target.modifier_ids, target.visible, target.mask_only, target.opacity, target.opacity_mask, canvas._interactive_render,
         canvas._rendering_compound_references, canvas._rendering_outward_gradient) = original
        painter.end()
    return image, bounds.width() * bounds.height() / (width * height)


class CurvesSampler:
    def __init__(self):
        self.key = None
        self.samples = []
        self.busy = False

    def histogram(self, canvas, modifier_id, mode, channel):
        if self.busy:
            return np.zeros(256)
        modifier = canvas.chapter.modifiers.get(modifier_id) if canvas.chapter else None
        if not isinstance(modifier, CurvesModifier):
            return np.zeros(256)
        self.busy = True
        try:
            key, records = _records(canvas, modifier_id)
            if key != self.key:
                samples = []
                for record in records:
                    image, weight = _capture(canvas, record)
                    if not image.isNull():
                        samples.append((_straight(_qimage_premultiplied(image)), weight))
                self.samples = samples
                self.key = key if sum(a.nbytes for a, _ in samples) <= 32 * 1024 * 1024 else None
            result = np.zeros(256, np.float64)
            selected = copy.copy(modifier)
            selected.color_mode, selected.channel = mode, channel
            for pixels, area in self.samples:
                alpha = pixels[..., 3]
                values = curve_graph_values(pixels[..., :3], alpha, selected, channel)
                weights = alpha * area
                if values.ndim > alpha.ndim:
                    weights = np.broadcast_to(weights[..., None] / values.shape[-1], values.shape)
                indices = np.minimum((np.clip(values, 0., 1.) * 256).astype(np.int32), 255)
                result += np.bincount(indices.ravel(), weights=weights.ravel(), minlength=256)
            return result
        finally:
            self.busy = False

    def pixel(self, canvas, modifier_id, world):
        _, records = _records(canvas, modifier_id)
        # Prefer the primary selected target, then other linked inputs beneath.
        bounds = QRectF(math.floor(world.x()) - 1, math.floor(world.y()) - 1, 3, 3)
        for record in reversed(records):
            if not record[3].intersects(bounds):
                continue
            image, _ = _capture(canvas, record, bounds)
            if not image.isNull():
                if not current_contract().floating:
                    color = image.pixelColor(1, 1)
                    if color.alpha() > 0:
                        return np.array((color.redF(), color.greenF(), color.blueF())), color.alphaF()
                    continue
                pixel = _straight(_qimage_premultiplied(image))[1, 1]
                if pixel[3] > 0:
                    return pixel[:3].copy(), float(pixel[3])
        return None


def _point(curves, key, x, y=None):
    points = [list(p) for p in curves.get(key, [(0., 0.), (1., 1.)])]
    x = float(np.clip(x, 0., 1.))
    y = float(evaluate_curve(points, np.asarray(x))) if y is None else float(np.clip(y, 0., 1.))
    index = min(range(len(points)), key=lambda i: abs(points[i][0] - x))
    if abs(points[index][0] - x) < 1e-4 or len(points) >= 256:
        points[index][1] = y
    else:
        points.append([x, y])
        points.sort()
        index = next(i for i, p in enumerate(points) if p[0] == x)
    curves[key] = points
    return index


class CurvesPicker(QObject):
    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.sampler = CurvesSampler()
        self.state = None
        self.updating = False
        self.serial = 0
        canvas.installEventFilter(self)
        for signal in (canvas.chapterReplaced, canvas.selectionSetChanged, canvas.toolChanged,
                       canvas.modifierSelectionChanged):
            signal.connect(self.cancel)
        canvas.documentChanged.connect(self.validate)
        canvas.hierarchyChanged.connect(self.validate)

    def start(self, modifier_id, mode, color_mode, channel):
        self.cancel()
        modifier = self.canvas.chapter.modifiers.get(modifier_id) if self.canvas.chapter else None
        if not isinstance(modifier, CurvesModifier) or not _targets(self.canvas, modifier_id):
            return
        if mode not in {"add_point", "black_point", "white_point", "gray_point", "white_balance"}:
            return
        if color_mode not in CURVE_CHANNELS or channel not in CURVE_CHANNELS[color_mode]:
            return
        self.state = dict(modifier_id=modifier_id, mode=mode, color_mode=color_mode, channel=channel,
                          chapter=self.canvas.chapter, cursor=self.canvas.cursor(), before=None,
                          pending=False, last_position=None, released=None)
        self.canvas.setCursor(Qt.CrossCursor)
        self.canvas.setFocus(Qt.OtherFocusReason)
        self.canvas.importStatusMessage.emit("Curves: click the artwork; drag up/down to adjust a point. Escape cancels.")

    def validate(self, *_):
        if self.state is None or self.updating:
            return
        if self.state['pending']:
            self.cancel()
            return
        modifier = self.canvas.chapter.modifiers.get(self.state["modifier_id"]) if self.canvas.chapter else None
        if (self.canvas.chapter is not self.state["chapter"] or not isinstance(modifier, CurvesModifier)
                or modifier.color_mode != self.state["color_mode"] or not _targets(self.canvas, self.state["modifier_id"])):
            self.cancel()

    def changed(self, before=None):
        self.updating = True
        try:
            if before is not None:
                from comic_editor.ui.record_edits import preview_records
                preview_records(self.canvas, before, 'Pick Curves point')
            else:
                self.canvas._compound_path_cache.clear()
                self.canvas._invalidate_scene_cache()
                self.canvas.update()
                self.canvas.documentChanged.emit(None)
        finally:
            self.updating = False

    def cancel(self, *_):
        state, self.state = self.state, None
        self.serial += 1
        if state is None:
            return
        self.canvas.setCursor(state["cursor"])
        if state["before"] is not None and self.canvas.chapter is state["chapter"]:
            modifier = self.canvas.chapter.modifiers.get(state["modifier_id"])
            if isinstance(modifier, CurvesModifier):
                before = self._curve_snapshot(state['modifier_id'])
                modifier.curves = state["curves_before"]
                self.changed(before)
        self.canvas.importStatusMessage.emit("")

    def press(self, position):
        from comic_editor.render.source_sampling import curves_pixel
        from comic_editor.ui.scene_consumers import scene_consumers
        state = self.state
        self.serial += 1
        serial = self.serial
        state.update(pending=True, last_position=QPointF(position), released=None)
        position = QPointF(position)
        world = self.canvas.widget_to_document(position)
        def accept(sampled, error):
            if self.state is not state or serial != self.serial:
                return
            state['pending'] = False
            if error is not None:
                self.canvas.importStatusMessage.emit('Curves: could not sample the artwork.')
                return
            self._apply_sample(position, sampled)
            if state['released'] is not None:
                self.release(state['released'])
            elif state['last_position'] is not None:
                self.move(state['last_position'])
        scene_consumers(self.canvas).request(('curves-picker',), curves_pixel,
            (state['modifier_id'], (world.x(), world.y()), tuple(self.canvas.selected_entities)), accept)

    def _curve_snapshot(self, modifier_id):
        from comic_editor.core.document_patch import RecordSnapshot
        return RecordSnapshot.capture(self.canvas.chapter, modifiers=(modifier_id,),
                                      attributes={'modifiers': ('curves',)})

    def _apply_sample(self, position, sampled):
        state = self.state
        if sampled is None:
            self.canvas.importStatusMessage.emit("Curves: click a visible pixel of the selected artwork.")
            return
        modifier = self.canvas.chapter.modifiers[state["modifier_id"]]
        rgb, alpha = sampled
        state["before"] = self._curve_snapshot(state['modifier_id'])
        state["curves_before"] = copy.deepcopy(modifier.curves)
        state["start_y"] = position.y()
        mode, channel = state["color_mode"], state["channel"]
        key = f"{mode}:{channel}"
        values = curve_graph_values(rgb, alpha, modifier, channel)
        x = float(np.clip(np.mean(values), 0., 1.))
        curves = copy.deepcopy(modifier.curves)
        selected_point = None
        if state["mode"] == "white_balance":
            luminance = float(rgb @ np.asarray((.2126, .7152, .0722)))
            neutral = color_coordinates(np.full(3, luminance), mode)
            for index, selected in enumerate(CURVE_CHANNELS[mode][1:-1]):
                source_x = float(curve_graph_values(rgb, alpha, modifier, selected))
                output_y = (neutral[index] - modifier.input_min) / (modifier.input_max - modifier.input_min)
                point_index = _point(curves, f"{mode}:{selected}", source_x, output_y)
                if selected_point is None or selected == channel:
                    selected_point = (f"{mode}:{selected}", point_index)
            if mode == "gray":
                selected_point = (key, _point(curves, key, x))
        elif state["mode"] in {"black_point", "white_point"}:
            black = state["mode"] == "black_point"
            if channel == "master" and mode == "lab":
                # A Lab neutral has centered a/b coordinates, not all zero or
                # all one. Anchor the physical channels to that neutral.
                neutral = color_coordinates(np.full(3, 0. if black else 1.), mode)
                for index, selected in enumerate(CURVE_CHANNELS[mode][1:-1]):
                    source_x = float(curve_graph_values(rgb, alpha, modifier, selected))
                    output_y = (neutral[index] - modifier.input_min) / (modifier.input_max - modifier.input_min)
                    point_index = _point(curves, f"{mode}:{selected}", source_x, output_y)
                    if index == 0:
                        selected_point = (f"{mode}:{selected}", point_index)
                modifier.curves = curves
                modifier.validate()
                self.changed(state['before'])
                self.canvas.curvesPointSelected.emit(state["modifier_id"], *selected_point)
                return
            low_endpoint = black != (channel == "master" and mode == "cmyk")
            if channel == "master":
                x = float(np.max(values) if low_endpoint else np.min(values))
            points = [list(p) for p in curves.get(key, [(0., 0.), (1., 1.)])]
            if low_endpoint:
                points = ([[0., 0.], [1., 0.]] if x >= points[-1][0]
                          else [[x, 0.]] + [p for p in points if p[0] > x])
            else:
                points = ([[0., 1.], [1., 1.]] if x <= points[0][0]
                          else [p for p in points if p[0] < x] + [[x, 1.]])
            curves[key] = points
            selected_point = (key, 0 if low_endpoint else len(points) - 1)
        else:
            y = (.5 - modifier.input_min) / (modifier.input_max - modifier.input_min) if state["mode"] == "gray_point" else None
            state["point"] = _point(curves, key, x, y)
            state["initial_y"] = curves[key][state["point"]][1]
            selected_point = (key, state["point"])
        modifier.curves = curves
        modifier.validate()
        self.changed(state['before'])
        if selected_point is not None:
            self.canvas.curvesPointSelected.emit(state["modifier_id"], *selected_point)

    def move(self, position):
        state = self.state
        if state['pending']:
            state['last_position'] = QPointF(position)
            return
        if state["before"] is None or state["mode"] != "add_point":
            return
        modifier = self.canvas.chapter.modifiers[state["modifier_id"]]
        before = self._curve_snapshot(state['modifier_id'])
        key = f"{state['color_mode']}:{state['channel']}"
        point = modifier.curves[key][state["point"]]
        modifier.curves[key][state["point"]] = (point[0], float(np.clip(
            state["initial_y"] + (state["start_y"] - position.y()) / 200., 0., 1.)))
        self.changed(before)

    def release(self, position):
        state = self.state
        if state['pending']:
            state['released'] = QPointF(position)
            state['last_position'] = QPointF(position)
            return
        if state["before"] is None:
            return
        self.move(position)
        self.state = None
        self.canvas.setCursor(state["cursor"])
        from comic_editor.ui.record_edits import commit_records
        commit_records(self.canvas, state['before'], 'Pick Curves point')
        self.canvas.importStatusMessage.emit("")

    def eventFilter(self, watched, event):
        if self.state is None:
            return False
        kind = event.type()
        if kind == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.cancel()
        elif kind in {QEvent.MouseButtonPress, QEvent.TabletPress}:
            if event.button() == Qt.LeftButton:
                self.press(event.position())
            else:
                self.cancel()
        elif kind in {QEvent.MouseMove, QEvent.TabletMove}:
            self.move(event.position())
        elif kind in {QEvent.MouseButtonRelease, QEvent.TabletRelease}:
            self.release(event.position())
        else:
            return False
        event.accept()
        return True


class CurvesFeatures:
    def _curves_controller(self):
        if not hasattr(self, "_curves_picker"):
            self._curves_picker = CurvesPicker(self)
        return self._curves_picker

    def curves_histogram(self, modifier_id, color_mode, channel):
        return self._curves_controller().sampler.histogram(self, modifier_id, color_mode, channel)

    def start_curves_picker(self, modifier_id, mode, color_mode="rgb", channel="master"):
        self._curves_controller().start(modifier_id, mode, color_mode, channel)

    def cancel_curves_picker(self):
        if hasattr(self, "_curves_picker"):
            self._curves_picker.cancel()
