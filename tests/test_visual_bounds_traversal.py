"""Bounds share mappings within one traversal and observe the next real edit."""
from collections import Counter

import pytest
from PySide6.QtGui import QColor, QImage

from comic_editor.core import assets
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, OutlineModifier
from comic_editor.core.tiles import TileStore


@pytest.mark.parametrize('effects', [False, True])
def test_nested_projective_siblings_keep_exact_bounds_and_retire_between_queries(qapp, monkeypatch, effects):
    chapter = ChapterDocument(width=256, height=256)
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 256, 256))
    page.transform_frame = (0, 0, 256, 256)
    page.transform_quad = [(11, 19), (301, 13), (277, 311), (-31, 238)]
    group = chapter.add_layer(page.layer_id, 'Nested', BoundGeometry.rectangle(9, 17, 120, 150))
    group.translate_x, group.translate_y = 7, -9
    tiles = TileStore()
    for index in range(18):
        obj = chapter.add_object(group.layer_id, RasterObject(x=11*index, y=7*index))
        tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        tile.fill(QColor('red'))
        tiles.set_tile(obj.object_id, (0, 0), tile)
        chapter.add_modifier(OutlineModifier(thickness=3), [('object', obj.object_id)])
    ordinary = assets._layer_world_transform
    calls = Counter()
    def mapping(document, identifier):
        calls[identifier] += 1
        return ordinary(document, identifier)
    monkeypatch.setattr(assets, '_layer_world_transform', mapping)
    def check():
        expected = assets.entity_visual_bounds(chapter, tiles, 'layer', group.layer_id,
            include_effects=effects, layer_mapping=lambda identifier: ordinary(chapter, identifier))
        actual = assets.entity_visual_bounds(chapter, tiles, 'layer', group.layer_id, include_effects=effects)
        assert actual.getRect() == expected.getRect()
        assert calls[group.layer_id] == 1
        assert all(count == 1 for count in calls.values())
        calls.clear()
        return actual
    before = check()
    page.transform_quad[2] = (411, 351)
    group.translate_x += 18
    assert check() != before

