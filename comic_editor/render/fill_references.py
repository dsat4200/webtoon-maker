"""Native fill-reference evaluation on detached sources and ordinary kernels."""
from dataclasses import replace

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from comic_editor.core.models import LayerNode
from comic_editor.render.admission import RENDER_ADMISSION, WorkCancelled, snapshot_working_bytes
from comic_editor.render.effect_pipeline import aligned
from comic_editor.render.pixels import pixel_scope
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.ui.point_lut import graphics_scope


def render_reference_entity(scene, painter, kind, identifier, visible_world):
    """Use the same hierarchy transforms and kernels as document artwork."""
    if kind == 'layer':
        layer = scene.chapter.layers[identifier]
        parent = scene.layer_world_transform(layer.parent_id) if layer.parent_id else QTransform()
        inverse, valid = parent.inverted()
        painter.save()
        painter.setTransform(parent, True)
        scene._render_layer(painter, layer, 1., inverse.mapRect(visible_world) if valid else visible_world)
        painter.restore()
        return
    obj = scene.chapter.objects[identifier]
    parent = scene.layer_world_transform(obj.parent_layer_id)
    inverse, valid = parent.inverted()
    opacity = 1.
    for ancestor in scene.chapter.ancestor_layers(obj.parent_layer_id):
        opacity *= ancestor.opacity
    painter.save()
    painter.setTransform(parent, True)
    scene._render_object(painter, obj, opacity, inverse.mapRect(visible_world) if valid else visible_world)
    painter.restore()


def reference_tile(scene, target, key, entities, profile):
    if entities == [('object', target.object_id)]:
        return scene.tiles.tile(target.object_id, key)
    size = scene.tiles.tile_size
    # Fill classifiers consume the existing premultiplied byte-reference
    # contract. Artwork kernels retain their native source/effect precision.
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    local_to_world = scene._drawing_local_to_world_transform(target)
    inverse, valid = local_to_world.inverted()
    if not valid:
        return image
    shift = QTransform()
    shift.translate(-key[0]*size, -key[1]*size)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.setTransform(inverse*shift)
    visible_world = local_to_world.mapRect(QRectF(key[0]*size, key[1]*size, size, size))
    tiling = scene._drawing_tiling(target) if profile.get('_tiling_context') else None
    try:
        for kind, identifier in entities:
            if tiling and isinstance(tiling[0], LayerNode) and kind == 'layer':
                owner = tiling[0]
                if any(layer.layer_id == identifier for layer in scene.chapter.ancestor_layers(owner.layer_id)):
                    bounds = aligned(visible_world)
                    painter.drawImage(bounds.topLeft(), scene._tiling_source(owner, bounds))
                    continue
            render_reference_entity(scene, painter, kind, identifier, visible_world)
    finally:
        painter.end()
    return image


def populate_references(snapshot, target_id, entities, keys, profile, captured, cancelled):
    """Worker-only evaluation; missing keys include the morphology halo."""
    missing = [key for key in keys if key not in captured]
    if not missing:
        return captured
    estimate = snapshot_working_bytes(snapshot) + len(missing)*snapshot.tiles.tile_size**2*4
    with RENDER_ADMISSION.reserve('fill-reference', estimate, priority=1,
                                  cancelled=cancelled.is_set):
        state = dict(snapshot.state, _render_exclude_text=True, _solo_suspended=True,
                     _render_base_alpha=False, _show_on_top_phase=None)
        backend = DetachedSceneBackend(replace(snapshot, state=state))
        scene = backend.scene
        scene._interactive_render = False
        scene._exact_reference_render = True
        try:
            with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment), \
                    graphics_scope(snapshot.graphics_worker):
                target = scene.chapter.objects[target_id]
                for key in missing:
                    if cancelled.is_set():
                        raise WorkCancelled()
                    image = reference_tile(scene, target, key, entities, profile)
                    if image is None or image.isNull():
                        captured[key] = QImage()
                    else:
                        rgba = image.convertToFormat(QImage.Format_RGBA8888)
                        alpha = np.frombuffer(rgba.constBits(), dtype=np.uint8).reshape(
                            rgba.height(), rgba.bytesPerLine())[:, 3:rgba.width()*4:4]
                        captured[key] = QImage(image) if np.any(alpha) else QImage()
        finally:
            backend.close()
    return captured
