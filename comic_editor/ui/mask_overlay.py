"""Latest viewport mask preparation, with resident editing feedback."""
from PySide6.QtCore import QObject, QRectF
from PySide6.QtGui import QImage, QPainter, QTransform

from comic_editor.render.mask_overlay import MaskOverlay, blue_mask_image, prepare_mask_overlay
from comic_editor.ui.scene_consumers import SceneConsumers


class MaskOverlayController(QObject):
    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        # Visible mask work has its own serial lane; cold probes cannot hold
        # up editing feedback. Memory admission remains shared with rendering.
        self.jobs = SceneConsumers(canvas)
        self.epoch = 0
        self.ready = self.ready_context = self.ready_key = None
        self.requested = None
        self.error = None
        self.hints, self.density = 0, 1.

    def context(self):
        canvas = self.canvas
        mask_id = canvas.active_tone_mask_id or canvas.preview_tone_mask_id
        mask = canvas.chapter.masks.get(mask_id) if canvas.chapter else None
        if mask is None:
            return None
        transform = canvas.camera_transform()
        matrix = tuple(getattr(transform, name)() for name in
                       ('m11', 'm12', 'm13', 'm21', 'm22', 'm23', 'm31', 'm32', 'm33'))
        visible = canvas.visible_document_rect()
        return (id(canvas.chapter), mask_id, max(1, canvas.width()), max(1, canvas.height()), matrix,
                (visible.x(), visible.y(), visible.width(), visible.height()), self.epoch,
                self.hints, self.density)

    def key(self, context):
        return context, self.canvas._render_document_state(), self.canvas._mask_runtime_revision

    def invalidate(self, contributors=True):
        if contributors:
            self.epoch += 1
            self.ready = self.ready_context = self.ready_key = None
            self.jobs.cancel(('mask-overlay',))
            self.requested = None
        else:
            self.refresh_resident()

    def refresh_resident(self):
        """Prepare changed positive paint tiles; never borrow a cold mapping."""
        canvas, context = self.canvas, self.context()
        if (self.ready is None or self.ready_context != context or self.ready.subtractions
                or self.ready.paint_frame is not None):
            return
        mask = canvas.chapter.masks[context[1]]
        if mask.paint_has_subtractions:
            return
        owner = canvas.tiles._tiles.get(mask.mask_id)
        if owner is None:
            paint = ()
        else:
            prepared = dict(self.ready.paint)
            for key in tuple(prepared):
                if key not in owner:
                    prepared.pop(key)
            # The stroke owns these keys and has already made their buffers
            # resident. Other ready paint tiles keep their worker-owned image.
            keys = getattr(canvas, '_mask_overlay_dirty_keys', set(canvas._stroke_before))
            for key in keys:
                if key not in owner:
                    prepared.pop(key, None)
                    continue
                resident = owner.residency.entries.get((owner, key))
                if resident is None:
                    return
                prepared[key] = blue_mask_image(canvas._image_alpha_array(resident[0]))
            canvas._mask_overlay_dirty_keys = set()
            paint = tuple(prepared.items())
        self.ready = MaskOverlay(self.ready.base, paint, False)
        self.ready_key = self.key(context)

    def draw(self, painter):
        self.hints = painter.renderHints().value
        self.density = painter.device().devicePixelRatioF()
        canvas, context = self.canvas, self.context()
        if context is None:
            self.invalidate()
            return
        key = self.key(context)
        if self.ready_key != key and self.requested != key:
            self.requested = key
            def valid():
                return self.context() == context and self.key(context) == key
            def accept(result, error):
                self.requested = None
                self.error = error
                if error is None:
                    self.ready, self.ready_context, self.ready_key = result, context, key
                    canvas._tone_mask_overlay_key = key
                canvas.update()
            def discard():
                if self.requested == key:
                    self.requested = None
                canvas.update()
            self.jobs.request(('mask-overlay',), prepare_mask_overlay, (*context[1:6], *context[7:]), accept,
                              valid=valid, discard=discard)
        if self.ready is None or self.ready_context != context:
            return
        painter.save()
        painter.resetTransform()
        painter.drawImage(0, 0, self.ready.base)
        if not self.ready.subtractions:
            painter.setCompositionMode(QPainter.CompositionMode_Plus)
            if self.ready.paint_frame is not None:
                painter.drawImage(0, 0, self.ready.paint_frame)
                painter.restore()
                return
            painter.setTransform(QTransform(*context[4]))
            painter.setClipRect(QRectF(0, 0, canvas.chapter.width, canvas.chapter.height))
            painter.translate(*canvas.chapter.masks[context[1]].paint_offset)
            for key, image in self.ready.paint:
                painter.drawImage(key[0] * canvas.tiles.tile_size, key[1] * canvas.tiles.tile_size, image)
        painter.restore()
