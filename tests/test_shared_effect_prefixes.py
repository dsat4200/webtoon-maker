"""Unchanged background warps survive neighboring requests and cache pressure."""
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import DistortModifier
from comic_editor.ui import distort_pipeline
from comic_editor.ui.effect_pipeline import render_stages
from test_effect_regions import scene, image, register, enable, crop


def test_full_warp_prefix_reused_across_regions_after_ordinary_eviction(scene, monkeypatch):
    first = DistortModifier(modifier_type="distort_deform", frame=(0, 0, 320, 240),
        source_points=[(0, 0), (1, 0), (1, 1), (0, 1)],
        points=[(.04, .01), (.95, .03), (.97, .96), (.01, .97)])
    second = DistortModifier(modifier_type="distort_twirl", frame=(0, 0, 320, 240),
                            center=(160, 120), radius=110, parameters={"angle": 35})
    register(scene, [first, second])
    source, bounds, mapping = image(320, 240), QRectF(0, 0, 320, 240), QTransform()
    expected, full_bounds = render_stages(scene, source, bounds, [first, second], mapping)
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    scene._modifier_render_cache_budget = 1
    enable(scene)
    scene._projection_exact = True
    calls = []
    original = distort_pipeline.render_distort_stage
    def record(*args, **kwargs):
        calls.append(args[5].modifier_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, "render_distort_stage", record)
    arguments = dict(source_key=("background", 1), request_scope=("object", "image", "canvas"))
    regions = (QRectF(30, 40, 100, 100), QRectF(130, 70, 100, 100))
    for region in regions:
        actual = render_stages(scene, source, bounds, [first, second], mapping,
                               required=region, **arguments)
        assert actual == crop(expected, full_bounds, region)
    assert calls.count(first.modifier_id) == 1
    assert calls.count(second.modifier_id) == 2
    assert scene._effect_jobs.retained_bytes <= scene._effect_jobs.retained_budget

    # A downstream slider preserves the expensive upstream result.
    second.parameters["angle"] = 48
    render_stages(scene, source, bounds, [first, second], mapping,
                  required=regions[0], **arguments)
    assert calls.count(first.modifier_id) == 1
    # An actual source revision cannot reuse the old prefix.
    arguments["source_key"] = ("background", 2)
    render_stages(scene, source, bounds, [first, second], mapping,
                  required=regions[0], **arguments)
    assert calls.count(first.modifier_id) == 2
