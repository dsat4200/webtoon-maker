"""Distant projection blocks retain independent exact outline captures."""
import pytest
from PySide6.QtCore import QRectF

from comic_editor.core.models import OutlineModifier
from test_projection_invalidation import scene, frame


@pytest.mark.parametrize("ancestor", [False, True], ids=["raster", "layer"])
def test_outline_windows_survive_ordinary_lru_eviction(scene, monkeypatch, ancestor):
    canvas, obj, group = scene
    owner = ("layer", group.layer_id) if ancestor else ("object", obj.object_id)
    canvas.chapter.add_modifier(OutlineModifier(thickness=20), [owner])
    canvas._modifier_render_cache_budget = 1
    canvas._modifier_source_cache_budget = 1
    expected = frame(canvas)
    from comic_editor.ui import interactive_effects
    monkeypatch.setattr(interactive_effects, "apply_modifier_stack",
                        lambda *_a, **_k: pytest.fail("Unchanged outline recomputed"))
    for region in (QRectF(500, 110, 10, 10), QRectF(1300, 110, 10, 10),
                   QRectF(770, 110, 10, 10)):
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0
        canvas._modifier_source_cache.clear()
        canvas._modifier_source_cache_bytes = 0
        canvas._mark_scene_dirty_world(region)
        assert frame(canvas) == expected

