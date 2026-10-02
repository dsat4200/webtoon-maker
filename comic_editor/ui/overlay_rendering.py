"""Ordered shape overlays with a separately preserved owner outline."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QTransform

from comic_editor.core.models import SolidColorOverlayModifier, TextureModifier, StrokeModifier, DotDashModifier
from comic_editor.ui.effect_pipeline import render_stages, empty_image, aligned
from comic_editor.ui.modifier_rendering import modifier_render_settings
from comic_editor.ui.stroke_rendering import placed, target_loops, transformed_loops, mask_parameters, warp_material, opacity_noise, apply_dots


def paint_shape_outline(canvas, painter, layer):
    """Use the same mesh and paint helpers as ordinary shape composition."""
    if not canvas._solo_content_visible("layer", layer.layer_id) or layer.border_width <= 0:
        return
    painter.save()
    painter.setTransform(canvas._layer_parent_transform(layer), True)
    path = (canvas.layer_effective_path(layer.layer_id) if layer.compound_enabled
            else canvas._layer_operand_path(layer))
    tolerance = canvas._outline_tolerance(painter, path.controlPointRect())
    if layer.layer_kind == "open_shape":
        from comic_editor.ui.shape_outline import outline_mesh
        style = layer.shape_style
        core = canvas.open_shape_mesh(layer.bound, style.base_thickness, 0, style.start_cap,
                                      style.end_cap, cache=canvas._outline_cache, tolerance=tolerance)
        ring = outline_mesh(layer.bound, style.outline_thickness, path,
                            core=core, base_width=style.base_thickness, cache=canvas._outline_cache,
                            tolerance=tolerance, start_cap=style.start_cap, end_cap=style.end_cap)
        painter.fillPath(ring, QColor(style.outline_color))
    elif layer.compound_enabled:
        from comic_editor.ui.compound_strokes import paint_outline
        sources = canvas._compound_outline_mesh(layer, path, tolerance, sources_only=True)
        paint_outline(canvas, painter, layer, path, sources, tolerance)
    else:
        from comic_editor.ui.compound_strokes import appearance, paint_outline
        from comic_editor.ui.shape_outline_compound import OutlineSource
        styled = appearance(canvas, layer)
        painter.setClipPath(path, Qt.IntersectClip)
        if styled is not None:
            paint_outline(canvas, painter, layer, path,
                          [OutlineSource(styled.bound, layer.border_width, QTransform(), owner_id=layer.layer_id)], tolerance)
        else:
            from comic_editor.ui.compound_outline_painting import paint_closed_shape_outline
            paint_closed_shape_outline(painter, layer.bound, layer.border_width, path,
                                       QColor(layer.border_color), cache=canvas._outline_cache, tolerance=tolerance)
    painter.restore()


def shape_overlay_stack(canvas, target, image, bounds, modifiers, mapping, source_key, scope,
                        *, provisional=False, tiled=False):
    from comic_editor.core.effect_geometry import effect_bounds
    final_bounds = QRectF(bounds)
    for modifier in modifiers:
        if not modifier.muted:
            final_bounds = aligned(effect_bounds(final_bounds, [modifier], mapping))
    key = ("shape-overlay-stack", source_key, tuple(repr(modifier_render_settings(m)) for m in modifiers),
           canvas._modifier_parameter_signature(target.modifier_ids), tuple(bounds.getRect()),
           canvas._modifier_mapping_signature(mapping))
    cached = None if provisional else canvas._modifier_cache_get(key)
    if cached is not None:
        return cached, final_bounds
    outline = empty_image(bounds)
    painter = QPainter(outline)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(-bounds.x(), -bounds.y())
    try:
        if tiled:
            canvas._tiling_shape_style(painter, target, outline=True)
        else:
            paint_shape_outline(canvas, painter, target)
    finally:
        painter.end()
    fill_key = ("overlay-fill-source", source_key, tiled)
    background = None if provisional else canvas._modifier_source_cache_get(fill_key)
    if background is None:
        background = empty_image(bounds)
        painter = QPainter(background)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(-bounds.x(), -bounds.y())
        previous = getattr(canvas, "_stroke_hide_border_id", None)
        canvas._stroke_hide_border_id = target.layer_id
        canvas._render_modifier_sources.add(("layer", target.layer_id))
        revision = getattr(canvas, "_effect_provisional_revision", 0)
        try:
            if tiled:
                captured, captured_bounds = canvas._tiling_stage(target)
                painter.drawImage(captured_bounds.topLeft(), captured)
            else:
                canvas._render_layer(painter, target, 1., mapping.mapRect(bounds))
        finally:
            canvas._stroke_hide_border_id = previous
            canvas._render_modifier_sources.discard(("layer", target.layer_id))
            painter.end()
        provisional |= revision != getattr(canvas, "_effect_provisional_revision", 0)
        if not provisional:
            canvas._modifier_source_cache_put(fill_key, background)
    loops = target_loops(canvas, target) if any(isinstance(m, StrokeModifier) for m in modifiers) else []
    if tiled:
        parent_mapping = canvas.layer_world_transform(target.parent_id) if target.parent_id else QTransform()
        loops = [loop.mapped(parent_mapping) for loop in loops]
    for index, modifier in enumerate(modifiers):
        if modifier.muted or modifier.intensity <= 0 and "intensity" not in modifier.parameter_masks:
            continue
        if isinstance(modifier, TextureModifier) and not modifier.texture_data:
            continue
        if isinstance(modifier, StrokeModifier):
            # Stroke stages transport each channel together before subsequent
            # overlays, so preserved outlines follow deformed/dotted contours.
            if not loops:
                continue
            from comic_editor.core.stroke_geometry import deform_loop
            from comic_editor.core.effect_geometry import effect_bounds
            expanded = aligned(effect_bounds(bounds, [modifier], mapping))
            image, background, outline = [placed(part, bounds, expanded) for part in (image, background, outline)]
            bounds = expanded
            parameters = mask_parameters(canvas, modifier, loops, bounds, mapping)
            blank = empty_image(bounds)
            if isinstance(modifier, DotDashModifier):
                image = apply_dots(image, background, bounds, loops, modifier, parameters)
                outline = apply_dots(outline, blank, bounds, loops, modifier, parameters)
            else:
                changed = [deform_loop(loop, modifier, parameter) for loop, parameter in zip(loops, parameters)]
                moved, opacity = [result[0] for result in changed], [result[1] for result in changed]
                image, background = warp_material(image, background, bounds, loops, moved)
                outline, _ = warp_material(outline, blank, bounds, loops, moved)
                image = opacity_noise(image, background, bounds, moved, opacity)
                outline = opacity_noise(outline, blank, bounds, moved, opacity)
                loops = moved
            continue
        old_bounds = QRectF(bounds)
        revision = getattr(canvas, "_effect_provisional_revision", 0)
        parts = [("fill", background)]
        excludes_outline = isinstance(modifier, SolidColorOverlayModifier) and not modifier.apply_to_outline
        if not excludes_outline:
            parts += [("material", image), ("outline", outline)]
        outputs = {}
        prefix = (source_key, tuple(repr(modifier_render_settings(m)) for m in modifiers[:index]),
                  canvas._modifier_parameter_signature([m.modifier_id for m in modifiers[:index]]))
        for channel, part in parts:
            result, output_bounds = render_stages(canvas, part, old_bounds, [modifier], mapping,
                request_scope=(*scope, "overlay-channel", channel, modifier.modifier_id) if scope is not None else None,
                provisional=provisional, source_key=("overlay-channel", channel, prefix))
            outputs[channel] = result, output_bounds
        background, bounds = outputs["fill"]
        if excludes_outline:
            outline = placed(outline, old_bounds, bounds)
            image = background.copy()
            painter = QPainter(image)
            painter.drawImage(0, 0, outline)
            painter.end()
        else:
            image = placed(*outputs["material"], bounds)
            outline = placed(*outputs["outline"], bounds)
        loops = transformed_loops(loops, modifier, mapping)
        provisional |= revision != getattr(canvas, "_effect_provisional_revision", 0)
    if not provisional:
        canvas._modifier_cache_put(key, image)
    return image, bounds
