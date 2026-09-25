"""Exact stage caches depend on artwork, parameters and actual capture extents."""
from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.models import (
    ArrayModifier, BlurModifier, BoundGeometry, BrightnessContrastModifier, CageTransformModifier,
    ChapterDocument, CurvesModifier, DistortModifier, HalftoneModifier,
    HueSaturationLightnessModifier, MirrorModifier, OutlineModifier, ParameterMaskBinding,
    PixelateModifier, PosterizeModifier, RadialBlurModifier, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_pipeline import cached_stage_output, render_stages
from comic_editor.ui.modifier_rendering import _premultiplied_qimage, modifier_render_settings


@pytest.fixture
def scene(qapp, monkeypatch):
    chapter = ChapterDocument(width=320, height=240, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 320, 240))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(chapter, TileStore())
    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _: None)
    monkeypatch.setattr("comic_editor.ui.gpu_textures.renderer_for", lambda _: None)
    yield canvas
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def source_image(width=96, height=80):
    yy, xx = np.mgrid[:height, :width]
    pixels = np.stack((xx / width, yy / height, (xx % 7) / 7, np.ones_like(xx)), axis=-1)
    pixels[yy < 8] = 0
    return _premultiplied_qimage(pixels)


def distort():
    return DistortModifier(modifier_type="distort_twirl", parameters={"angle": 60},
                           frame=(-10, 20, 96, 80), center=(38, 60), radius=32)


FACTORIES = [
    lambda: BrightnessContrastModifier(brightness=10),
    lambda: CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .7), (1, 1)]}),
    lambda: HueSaturationLightnessModifier(hue=50),
    lambda: BlurModifier(strength=2),
    lambda: OutlineModifier(thickness=4),
    lambda: OutlineModifier(thickness=4, blur_radius=2, blur_strength=70),
    lambda: PixelateModifier(pixel_size=9),
    lambda: HalftoneModifier(blur=0),
    lambda: PosterizeModifier(),
    lambda: ArrayModifier(count=1, axis_end=(15, 0)),
    lambda: MirrorModifier(axis_start=(40, 0), axis_end=(40, 120)),
    lambda: RadialBlurModifier(angle=20, center=(38, 60)),
    lambda: CageTransformModifier(frame=(-10, 20, 96, 80)),
    distort,
]


@pytest.mark.parametrize("factory", FACTORIES)
def test_ui_metadata_and_larger_camera_request_reuse_exact_stage(scene, monkeypatch, factory):
    modifier = factory()
    scene.chapter.modifiers[modifier.modifier_id] = modifier
    source = source_image()
    bounds = QRectF(-10, 20, 96, 80)
    calls = []
    original_fields = scene._modifier_mask_fields
    def fields(*args, **kwargs):
        calls.append(1)
        return original_fields(*args, **kwargs)
    monkeypatch.setattr(scene, "_modifier_mask_fields", fields)
    first, placement = render_stages(scene, source, bounds, [modifier], QTransform(),
        required=QRectF(-200, -200, 600, 600), source_key=("unchanged-source",))
    assert len(calls) == 1
    modifier.name, modifier.expanded = "Renamed card", False
    if isinstance(modifier, CurvesModifier):
        modifier.channel = "red"
    scene._interactive_render = True
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *_a, **_k: pytest.fail("unchanged stage queued a worker"))
    before_revision = getattr(scene, "_effect_provisional_revision", 0)
    second, second_placement = render_stages(scene, source.copy(), bounds, [modifier], QTransform(),
        required=QRectF(-300, -250, 800, 700), request_scope=("object", "stable", "canvas"),
        source_key=("unchanged-source",))
    assert len(calls) == 1
    assert second == first and second_placement == placement
    assert getattr(scene, "_effect_provisional_revision", 0) == before_revision
    # An actual parameter edit in every rendering family still needs pixels.
    modifier.intensity = 51
    scene._interactive_render = False
    render_stages(scene, source, bounds, [modifier], QTransform(),
        required=QRectF(-300, -250, 800, 700), source_key=("unchanged-source",))
    assert len(calls) == 2


def test_real_upstream_parameter_and_mask_changes_invalidate_downstream(scene, monkeypatch):
    source, bounds = source_image(), QRectF(0, 0, 96, 80)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    first = BrightnessContrastModifier(brightness=10)
    second = CurvesModifier(curves={"rgb:red": [(0, 1), (1, 0)]})
    for modifier in (first, second):
        scene.chapter.modifiers[modifier.modifier_id] = modifier
    calls = []
    original_fields = scene._modifier_mask_fields
    def fields(*args, **kwargs):
        calls.append(1)
        return original_fields(*args, **kwargs)
    monkeypatch.setattr(scene, "_modifier_mask_fields", fields)
    def render():
        return render_stages(scene, source, bounds, [first, second], QTransform(),
                             source_key=("pixels",))[0]
    original = render()
    first.brightness = 30
    changed = render()
    assert len(calls) == 4 and changed != original
    first.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 0)
    masked = render()
    assert len(calls) == 6 and masked != changed
    first.parameter_masks["intensity"].black_value = 100
    assert render() != masked and len(calls) == 8


