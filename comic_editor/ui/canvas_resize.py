"""Height-only canvas resizing with a focused, source-preserving history edit."""
from copy import deepcopy

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QPolygonF, QTransform
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QSpinBox

from comic_editor.core.document_patch import RecordSnapshot
from comic_editor.ui.record_edits import commit_records


class ResizeCanvasDialog(QDialog):
    def __init__(self, height, minimum, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Resize Canvas")
        self.setObjectName("resizeCanvasDialog")
        self._original_height = height
        layout = QFormLayout(self)
        self.height = QSpinBox(self)
        self.height.setObjectName("canvasHeight")
        self.height.setRange(minimum, 10_000_000)
        self.height.setSuffix(" px")
        self.height.setValue(max(minimum, height))
        layout.addRow(f"Height (minimum {minimum} px):", self.height)
        self.add_position = QComboBox(self)
        self.add_position.setObjectName("canvasHeightAddPosition")
        self.add_position.addItem("Bottom", "bottom")
        self.add_position.addItem("Top", "top")
        layout.addRow("Add extra height to:", self.add_position)
        self.height.valueChanged.connect(self._height_changed)
        self._height_changed(self.height.value())
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _height_changed(self, value):
        self.add_position.setEnabled(value > self._original_height)


def minimum_canvas_height(canvas):
    chapter = canvas.chapter
    minimum = chapter.minimum_safe_height()
    for object_id in chapter.objects:
        rect = canvas.object_world_rect(object_id)
        if rect is not None:
            minimum = max(minimum, int(rect.bottom() + .999))
    return minimum


def _shifted_root(layer, delta):
    """Keep valid quads exact; materialize the renderer's singular fallback."""
    if layer.transform_frame is not None and layer.transform_quad is not None:
        frame = QRectF(*layer.transform_frame)
        corners = [frame.topLeft(), frame.topRight(), frame.bottomRight(), frame.bottomLeft()]
        mapping = QTransform.quadToQuad(QPolygonF(corners),
            QPolygonF([QPointF(*point) for point in layer.transform_quad]))
        quad = (layer.transform_quad if isinstance(mapping, QTransform)
                else [point.toTuple() for point in corners])
        return {"transform_quad": [(x, y + delta) for x, y in quad]}
    return {"translate_y": layer.translate_y + delta}


def resize_canvas_height(canvas, height, add_to="bottom"):
    """Add Top space by rebasing world data; shrinking always trims Bottom."""
    chapter = canvas.chapter
    if chapter is None or chapter.document_kind == "image":
        raise ValueError("This document has a fixed canvas size")
    if add_to not in {"bottom", "top"}:
        raise ValueError("Additional height must go to Bottom or Top")
    height = int(height)
    if not minimum_canvas_height(canvas) <= height <= 10_000_000:
        raise ValueError("Canvas height cannot cut existing artwork or exceed 10000000 px")
    if height == chapter.height:
        return False
    delta = height - chapter.height if add_to == "top" and height > chapter.height else 0
    if not delta:
        before = RecordSnapshot.capture(chapter, scalars=("size",))
        chapter.trim_height(height)
        return commit_records(canvas, before, "Resize Canvas")

    from comic_editor.core.assets import _translate_mask
    from comic_editor.ui.transform_modifier_preview import _RIG_TYPES, transform_modifier_rig
    roots = {identifier: _shifted_root(layer, delta)
             for identifier, layer in chapter.layers.items() if not layer.parent_id}
    transform = QTransform.fromTranslate(0, delta)
    rigs = {identifier: deepcopy(modifier) for identifier, modifier in chapter.modifiers.items()
            if isinstance(modifier, _RIG_TYPES)}
    masks = {identifier: deepcopy(mask) for identifier, mask in chapter.masks.items()}
    # Prepare every value before mutating the live document. Source stores are
    # never copied or edited; masks retain their original sparse paint grid.
    for modifier in rigs.values():
        transform_modifier_rig(canvas, modifier, transform)
    for mask in masks.values():
        _translate_mask(mask, 0, delta)
    before = RecordSnapshot.capture(chapter, layers=roots, modifiers=rigs, masks=masks,
        scalars=("size", "export_rect"),
        attributes={"layers": ("translate_y", "transform_quad")})
    for identifier, values in roots.items():
        vars(chapter.layers[identifier]).update(values)
    for identifier, modifier in rigs.items():
        vars(chapter.modifiers[identifier]).update(vars(modifier))
    for identifier, mask in masks.items():
        vars(chapter.masks[identifier]).update(vars(mask))
    if chapter.export_rect is not None:
        x, y, width, crop_height = chapter.export_rect
        chapter.export_rect = (x, y + delta, width, crop_height)
    chapter.height = height
    return commit_records(canvas, before, "Resize Canvas")
