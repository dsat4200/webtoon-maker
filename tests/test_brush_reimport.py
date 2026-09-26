"""Re-importing source brushes must not replace saved user preset edits."""
from copy import deepcopy
from dataclasses import replace
import sqlite3
from uuid import UUID

import pytest

from comic_editor.core.brushes import BrushDefinition, BrushDynamics
from comic_editor.core.settings import EditorSettings
from comic_editor.core.sut_import import import_sut
from comic_editor.ui.brush_controls import BrushControls


@pytest.fixture
def sut_path(tmp_path):
    path = tmp_path / "brush.sut"
    with sqlite3.connect(path) as database:
        database.executescript("""
            CREATE TABLE Manager(Version INTEGER);
            INSERT INTO Manager VALUES(138);
            CREATE TABLE Node(NodeName TEXT, NodeVariantID INTEGER);
            INSERT INTO Node VALUES('Source brush', 1);
            CREATE TABLE Variant(VariantID INTEGER, BrushSize REAL, Opacity INTEGER,
                BrushChangePatternColor INTEGER, BrushHueChange INTEGER,
                BrushSaturationChange INTEGER, BrushValueChange INTEGER);
            INSERT INTO Variant VALUES(1, 20, 80, 1, 200, -10, -20);
        """)
    return path


def controls_for(*definitions):
    settings = EditorSettings(brush_presets=[brush.to_dict() for brush in definitions],
                              active_brush_id=definitions[0].id)
    return BrushControls(settings)


def test_identical_reimport_selects_existing_without_rewriting(qapp, sut_path):
    definition = import_sut(sut_path)
    controls = controls_for(definition)
    before = deepcopy(controls.settings.brush_presets)
    controls.settings.brush_size_px = 93
    changes = []
    controls.settingsChanged.connect(lambda: changes.append(True))
    result = controls.import_path(sut_path)
    assert result.id == definition.id
    assert controls.settings.brush_presets == before
    assert controls.settings.active_brush_id == result.id
    assert controls.settings.brush_size_px == 20
    assert controls.settings.brush_opacity == .8
    assert changes == [True]
    controls.deleteLater()


@pytest.mark.parametrize("edit", ["old_mapping", "size", "name", "dynamics", "secondary"])
def test_different_same_source_preset_is_preserved_and_new_copy_reused(qapp, sut_path, edit):
    imported = import_sut(sut_path)
    if edit == "old_mapping":
        source = deepcopy(imported.source)
        source["importer_version"] = 3
        original = replace(imported, source=source, hue_shift=0, saturation_shift=0,
                           luminosity_shift=0, hue_jitter=1)
    elif edit == "size":
        original = replace(imported, size=39)
    elif edit == "name":
        original = replace(imported, name="My renamed brush")
    elif edit == "dynamics":
        original = replace(imported, dynamics={"size": BrushDynamics(pressure=True, minimum=.42)})
    else:
        original = replace(imported, dual=BrushDefinition(name="My added secondary", size=5))
    # Occupied copy names must not be replaced or reused merely by their names.
    occupied = BrushDefinition(id="other", name="Source brush copy", size=8)
    controls = controls_for(original, occupied)
    before = deepcopy(controls.settings.brush_presets)
    result = controls.import_path(sut_path)
    assert result.id != original.id
    assert UUID(result.id).version == 4
    assert result.name == "Source brush copy 2"
    assert replace(result, id=imported.id, name=imported.name) == imported
    assert controls.settings.brush_presets[:-1] == before
    after = deepcopy(controls.settings.brush_presets)
    again = controls.import_path(sut_path)
    assert again.id == result.id
    assert controls.settings.brush_presets == after
    assert controls.settings.active_brush_id == result.id
    controls.deleteLater()


def test_edited_copy_is_preserved_and_renamed_identical_copy_is_reused(qapp, sut_path):
    imported = import_sut(sut_path)
    original = replace(imported, size=31)
    controls = controls_for(original)
    first = controls.import_path(sut_path)
    edited = replace(first, opacity=.23)
    controls.settings.brush_presets[-1] = edited.to_dict()
    before = deepcopy(controls.settings.brush_presets)
    second = controls.import_path(sut_path)
    assert second.id not in {original.id, first.id}
    assert second.name == "Source brush copy 2"
    assert controls.settings.brush_presets[:-1] == before
    renamed = replace(second, name="Preferred corrected import")
    controls.settings.brush_presets[-1] = renamed.to_dict()
    before_reuse = deepcopy(controls.settings.brush_presets)
    again = controls.import_path(sut_path)
    assert again.id == renamed.id
    assert again.name == renamed.name
    assert controls.settings.brush_presets == before_reuse
    controls.deleteLater()
