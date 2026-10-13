"""Semantic signature validation never mutates shared scene metadata."""
import copy
import json

from comic_editor.core.models import (BoundGeometry, ChapterDocument, LayerNode,
    RasterObject, ToneMask, VectorDrawingObject, VectorStroke, VectorStrokePoint,
    OutlineModifier, ParameterMaskBinding)
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


def owned_signature_scene():
    chapter = ChapterDocument(width=64, height=64, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 64, 64))
    raster = chapter.add_object(page.layer_id, RasterObject())
    mask = ToneMask(contributors=[('object', raster.object_id)])
    chapter.masks[mask.mask_id] = mask
    modifier = OutlineModifier(thickness=2.)
    modifier.parameter_masks['thickness'] = ParameterMaskBinding(mask.mask_id, 1., 3.)
    chapter.add_modifier(modifier, [('object', raster.object_id)])
    scene = EvaluatedScene(capture_document(chapter, TileStore()))
    return scene, page.layer_id, raster.object_id, mask.mask_id, modifier.modifier_id


def test_repeated_owned_settings_preserve_all_original_json_bytes(monkeypatch):
    from comic_editor.render.scene_kernels import SceneKernels
    scene, layer_id, object_id, mask_id, modifier_id = owned_signature_scene()
    cases = [(scene._modifier_entity_signature, SceneKernels._modifier_entity_signature,
              scene.chapter.layers[layer_id], {}),
             (scene._modifier_entity_signature, SceneKernels._modifier_entity_signature,
              scene.chapter.objects[object_id], {'compact': True}),
             (scene._modifier_settings_signature, SceneKernels._modifier_settings_signature,
              scene.chapter.modifiers[modifier_id], {}),
             (scene._tone_mask_settings_signature, SceneKernels._tone_mask_settings_signature,
              scene.chapter.masks[mask_id], {})]
    for cached, original, record, kwargs in cases:
        expected = original(scene, record, **kwargs)
        calls = []
        kind = type(record)
        ordinary = kind.to_dict
        def serialize(value, ordinary=ordinary):
            calls.append(value)
            return ordinary(value)
        with monkeypatch.context() as patch:
            patch.setattr(kind, 'to_dict', serialize)
            assert cached(record, **kwargs) == cached(record, **kwargs) == expected
        assert len(calls) == 1
    exported = scene.cache_state()
    assert '_model_serializations' not in exported


def test_derived_preview_record_is_not_the_frozen_settings_owner():
    scene, layer_id, object_id, mask_id, modifier_id = owned_signature_scene()
    original = scene.chapter.modifiers[modifier_id]
    before = scene._modifier_settings_signature(original)
    preview = copy.deepcopy(original)
    preview.thickness = 12.
    first = scene._modifier_settings_signature(preview)
    preview.thickness = 20.
    second = scene._modifier_settings_signature(preview)
    assert first != second != before
    assert scene._modifier_settings_signature(original) == before


def test_complete_dependencies_revalidate_pixels_and_render_context_after_settings_reuse():
    from PySide6.QtGui import QColor, QImage
    scene, layer_id, object_id, mask_id, modifier_id = owned_signature_scene()
    obj = scene.chapter.objects[object_id]
    initial = scene._modifier_object_signature(obj)
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    scene.tiles.set_tile(object_id, (0, 0), image)
    changed = scene._modifier_object_signature(obj)
    assert initial[0] == changed[0] and initial[3] != changed[3]
    ordinary_mask = scene._tone_mask_signature(mask_id)
    scene._rendering_halftone_source = True
    assert ordinary_mask != scene._tone_mask_signature(mask_id)
    scene._rendering_halftone_source = False
    scene._tiling_capture_geometry = {'repeat': (11., 13.)}
    assert changed != scene._modifier_object_signature(obj)


def test_settings_reuse_cannot_cross_a_new_snapshot():
    scene, layer_id, object_id, mask_id, modifier_id = owned_signature_scene()
    original = scene._modifier_settings_signature(scene.chapter.modifiers[modifier_id])
    chapter = copy.deepcopy(scene.chapter)
    chapter.modifiers[modifier_id].thickness = 30.
    next_scene = EvaluatedScene(capture_document(chapter, TileStore()))
    assert next_scene._modifier_settings_signature(next_scene.chapter.modifiers[modifier_id]) != original
    assert scene._modifier_settings_signature(scene.chapter.modifiers[modifier_id]) == original
