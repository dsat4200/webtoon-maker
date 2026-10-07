"""Conservative device output for a complete, native, isolated source stack.

The scene owns the same immutable document/source identity as normal evaluation.
This storage route does not alter semantic requests, quality or invalidation.
Every unsupported composition or sampling operation returns to the shared scene
kernels. In particular, outer capture gutters still use their normal renderer.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import BlurModifier, CurvesModifier, ImageObject, RasterObject
from comic_editor.render.device import DeviceImage
from comic_editor.render.pixels import color_environment, current_contract
from comic_editor.render.service import RenderQuality


def _integer_rect(rect):
    values = tuple(rect.getRect())
    return all(math.isfinite(v) and v == round(v) for v in values)


def try_device_region(scene, document, request):
    """Return an exact shared texture view, or decline without changing output.

    Image generic stacks retain float point intermediates and their complete
    expanded blur frame. Raster point stacks retain byte boundaries on their
    original sparse grid. Raster blur uses the existing regional stage graph.
    """
    worker = getattr(scene, "_graphics_worker", None)
    if (worker is None or worker.closed or not worker.available or not worker.ready.is_set()
            or worker.share_context is None or current_contract().floating
            or request.quality is not RenderQuality.EXACT or request.scale != 1.
            or request.output_transform is not None or request.target is not None
            or not request.clip_document or request.requested.isEmpty()
            or not request.requested.intersects(request.bounds)
            or getattr(scene,'_effect_preview_channel','canvas') != 'canvas'
            or request.pixel_size != (round(request.region[2]), round(request.region[3]))
            or not _integer_rect(request.bounds) or document.live_preview
            or document.overflow or document.underlay[1] or QColor(document.background).alpha()
            or request.phase == "top" or getattr(scene.settings, "canvas_renderer", "auto") == "raster"):
        return None
    # These values change source visibility, capture geometry or live pixels.
    for name in ("_solo_entities", "_mask_wand_sample_entities", "_cage_session",
                 "_multi_transform_preview_quads", "_transform_preview_quad",
                 "_selection_overlay_tiles", "_selection_transform_quad",
                 "_vector_eraser_preview", "_vector_samples", "_vector_sweep",
                 "_overlay_color_preview", "_page_gap_draft", "_render_modifier_sources",
                 "_tiling_capture_geometry", "_geometry_transform_target",
                 "_render_base_alpha", "_render_cage_source", "_rendering_mask_contributor",
                 "_render_excluded_object_id"):
        if getattr(scene, name, None):
            return None
    chapter = scene.chapter
    if len(chapter.root_page_ids) != 1 or len(chapter.layers) != 1 or len(chapter.objects) != 1:
        return None
    page = chapter.layers[chapter.root_page_ids[0]]
    if (not page.is_page or not page.visible or page.opacity != 1. or page.parent_id
            or page.mask_only or page.show_on_top or page.compound_enabled
            or page.modifier_ids or page.opacity_mask or page.layer_kind != "bounded"
            or page.translate_x or page.translate_y or page.transform_quad or page.transform_frame
            or page.shape_style.primary_color or page.shape_style.outline_thickness
            or page.bound is None or page.bound.primitive != "rectangle"
            or not page.bound.closed or page.bound.additional_contours
            or len(page.children) != 1 or page.children[0].kind != "object"
            or not _full_rectangle(page.bound, document.bounds)):
        return None
    obj = chapter.objects[page.children[0].entity_id]
    if (not isinstance(obj, (ImageObject, RasterObject)) or not obj.visible
            or obj.parent_layer_id != page.layer_id or obj.opacity != 1.
            or obj.mask_only or obj.show_on_top or obj.blend_mode != "normal"
            or obj.opacity_mask or obj.underlay_opacity or obj.geometry_reference != "direct"
            or obj.transform_quad or obj.transform_frame or obj.x != round(obj.x)
            or obj.y != round(obj.y)):
        return None
    modifiers = scene._active_modifier_instances(obj.modifier_ids)
    from comic_editor.ui.point_lut import _supported
    if (not modifiers or any(not (_supported(m) or isinstance(m, BlurModifier)
            and m.mode == "full" and not m.parameter_masks and m.intensity == 100.) for m in modifiers)):
        return None
    if isinstance(obj, ImageObject):
        if obj.placement_mode != "free" or obj.is_blender_linked:
            return None
        image = scene.images.image(obj.object_id, contract=current_contract())
        if image.isNull() or image.width() != obj.pixel_width or image.height() != obj.pixel_height:
            return None
        frame = QRectF(obj.x, obj.y, obj.pixel_width, obj.pixel_height)
        padding = sum(m.strength * 3. for m in modifiers if isinstance(m, BlurModifier))
        frame = frame.adjusted(-padding, -padding, padding, padding).toAlignedRect()
        source_identity = scene.images.pixel_signature(obj.object_id)
        spatial = False
        # Capture exactly the generic stack's native source frame. Integer
        # translation is a copy; QPainter is a genuine source assembly consumer.
        if padding:
            padded = QImage(frame.size(), current_contract().image_format)
            padded.fill(Qt.transparent)
            painter = QPainter(padded)
            painter.drawImage(round(obj.x-frame.x()), round(obj.y-frame.y()), image)
            painter.end()
            image = padded
    else:
        # Curves dispatch Raster through render_stages; an ordinary generic
        # raster point stack has the generic stack's float boundary instead.
        if obj.modifier_source_frame is not None or any(isinstance(m, BlurModifier) for m in modifiers):
            return None
        local = scene.tiles.content_bounds(obj.object_id)
        if local is None or local.isEmpty() or not _integer_rect(local):
            return None
        frame = local.toAlignedRect().translated(round(obj.x), round(obj.y))
        image = QImage(frame.size(), current_contract().image_format)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        try:
            for (x,y), tile in scene.tiles.iter_tiles(obj.object_id, local):
                painter.drawImage(x*obj.tile_size-round(local.x()), y*obj.tile_size-round(local.y()),
                                  scene._working_raster_tile(tile))
        finally:
            painter.end()
        source_identity = scene.tiles.object_signature(obj.object_id)
        spatial = any(isinstance(m, CurvesModifier) for m in modifiers)
    # Chapter/page clipping is a no-op for this requested native region. A view
    # cannot manufacture transparent padding outside its source, so decline.
    if not document.bounds.contains(request.bounds) or not QRectF(frame).contains(request.bounds):
        return None
    from comic_editor.ui.gpu_effects import resident_stack
    identity = ("scene-source", document.identity, obj.object_id, source_identity,
                tuple(frame.getRect()), current_contract().signature,color_environment(current_contract()))
    result = resident_stack(image, modifiers, worker=worker, source_key=identity, quantize_stages=spatial)
    if not isinstance(result, DeviceImage) or result.isNull():
        return None
    return result.copy(QRect(round(request.bounds.x()-frame.x()), round(request.bounds.y()-frame.y()),
                             *request.pixel_size))


def _full_rectangle(bound, rect):
    points = tuple(node.position for node in bound.nodes)
    expected = tuple((p.x(), p.y()) for p in
                     (rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()))
    return (points == expected and all(node.point_type == "vector" and not node.roundness_enabled
                                      for node in bound.nodes))