def test_cage_downstream_keys_keep_semantic_placement_after_source_recreation(scene, monkeypatch):
    source, bounds = source_image(), QRectF(-10, 20, 96, 80)
    modifiers = [CageTransformModifier(frame=(-10, 20, 96, 80)),
                 BrightnessContrastModifier(brightness=15), OutlineModifier(thickness=3)]
    for modifier in modifiers:
        scene.chapter.modifiers[modifier.modifier_id] = modifier
    calls = []
    original_fields = scene._modifier_mask_fields
    def fields(*args, **kwargs):
        calls.append(1)
        return original_fields(*args, **kwargs)
    monkeypatch.setattr(scene, "_modifier_mask_fields", fields)
    def render(image, required):
        return render_stages(scene, image, bounds, modifiers, QTransform(),
            required=required, source_key=("cage-input",))
    expected, expected_bounds = render(source, QRectF(-200, -200, 600, 600))
    assert len(calls) == 3
    modifiers[0].expanded = False
    actual, actual_bounds = render(source.copy(), QRectF(-300, -300, 800, 800))
    assert actual == expected and actual_bounds == expected_bounds and len(calls) == 3


def test_changed_output_region_never_returns_an_old_crop(scene):
    source, bounds, modifier = source_image(), QRectF(-10, 20, 96, 80), distort()
    scene.chapter.modifiers[modifier.modifier_id] = modifier
    full, full_bounds = render_stages(scene, source, bounds, [modifier], QTransform(),
                                      source_key=("same-source",))
    for required in (QRectF(5, 35, 40, 35), QRectF(30, 50, 40, 35)):
        cropped, placement = render_stages(scene, source, bounds, [modifier], QTransform(),
            required=required, source_key=("same-source",))
        offset = placement.topLeft() - full_bounds.topLeft()
        assert cropped == full.copy(int(offset.x()), int(offset.y()), cropped.width(), cropped.height())
    again, again_bounds = render_stages(scene, source, bounds, [modifier], QTransform(),
                                        source_key=("same-source",))
    assert again == full and again_bounds == full_bounds


def test_distort_draft_cannot_be_reused_as_exact_export(scene, monkeypatch):
    source, bounds = source_image(320, 240), QRectF(0, 0, 320, 240)
    modifier = DistortModifier(modifier_type="distort_twirl", parameters={"angle": 75},
                               frame=(0, 0, 320, 240), center=(160, 120), radius=100)
    scene.chapter.modifiers[modifier.modifier_id] = modifier
    requested = []
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *_a, **_k: requested.append(1) or True)
    scene._interactive_render = True
    draft, _ = render_stages(scene, source, bounds, [modifier], QTransform(),
        request_scope=("object", "draft", "canvas"), source_key=("stable",))
    assert requested and scene._effect_provisional_revision > 0
    scene._interactive_render = False
    exact, exact_bounds = render_stages(scene, source, bounds, [modifier], QTransform(), source_key=("stable",))
    assert exact != draft
    scene._interactive_render = True
    count = len(requested)
    revision = scene._effect_provisional_revision
    reused, reused_bounds = render_stages(scene, source.copy(), bounds, [modifier], QTransform(),
        request_scope=("object", "draft", "canvas"), source_key=("stable",))
    assert reused == exact and reused_bounds == exact_bounds
    assert len(requested) == count and scene._effect_provisional_revision == revision


def test_render_settings_keep_all_actual_curve_and_mask_dependencies():
    modifier = CurvesModifier(curves={"rgb:red": [(0, 1), (1, 0)]})
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("mask", 0, 80)
    before = modifier_render_settings(modifier)
    changed = deepcopy(modifier)
    changed.name, changed.expanded, changed.channel = "Other", False, "red"
    assert modifier_render_settings(changed) == before
    for field, value in (("intensity", 50), ("input_max", 2), ("color_mode", "lab"),
                         ("blend_mode", "multiply"), ("muted", True)):
        changed = deepcopy(modifier)
        setattr(changed, field, value)
        assert modifier_render_settings(changed) != before
    changed = deepcopy(modifier)
    changed.parameter_masks["intensity"].white_value = 60
    assert modifier_render_settings(changed) != before


