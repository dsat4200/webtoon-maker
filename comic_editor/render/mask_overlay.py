"""Prepared presentation of the common scalar mask, outside widget paint."""
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QTransform, QPainter


NATIVE_TILE_LIMIT = 128


def blue_mask_image(values):
    alpha = np.ascontiguousarray(np.clip(values, 0., 1.) * (0.35 * 255.), dtype=np.uint8)
    height, width = alpha.shape
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[..., :3] = (0x64, 0xB5, 0xF6)
    rgba[..., 3] = alpha
    return QImage(rgba.data, width, height, width * 4, QImage.Format_RGBA8888).copy().convertToFormat(
        QImage.Format_ARGB32_Premultiplied)


@dataclass(frozen=True)
class MaskOverlay:
    base: QImage
    paint: tuple
    subtractions: bool
    paint_frame: QImage | None = None


def image_alpha_array(image):
    rgba = image.convertToFormat(QImage.Format_RGBA8888)
    rows = np.frombuffer(rgba.constBits(), dtype=np.uint8).reshape(rgba.height(), rgba.bytesPerLine())
    return rows[:, :rgba.width()*4].reshape(rgba.height(), rgba.width(), 4)[..., 3].astype(np.float32) / 255.


def prepare_mask_overlay(snapshot, mask_id, width, height, matrix, visible, hints=0, density=1.):
    from comic_editor.render.scene import DetachedSceneBackend
    from comic_editor.render.pixels import pixel_scope
    from comic_editor.ui.attached_translation import effective_preview_mask
    backend = DetachedSceneBackend(snapshot)
    scene = backend.scene
    try:
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            mask = effective_preview_mask(scene, scene.chapter.masks[mask_id])
            transform, region = QTransform(*matrix), QRectF(*visible)
            field = scene.render_tone_mask_field(mask_id, width, height, transform, region,
                                                 include_paint=mask.paint_has_subtractions)
            paint = ()
            paint_frame = None
            if not mask.paint_has_subtractions:
                local = region.intersected(QRectF(0, 0, scene.chapter.width, scene.chapter.height)).translated(
                    -mask.paint_offset[0], -mask.paint_offset[1])
                owner = scene.tiles._tiles.get(mask_id)
                keys = scene.tiles.keys_for_rect(local)
                count = sum(key in owner for key in keys) if owner is not None else 0
                if count <= NATIVE_TILE_LIMIT:
                    paint = tuple((key, blue_mask_image(image_alpha_array(tile)))
                                  for key, tile in scene.tiles.iter_tiles(mask_id, local))
                else:
                    # Addition is associative in premultiplied display bytes.
                    # Fold a large zoomed-out paint set into one presentation
                    # surface, retaining the original source sampling grids.
                    paint_frame = QImage(max(1, round(width*density)), max(1, round(height*density)),
                                         QImage.Format_ARGB32_Premultiplied)
                    paint_frame.setDevicePixelRatio(density)
                    paint_frame.fill(Qt.transparent)
                    painter = QPainter(paint_frame)
                    try:
                        painter.setRenderHints(QPainter.RenderHint(hints))
                        painter.setCompositionMode(QPainter.CompositionMode_Plus)
                        painter.setTransform(transform)
                        painter.setClipRect(QRectF(0, 0, scene.chapter.width, scene.chapter.height))
                        painter.translate(*mask.paint_offset)
                        for key, tile in scene.tiles.iter_tiles(mask_id, local):
                            painter.drawImage(key[0]*scene.tiles.tile_size, key[1]*scene.tiles.tile_size,
                                              blue_mask_image(image_alpha_array(tile)))
                    finally:
                        painter.end()
            return MaskOverlay(blue_mask_image(field), paint, mask.paint_has_subtractions, paint_frame)
    finally:
        backend.close()


prepare_mask_overlay.admission_priority = 0
prepare_mask_overlay.working_bytes = lambda _snapshot, _mask, width, height, _matrix, _visible, _hints=0, density=1.: width * height * (28+4*density*density)
