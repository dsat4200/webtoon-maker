from __future__ import annotations

import pytest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject,
    SpeedLinesGradientObject, VectorDrawingObject,
)


def _legacy_payload() -> tuple[dict, str, str, str]:
    chapter = ChapterDocument(height=500)
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 400, 300))
    drawing = chapter.add_object(
        page.layer_id,
        VectorDrawingObject(),
    )
    payload = chapter.to_dict()
    next(
        item for item in payload["objects"]
        if item["id"] == drawing.object_id
    )["fill_child_ids"] = ["legacy-gradient"]
    payload["objects"].extend([
        {
            "id": "legacy-gradient", "type": "gradient",
            "gradient_type": "speed_lines",
            "parent_layer_id": page.layer_id,
        },
        {
            "id": "legacy-center", "type": "speed_center",
            "parent_layer_id": page.layer_id,
        },
    ])
    payload["layers"][0]["children"].extend([
        {"kind": "object", "id": "legacy-gradient"},
        {"kind": "object", "id": "legacy-center"},
    ])
    return payload, page.layer_id, drawing.object_id, "legacy-gradient"


def test_legacy_speed_lines_are_omitted_with_warning_and_references_repaired():
    payload, page_id, drawing_id, legacy_id = _legacy_payload()
    warnings: list[str] = []
    restored = ChapterDocument.from_dict(payload, warnings=warnings)

    assert restored.schema_version == 20
    assert warnings == ["Omitted 2 unsupported Speed Lines objects."]
    assert legacy_id not in restored.objects
    assert "legacy-center" not in restored.objects
    assert "fill_child_ids" not in restored.objects[drawing_id].to_dict()
    assert all(
        child.entity_id not in {legacy_id, "legacy-center"}
        for child in restored.layers[page_id].children
    )
    restored.validate()


def test_supported_gradient_round_trip_is_unchanged():
    chapter = ChapterDocument(height=500)
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 400, 300))
    gradient = chapter.add_object(
        page.layer_id, ColorFillGradientObject(field_type="radial")
    )
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert isinstance(restored.objects[gradient.object_id], ColorFillGradientObject)


def test_speed_lines_creation_is_disabled():
    chapter = ChapterDocument()
    page = chapter.add_page()
    with pytest.raises(ValueError, match="no longer supported"):
        chapter.add_object(page.layer_id, SpeedLinesGradientObject())