def completed_array_curves(scene, monkeypatch):
    """Render a spatial/color stack, retaining exact pixels outside ordinary LRUs."""
    modifiers = [ArrayModifier(count=1, axis_end=(20, 0)),
                 CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .7), (1, 1)]})]
    for modifier in modifiers:
        scene.chapter.modifiers[modifier.modifier_id] = modifier
    scene._interactive_render = True
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *_a, **_k: False)
    arguments = dict(request_scope=("object", "retained", "canvas"),
                     source_key=("stable-source",), required=QRectF(-100, -100, 400, 400))
    bounds = QRectF(0, 0, 96, 80)
    expected = render_stages(scene, source_image(), bounds, modifiers, QTransform(), **arguments)
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    scene._modifier_source_cache.clear()
    scene._modifier_source_cache_bytes = 0
    return modifiers, bounds, arguments, expected


def test_completed_stage_lookup_reuses_pixels_before_source_capture(scene, monkeypatch):
    modifiers, bounds, arguments, expected = completed_array_curves(scene, monkeypatch)
    monkeypatch.setattr(scene, "_modifier_mask_fields", lambda *_a, **_k: pytest.fail("lookup sampled pixels"))
    modifiers[0].expanded, modifiers[1].channel = False, "red"
    # Different camera requests with the same concrete stage extents share the
    # exact same plan as render_stages; no second key implementation is needed.
    arguments["required"] = QRectF(-200, -200, 600, 600)
    cached = cached_stage_output(scene, bounds, modifiers, QTransform(), **arguments)
    assert cached is not None
    assert cached[0] == expected[0] and cached[1] == expected[1]


@pytest.mark.parametrize("changed", ["upstream_parameter", "upstream_mask", "crop", "source", "mapping", "bounds"])
def test_completed_stage_lookup_rejects_changed_dependencies(scene, monkeypatch, changed):
    modifiers, bounds, arguments, _expected = completed_array_curves(scene, monkeypatch)
    mapping = QTransform()
    if changed == "upstream_parameter":
        modifiers[0].count = 2
    elif changed == "upstream_mask":
        mask = ToneMask()
        scene.chapter.masks[mask.mask_id] = mask
        modifiers[0].parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 50)
    elif changed == "crop":
        arguments["required"] = QRectF(20, 10, 40, 30)
    elif changed == "source":
        arguments["source_key"] = ("changed-source",)
    elif changed == "mapping":
        mapping.scale(2, 2)
    else:
        bounds = bounds.adjusted(-1, -1, 1, 1)
    assert cached_stage_output(scene, bounds, modifiers, mapping, **arguments) is None


@pytest.mark.parametrize("context", ["export", "navigator", "base_alpha", "mask_contributor", "missing_source"])
def test_completed_stage_lookup_rejects_special_capture_contexts(scene, monkeypatch, context):
    modifiers, bounds, arguments, _expected = completed_array_curves(scene, monkeypatch)
    if context == "export":
        scene._interactive_render = False
    elif context == "navigator":
        scene._effect_preview_channel = "navigator"
    elif context == "base_alpha":
        scene._render_base_alpha = True
    elif context == "mask_contributor":
        scene._rendering_mask_contributor = 1
    else:
        arguments["source_key"] = None
    assert cached_stage_output(scene, bounds, modifiers, QTransform(), **arguments) is None


def test_completed_stage_lookup_never_returns_a_partial_checkpoint(scene, monkeypatch):
    modifiers = [ArrayModifier(count=1, axis_end=(20, 0)),
                 CurvesModifier(curves={"rgb:red": [(0, 1), (1, 0)]})]
    for modifier in modifiers:
        scene.chapter.modifiers[modifier.modifier_id] = modifier
    scene._interactive_render = True
    requests = []
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *_a, **_k: requests.append(1) or True)
    arguments = dict(request_scope=("object", "partial", "canvas"), source_key=("stable-source",))
    bounds = QRectF(0, 0, 240, 160)
    render_stages(scene, source_image(240, 160), bounds, modifiers, QTransform(), **arguments)
    assert requests and scene._effect_provisional_revision > 0
    checkpoint = scene._effect_jobs.retained[("pipeline", arguments["request_scope"])]
    assert checkpoint[2][0] == 1  # Array exact; Curves still a draft.
    assert cached_stage_output(scene, bounds, modifiers, QTransform(), **arguments) is None


@pytest.mark.parametrize("trailing", [CurvesModifier(muted=True),
    BrightnessContrastModifier(intensity=0), RadialBlurModifier(angle=0)])
def test_completed_stage_lookup_accepts_trailing_skipped_stages(scene, monkeypatch, trailing):
    modifiers, bounds, arguments, _expected = completed_array_curves(scene, monkeypatch)
    modifiers.append(deepcopy(trailing))
    scene.chapter.modifiers[modifiers[-1].modifier_id] = modifiers[-1]
    expected = render_stages(scene, source_image(), bounds, modifiers, QTransform(), **arguments)
    cached = cached_stage_output(scene, bounds, modifiers, QTransform(), **arguments)
    assert cached is not None
    assert cached[0] == expected[0] and cached[1] == expected[1]
