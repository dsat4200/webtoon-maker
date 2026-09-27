"""Canonical mesh output keeps full-stage pixels and shares bounded storage."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.effect_geometry import effect_bounds
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, DistortModifier, KuwaharaModifier,
    ParameterMaskBinding, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.distort_regions import render_mesh_regions
from comic_editor.ui.effect_pipeline import _stage_plan, aligned, render_stages
from comic_editor.ui.modifier_rendering import _premultiplied_qimage, modifier_render_settings


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    chapter = ChapterDocument(width=800, height=600, document_kind="asset")
    chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 800, 600))
    canvas.set_document(chapter, TileStore())
    canvas._projection_exact = True
    yield canvas
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def source_image(width=331, height=273):
    yy, xx = np.mgrid[:height, :width]
    alpha = ((xx + yy) % 193) / 192.
    pixels = np.stack((xx / width, yy / height, (xx % 7) / 7, np.ones_like(xx)), axis=-1)
    return _premultiplied_qimage(pixels * alpha[..., None])


def mesh(scene, bounds, mapping, interpolation="bilinear", edges="transparent"):
    modifier = DistortModifier(modifier_type="distort_mesh_warp",
        frame=mapping.mapRect(bounds).getRect(),
        points=[(-.09, -.04), (.49, .06), (1.04, -.03),
                (-.04, .53), (.55, .59), (1.08, .48),
                (.03, 1.06), (.52, .98), (1.02, 1.09)],
        source_points=[(x / 2, y / 2) for y in range(3) for x in range(3)],
        parameters={"rows": 3, "columns": 3, "smoothness": 20.,
                    "interpolation": interpolation, "edges": edges})
    modifier.validate()
    scene.chapter.modifiers[modifier.modifier_id] = modifier
    return modifier


def stage_key(scene, bounds, target, modifier, mapping, revision=0):
    return ("stage", ("source", revision), scene._rect_signature(bounds),
            scene._rect_signature(target), repr(modifier_render_settings(modifier)),
            scene._modifier_parameter_signature([modifier.modifier_id]),
            mapping.map(bounds.topLeft()).toTuple(), False, (),
            tuple(getattr(mapping, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)))


def test_integrated_pipeline_reuses_mesh_pixels_when_request_extent_changes(scene, monkeypatch):
    source, bounds = source_image(), QRectF(-37, -61, 331, 273)
    mapping = QTransform()
    modifier = mesh(scene, bounds, mapping)
    full, frame = render_stages(scene, source, bounds, [modifier], mapping)
    scene._interactive_render = True
    scene._effect_region_requests = True
    from comic_editor.ui import distort_rendering
    calls = []
    original = distort_rendering.render_distort
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_rendering, "render_distort", counted)
    for region in (QRectF(10, 10, 180, 120), QRectF(20, 20, 190, 110)):
        result, placement = render_stages(scene, source, bounds, [modifier], mapping,
            required=region, request_scope=("object", "mesh", "canvas"), source_key=("source", 1))
        crop = placement.translated(-frame.topLeft()).toAlignedRect()
        assert result == full.copy(crop)
        assert len(calls) == 1


@pytest.mark.parametrize("variant", ["original", "anisotropic"])
def test_distortion_and_kuwahara_region_matches_complete_output(scene, variant):
    source, bounds = source_image(), QRectF(-37, -61, 331, 273)
    mapping = QTransform()
    warp = mesh(scene, bounds, mapping)
    smooth = KuwaharaModifier(variant=variant, size=3, tensor_radius=1,
                              anisotropy=60, processing_scale=100, iterations=1)
    scene.chapter.modifiers[smooth.modifier_id] = smooth
    modifiers = [warp, smooth]
    full, full_bounds = render_stages(scene, source, bounds, modifiers, mapping)
    scene._interactive_render = True
    scene._effect_region_requests = True
    for requested in (QRectF(40, 20, 95, 90), QRectF(-68, -72, 110, 95),
                      QRectF(210, 145, 110, 100)):
        plan = _stage_plan(scene, bounds, modifiers, mapping, ("kuwahara-source", variant),
                           False, requested)
        assert plan.targets[0].width() < full_bounds.width()
        region, placement = render_stages(
            scene, source, bounds, modifiers, mapping, required=requested,
            request_scope=("object", "kuwahara", variant), source_key=("kuwahara-source", variant))
        expected = crop(full, full_bounds, placement)
        actual_bytes = np.frombuffer(region.constBits(), dtype=np.uint8)
        expected_bytes = np.frombuffer(expected.constBits(), dtype=np.uint8)
        difference = np.abs(actual_bytes.astype(np.int16) - expected_bytes.astype(np.int16))
        # The anisotropic sector reduction can round a rare channel by one byte
        # when the BLAS batch shape changes with the requested rectangle.
        assert difference.max() <= 1
        assert np.count_nonzero(difference) <= actual_bytes.size // 1000


def regional(scene, source, bounds, modifier, mapping, requested, revision=0):
    frame = aligned(effect_bounds(bounds, [modifier], mapping))
    target = aligned(requested.intersected(frame))
    image, provisional = render_mesh_regions(scene, source, bounds, frame, target,
        modifier, mapping, stage_key(scene, bounds, target, modifier, mapping, revision),
        ("object", "a", modifier.modifier_id))
    return image, target, provisional


def crop(image, bounds, target):
    rect = QRectF(target)
    rect.translate(-bounds.topLeft())
    return image.copy(rect.toAlignedRect())


@pytest.mark.parametrize("interpolation", ["nearest", "bilinear", "bicubic"])
@pytest.mark.parametrize("edges", ["transparent", "clamp", "wrap"])
@pytest.mark.parametrize("transformed", [False, True, "projective"])
def test_native_tiles_equal_complete_stage_crop(scene, interpolation, edges, transformed):
    source, bounds = source_image(), QRectF(-37, -61, 331, 273)
    mapping = QTransform()
    if transformed:
        mapping.translate(13, -19).rotate(17).scale(1.15, .8)
    if transformed == "projective":
        mapping = mapping * QTransform(1., .07, .0002, -.04, 1., -.0001, 11., -7., 1.)
    modifier = mesh(scene, bounds, mapping, interpolation, edges)
    full, full_bounds = render_stages(scene, source, bounds, [modifier], mapping)
    for requested in (QRectF(-31, -47, 271, 219), QRectF(233, -83, 103, 319), full_bounds):
        actual, target, provisional = regional(scene, source, bounds, modifier, mapping, requested)
        assert not provisional
        assert actual == crop(full, full_bounds, target)


def test_overlapping_requests_and_camera_zoom_reuse_finished_native_tiles(scene, monkeypatch):
    source, bounds = source_image(600, 400), QRectF(0, 0, 600, 400)
    modifier = mesh(scene, bounds, QTransform())
    calls = []
    from comic_editor.ui import distort_pipeline
    original = distort_pipeline.render_distort_stage
    monkeypatch.setattr(distort_pipeline, "render_distort_stage",
        lambda *a, **kw: calls.append(QRectF(a[4])) or original(*a, **kw))
    first = regional(scene, source, bounds, modifier, QTransform(), QRectF(20, 20, 270, 160))
    assert len(calls) == 2
    scene.scale, scene.rotation, scene.center_x = 4., 23., 800.
    second = regional(scene, source, bounds, modifier, QTransform(), QRectF(80, 40, 290, 130))
    assert len(calls) == 2
    full, full_bounds = render_stages(scene, source, bounds, [modifier], QTransform())
    assert second[0] == crop(full, full_bounds, second[1])
    assert first[2] is False
    regional(scene, source, bounds, modifier, QTransform(), QRectF(80, 40, 290, 130), revision=1)
    assert len(calls) == 5  # one full reference render and two replaced tile revisions


@pytest.mark.parametrize("masked", [False, True])
def test_intensity_and_mask_sampling_keep_world_mapping_and_dependency_revision(scene, monkeypatch, masked):
    source, bounds = source_image(), QRectF(-37, -61, 331, 273)
    mapping = QTransform().translate(13, 22).scale(1.5, .5)
    modifier = mesh(scene, bounds, mapping)
    modifier.intensity = 37.
    mask = ToneMask()
    if masked:
        scene.chapter.masks[mask.mask_id] = mask
        modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
        def fields(modifiers, width, height, world_to_image, _bounds):
            inverse = world_to_image.inverted()[0]
            yy, xx = np.mgrid[:height, :width]
            world_x = inverse.m11() * (xx + .5) + inverse.m21() * (yy + .5) + inverse.dx()
            field = np.clip((world_x + 100) / 700 + mask.revision * .1, 0, 1).astype(np.float32)
            return {(modifier.modifier_id, "intensity"): field}
        monkeypatch.setattr(scene, "_modifier_mask_fields", fields)
    required = QRectF(-21, -39, 301, 227)
    full, full_bounds = render_stages(scene, source, bounds, [modifier], mapping)
    first = regional(scene, source, bounds, modifier, mapping, required)
    assert first[0] == crop(full, full_bounds, first[1])
    if masked:
        mask.revision += 1
    else:
        modifier.intensity = 71.
    second = regional(scene, source, bounds, modifier, mapping, required)
    full, full_bounds = render_stages(scene, source, bounds, [modifier], mapping)
    assert second[0] == crop(full, full_bounds, second[1])
    assert first[0] != second[0]


def test_retained_budget_evicts_tiles_without_changing_pixels(scene):
    source, bounds = source_image(600, 400), QRectF(0, 0, 600, 400)
    modifier = mesh(scene, bounds, QTransform())
    scene._effect_jobs.retained_budget = 256 * 256 * 4
    first = regional(scene, source, bounds, modifier, QTransform(), QRectF(20, 20, 490, 350))
    assert len(scene._effect_jobs.retained) == 1
    assert scene._effect_jobs.retained_bytes <= scene._effect_jobs.retained_budget
    assert regional(scene, source, bounds, modifier, QTransform(), QRectF(20, 20, 490, 350)) == first


def test_provisional_mask_capture_never_enters_exact_tile_cache(scene, monkeypatch):
    source, bounds = source_image(), QRectF(-37, -61, 331, 273)
    modifier = mesh(scene, bounds, QTransform())
    def fields(*_args):
        scene._effect_provisional_revision = getattr(scene, "_effect_provisional_revision", 0) + 1
        return {}
    monkeypatch.setattr(scene, "_modifier_mask_fields", fields)
    result = regional(scene, source, bounds, modifier, QTransform(), QRectF(10, 10, 100, 100))
    assert result[2]
    assert not scene._effect_jobs.retained
