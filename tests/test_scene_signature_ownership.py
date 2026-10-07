"""Semantic signature validation never mutates shared scene metadata."""
import copy
import json

from comic_editor.core.models import (BoundGeometry, ChapterDocument, LayerNode,
    RasterObject, ToneMask, VectorDrawingObject, VectorStroke, VectorStrokePoint)
from comic_editor.core.tiles import TileStore
from comic_editor.render.outputs import capture_document
from comic_editor.render.scene import EvaluatedScene


def test_mask_entity_and_tiled_stroke_signatures_validate_private_records(monkeypatch):
    chapter = ChapterDocument(width=64, height=64, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 64, 64))
    raster = chapter.add_object(page.layer_id, RasterObject())
    drawing = chapter.add_object(page.layer_id, VectorDrawingObject(strokes=[
        VectorStroke(tiling_group='group', points=[VectorStrokePoint(x=1, y=2),
                                                  VectorStrokePoint(x=20, y=25)])]))
    mask = ToneMask(contributors=[('object', raster.object_id)])
    chapter.masks[mask.mask_id] = mask
    snapshot = capture_document(chapter, TileStore())
    scene = EvaluatedScene(snapshot)
    frozen_layer = scene.chapter.layers[page.layer_id]
    frozen_mask = scene.chapter.masks[mask.mask_id]
    frozen_drawing = scene.chapter.objects[drawing.object_id]
    frozen_stroke = frozen_drawing.strokes[0]
    records = (frozen_layer, frozen_mask, frozen_stroke)
    before = [repr(record) for record in records]
    containers = (frozen_layer.bound.nodes, frozen_mask.contributors,
                  frozen_stroke.points, frozen_stroke.points[0])
    originals = {record_type: record_type.to_dict for record_type in (LayerNode, ToneMask, VectorStroke)}
    expected = [originals[type(record)](copy.deepcopy(record)) for record in records]
    frozen_ids = {id(record) for record in records}
    validated = []
    for record_type, original in originals.items():
        def private_only(record, original=original):
            assert id(record) not in frozen_ids, 'Signature serializer validated shared scene metadata'
            validated.append(type(record))
            return original(record)
        monkeypatch.setattr(record_type, 'to_dict', private_only)
    settings = scene._modifier_entity_settings(frozen_layer)
    for field in ('name', 'custom_name', 'fill_reference', 'grid_override', 'last_raster_id', 'blend_mode'):
        expected[0].pop(field, None)
    assert settings == expected[0]
    assert scene._tone_mask_signature(frozen_mask.mask_id)[0] == json.dumps(expected[1], sort_keys=True)
    tokens = []
    monkeypatch.setattr(scene, '_vector_stroke_image', lambda _drawing, _stroke, **kw:
                        tokens.append(kw['cache_token']))
    scene._render_tiled_vector_group(None, frozen_drawing, 'group')
    assert tokens == [repr(expected[2])]
    assert {LayerNode, ToneMask, VectorStroke} <= set(validated)
    assert [repr(record) for record in records] == before
    assert frozen_layer.bound.nodes is containers[0] and frozen_mask.contributors is containers[1]
    assert frozen_stroke.points is containers[2] and frozen_stroke.points[0] is containers[3]
