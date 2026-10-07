"""Mask contact never enters exact spatial work on the presentation thread."""
from threading import get_ident

import pytest
from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QImage

from comic_editor.core.models import (
    DistortModifier, HueSaturationLightnessModifier, ImageObject,
    OutlineModifier, ParameterMaskBinding, ToneMask,
)
from comic_editor.render.service import RenderResult, RenderStatus
from comic_editor.ui.cache_dependencies import exact_cache_allowed
from comic_editor.ui.canvas import ToolKind
from test_navigator_patterns import source_image
from test_projection_invalidation import scene
from test_projection_interaction_preview import oracle, paint, warm


def mask_scene(canvas, group):
    obj = canvas.chapter.add_object(group.layer_id,
        ImageObject(x=700, y=40, pixel_width=640, pixel_height=240))
    canvas.images.put_decoded(obj.object_id, 'colors.png', b'', source_image())
    warp = DistortModifier(modifier_type='distort_twirl', frame=(700, 40, 640, 240),
        center=(1020, 160), radius=240, parameters={'angle': 35})
    warp.validate()
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    color = HueSaturationLightnessModifier(saturation=-100)
    color.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 0, 100)
    for modifier in (warp, color, OutlineModifier(thickness=4)):
        canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    canvas.set_selection('object', obj.object_id)
    canvas.set_tone_mask_mode(mask.mask_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    return obj, mask


def test_image_parameter_mask_contact_has_current_bounded_preview_and_exact_undo(scene, qapp, monkeypatch):
    from comic_editor.ui import distort_pipeline
    canvas, _raster, group = scene
    _obj, mask = mask_scene(canvas, group)
    before = paint(canvas, interactive=False)
    completed = canvas._projection_completed_view
    tile_keys = {key: tile.image.cacheKey() for key, tile in canvas._document_projection.tiles.items()}
    gui, draft_grids = get_ident(), []
    original = distort_pipeline.render_distort_stage

    def guarded(*args, **kwargs):
        if get_ident() == gui:
            assert not canvas._projection_exact, 'Mask contact evaluated native exact distortion on the GUI'
            assert not exact_cache_allowed(canvas, ('mask-contact',))
            draft_grids.append(args[1].width() * args[1].height())
        return original(*args, **kwargs)

    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', guarded)
    exact = canvas._render_document_tiles
    monkeypatch.setattr(canvas, '_render_document_tiles',
        lambda *_: pytest.fail('Mask contact entered exact tile collection'))
    canvas._begin_mask_stroke(QPointF(1000, 120), 1.)
    paint(canvas)
    first = canvas._projection_interaction_preview
    assert first is not None and first.coverage == canvas.visible_document_rect()
    assert first.revision == canvas._document_projection.revision
    assert first.tile.image.width() * first.tile.image.height() <= 1024 * 1024
    assert first.tile.image == oracle(canvas, first)
    assert canvas._projection_completed_view is completed
    assert canvas._projection_presented_revision == completed[2]
    assert canvas._projection_frame_pending and not canvas._effect_jobs.pending
    assert all(tile.image.cacheKey() == tile_keys[key] for key, tile in canvas._document_projection.tiles.items())
    heartbeat = []
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    qapp.processEvents()
    assert heartbeat
    canvas._continue_mask_stroke(QPointF(1120, 120), 1.)
    canvas._flush_mask_samples()
    paint(canvas)
    second = canvas._projection_interaction_preview
    assert second is not first and second.revision > first.revision
    assert second.tile.image != first.tile.image
    assert second.tile.image == oracle(canvas, second)
    assert draft_grids and max(draft_grids) <= 32768
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', original)
    monkeypatch.setattr(canvas, '_render_document_tiles', exact)
    canvas._end_mask_stroke()
    after = paint(canvas, interactive=False)
    assert after != before and not canvas._projection_frame_pending
    canvas._render_service.invalidate()
    assert paint(canvas, interactive=False) == after
    canvas.command_stack.undo()
    assert canvas.tiles.content_bounds(mask.mask_id) is None
    assert paint(canvas, interactive=False) == before


@pytest.mark.parametrize('status', [RenderStatus.PENDING, RenderStatus.FAILED])
def test_unavailable_mask_contact_draft_retains_coherent_frame_without_exact_fallback(scene, monkeypatch, status):
    canvas, _obj, group = scene
    mask_scene(canvas, group)
    completed = warm(canvas)
    canvas._begin_mask_stroke(QPointF(1000, 120), 1.)
    original = canvas._render_service.render_region

    def unavailable(document, request):
        assert not canvas._projection_exact
        return RenderResult(request, document, QImage(), status, 'Draft unavailable')

    monkeypatch.setattr(canvas._render_service, 'render_region', unavailable)
    monkeypatch.setattr(canvas, '_collect_document_projection',
        lambda *_: pytest.fail('Unavailable mask draft forced exact collection'))
    paint(canvas)
    assert canvas._projection_interaction_preview is None
    assert canvas._projection_completed_view is completed
    assert canvas._projection_presented_revision == completed[2]
    assert canvas._projection_frame_pending
    monkeypatch.setattr(canvas._render_service, 'render_region', original)
    assert canvas._capture_interaction_projection_preview(mask_contact=True)
    preview = canvas._projection_interaction_preview
    assert preview.revision == canvas._document_projection.revision
    assert preview.tile.image == oracle(canvas, preview)
    canvas._end_mask_stroke()
