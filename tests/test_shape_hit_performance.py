"""Selection must not construct distant outlines or rebuild unchanged meshes."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QTransform

from comic_editor.core.models import BoundGeometry, ChapterDocument, PathNode, ShapeStyle
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=20000)
    page = chapter.add_page("Page", BoundGeometry.rectangle(-1000, -1000, 4000, 22000))
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas.scale = 1
    yield canvas, chapter, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def open_shape(chapter, page):
    layer = chapter.add_layer(page.layer_id, "Open", BoundGeometry(
        nodes=[PathNode(x=100, y=100, width_multiplier=3),
               PathNode(x=180, y=180, width_multiplier=3)], closed=False, primitive="custom"))
    layer.layer_kind = "open_shape"
    layer.shape_style = ShapeStyle(base_thickness=40, outline_thickness=5,
                                   start_cap="square", end_cap="square")
    layer.border_width = 5
    return layer


def test_raw_hit_does_not_construct_distant_meshes(scene, monkeypatch):
    canvas, chapter, page = scene
    layer = open_shape(chapter, page)
    layer.translate_y = 12000
    monkeypatch.setattr(canvas, "layer_shape_path", lambda _layer:
                        pytest.fail("Built an offscreen raw shape mesh"))
    assert not canvas._shape_border_contains(layer.layer_id, QPointF(100, 100), raw=True)


def test_compound_hit_does_not_construct_distant_outline(scene, monkeypatch):
    canvas, chapter, page = scene
    layer = chapter.add_layer(page.layer_id, "Compound",
                              BoundGeometry.rectangle(100, 12000, 200, 100))
    layer.compound_enabled = True
    layer.border_width = 12
    monkeypatch.setattr(canvas, "_compound_outline_mesh", lambda *_args:
                        pytest.fail("Built a distant compound outline"))
    assert not canvas._shape_border_contains(layer.layer_id, QPointF(100, 100))


def test_raw_mesh_cache_tracks_shape_and_style_edits(scene, monkeypatch):
    canvas, chapter, page = scene
    layer = open_shape(chapter, page)
    builds = []
    original = canvas.layer_shape_path
    monkeypatch.setattr(canvas, "layer_shape_path", lambda value:
                        (builds.append(value.layer_id), original(value))[1])
    for _ in range(4):
        assert canvas._shape_border_contains(layer.layer_id, QPointF(110, 110), raw=True)
    assert len(builds) == 1
    for change in (lambda: setattr(layer.bound.nodes[0], "width_multiplier", 2),
                   lambda: setattr(layer.bound.nodes[0], "x", 95),
                   lambda: setattr(layer.shape_style, "start_cap", "round"),
                   lambda: setattr(layer.shape_style, "outline_thickness", 8)):
        change()
        assert canvas._shape_border_contains(layer.layer_id, QPointF(110, 110), raw=True)
    assert len(builds) == 5


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("scale", [.1, 1, 8])
def test_transformed_wide_square_caps_remain_selectable(scene, raw, scale):
    canvas, chapter, page = scene
    layer = open_shape(chapter, page)
    mapping = QTransform().translate(200, 1000).rotate(39).scale(.25, 1.8)
    layer.transform_frame = (0, 0, 300, 300)
    layer.transform_quad = [mapping.map(QPointF(x, y)).toTuple()
                            for x, y in ((0, 0), (300, 0), (300, 300), (0, 300))]
    canvas.scale = scale
    # This corner is beyond the centerline bounds, inside the thick square cap.
    local = QPointF(20, 100)
    assert canvas.layer_shape_path(layer).contains(local)
    assert canvas._shape_border_contains(layer.layer_id, mapping.map(local), raw=raw)


@pytest.mark.parametrize("scale", [.1, 1, 8])
def test_world_hit_tolerance_survives_nonuniform_layer_scaling(scene, scale):
    canvas, chapter, page = scene
    layer = chapter.add_layer(page.layer_id, "Box", BoundGeometry.rectangle(0, 0, 100, 100))
    layer.border_width = 0
    layer.transform_frame = (0, 0, 100, 100)
    layer.transform_quad = [(200, 200), (210, 200), (210, 700), (200, 700)]
    canvas.scale = scale
    assert canvas._shape_border_contains(layer.layer_id, QPointF(200 - 11 / scale, 400), raw=True)
    assert not canvas._shape_border_contains(layer.layer_id, QPointF(200 - 13 / scale, 400), raw=True)


def test_projective_shape_retains_exact_hit_path(scene):
    canvas, chapter, page = scene
    layer = chapter.add_layer(page.layer_id, "Perspective",
                              BoundGeometry.rectangle(0, 0, 100, 100))
    layer.border_width = 0
    layer.transform_frame = (0, 0, 100, 100)
    layer.transform_quad = [(200, 100), (500, 50), (390, 800), (250, 700)]
    assert not canvas.layer_world_transform(layer.layer_id).isAffine()
    assert canvas._shape_border_contains(layer.layer_id, QPointF(200, 100), raw=True)
    assert not canvas._shape_border_contains(layer.layer_id, QPointF(1000, 100), raw=True)
