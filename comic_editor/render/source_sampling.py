"""Detached consumers for existing isolated modifier-input sampling kernels."""
import copy

from comic_editor.render.pixels import pixel_scope
from comic_editor.render.scene import DetachedSceneBackend


def input_backend(snapshot):
    backend = DetachedSceneBackend(snapshot)
    chapter = copy.copy(snapshot.chapter)
    # Isolated input probes temporarily hide stages and owner opacity. Their
    # records must be private even when another consumer shares the snapshot.
    chapter.layers = {identifier: copy.copy(record) for identifier, record in chapter.layers.items()}
    chapter.objects = {identifier: copy.copy(record) for identifier, record in chapter.objects.items()}
    backend.scene.chapter = chapter
    backend.scene._solo_suspended = True
    backend.scene._interactive_render = False
    backend.scene._exact_reference_render = True
    backend.scene._effect_region_requests = False
    return backend


def curves_histogram(snapshot, modifier_id, color_mode, channel, targets=None):
    from comic_editor.ui.curves_features import CurvesSampler
    backend = input_backend(snapshot)
    if targets is not None:
        backend.scene.selected_entities = list(targets)
        if targets:
            backend.scene.selected_kind, backend.scene.selected_id = targets[-1]
    try:
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            return CurvesSampler().histogram(backend.scene, modifier_id, color_mode, channel)
    finally:
        backend.close()


def curves_pixel(snapshot, modifier_id, world, targets):
    from PySide6.QtCore import QPointF
    from comic_editor.ui.curves_features import CurvesSampler
    backend = input_backend(snapshot)
    backend.scene.selected_entities = list(targets)
    if targets:
        backend.scene.selected_kind, backend.scene.selected_id = targets[-1]
    try:
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            return CurvesSampler().pixel(backend.scene, modifier_id, QPointF(*world))
    finally:
        backend.close()


def posterize_statistics(snapshot, targets, before_id, value_mode=False):
    from comic_editor.ui.posterize_controls import PosterizeSampler
    backend = input_backend(snapshot)
    try:
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            return PosterizeSampler(value_mode).sample(backend.scene, targets, before_id)
    finally:
        backend.close()


def eyedropper_region(snapshot, x, y, size):
    from comic_editor.ui.eyedropper_sampling import render_sample_region
    from comic_editor.render.pixels import display_image
    backend = input_backend(snapshot)
    backend.scene._solo_suspended = snapshot.state.get('_solo_suspended', False)
    try:
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            return display_image(render_sample_region(backend.scene, x, y, size), snapshot.document.pixel_contract)
    finally:
        backend.close()


def distort_beneath(snapshot, modifier_id):
    from comic_editor.ui.distort_sources import capture_distort_beneath
    backend = input_backend(snapshot)
    try:
        return capture_distort_beneath(backend.scene, modifier_id)
    finally:
        backend.close()


def _distort_workspace(snapshot, modifier_id):
    frame = snapshot.chapter.modifiers[modifier_id].frame
    return max(1, int(frame[2]))*max(1, int(frame[3]))*64


distort_beneath.working_bytes = _distort_workspace


def tone_mask_thumbnails(snapshot, mask_ids, width=80, height=80):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QTransform
    import numpy as np
    backend = input_backend(snapshot)
    scene = backend.scene
    transform = QTransform()
    transform.scale(width / max(1., scene.chapter.width), height / max(1., scene.chapter.height))
    visible = QRectF(0, 0, scene.chapter.width, scene.chapter.height)
    try:
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            result = {}
            for mask_id in mask_ids:
                if mask_id not in scene.chapter.masks:
                    continue
                field = scene.render_tone_mask_field(mask_id, width, height, transform, visible)
                values = np.ascontiguousarray(np.clip(field * 255., 0, 255).astype(np.uint8))
                result[mask_id] = QImage(values.data, width, height, width, QImage.Format_Grayscale8).copy()
            return result
    finally:
        backend.close()


def owned_object_tiles(snapshot, object_id):
    snapshot.finish_sources()
    images = snapshot.tiles.object_tiles(object_id)
    return images, {key: snapshot.tiles._alpha_bbox(image) for key, image in images.items()}
