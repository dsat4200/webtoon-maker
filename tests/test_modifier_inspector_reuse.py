"""Duplicate public signals preserve one synchronous, current inspector tree."""
import pytest
from PySide6.QtWidgets import QCheckBox, QLabel

from comic_editor.core.models import (
    BrightnessContrastModifier, HalftoneModifier, ParameterMaskBinding,
    SolidColorOverlayModifier, ToneMask,
)
from comic_editor.ui.mask_controls import DualEndpointSlider
from comic_editor.ui.modifier_controls import ModifierCard
from test_modifier_edit_locality import scene


def track_cards(monkeypatch):
    calls = []
    original = ModifierCard.__init__
    def construct(self, modifier, owner, parent=None):
        calls.append((owner.canvas.selected_id, modifier.modifier_id))
        original(self, modifier, owner, parent)
    monkeypatch.setattr(ModifierCard, '__init__', construct)
    return calls


def test_selection_changed_and_selection_set_changed_build_once_synchronously(scene, monkeypatch):
    canvas, controls, _page, target, other, _image = scene
    modifier = BrightnessContrastModifier(brightness=25)
    canvas.chapter.add_modifier(modifier, [('object', target.object_id), ('object', other.object_id)])
    controls.refresh()
    calls = track_cards(monkeypatch)
    canvas.set_selection('object', other.object_id, activate_default_tool=False)
    assert calls == [(other.object_id, modifier.modifier_id)]
    card = controls._cards[modifier.modifier_id]
    assert card._parameter_controls['brightness'][1].value() == 25
    assert controls.targets() == [('object', other.object_id)]
    canvas.selectionChanged.emit('object', other.object_id)
    canvas.selectionSetChanged.emit([('object', other.object_id)])
    assert controls._cards[modifier.modifier_id] is card
    assert len(calls) == 1


def test_full_modifier_undo_three_refresh_notifications_build_once_with_restored_values(scene, monkeypatch):
    canvas, controls, _page, target, _other, _image = scene
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [('object', target.object_id)])
    controls.refresh()
    controls.set_parameter(modifier.modifier_id, 'brightness', 40, True)
    controls.refresh()
    calls = track_cards(monkeypatch)
    refreshes = []
    original = controls.refresh
    def refresh():
        refreshes.append(1)
        original()
    monkeypatch.setattr(controls, 'refresh', refresh)
    canvas.command_stack.undo()
    assert len(refreshes) == 3
    assert calls == [(target.object_id, modifier.modifier_id)]
    card = controls._cards[modifier.modifier_id]
    assert card.modifier is canvas.chapter.modifiers[modifier.modifier_id]
    assert card._parameter_controls['brightness'][1].value() == 0
    assert canvas.entity_world_rect('object', target.object_id).x() == target.x


@pytest.mark.parametrize('change', ['endpoint', 'mask_record', 'source_name', 'shared_owner_name',
                                    'ancestor_geometry', 'same_value_replacement', 'selection_geometry'])
def test_mutable_ui_dependencies_cannot_skip_needed_inspector_refresh(scene, monkeypatch, change):
    canvas, controls, page, target, other, _image = scene
    mask = ToneMask(name='Original', contributors=[('object', other.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    modifier = HalftoneModifier(color_mode='target_layer', target_layer_id=other.object_id,
                               parameter_masks={'intensity': ParameterMaskBinding(mask.mask_id, 10, 80)})
    canvas.chapter.add_modifier(modifier, [('object', target.object_id), ('object', other.object_id)])
    controls.refresh()
    old = controls._cards[modifier.modifier_id]
    calls = track_cards(monkeypatch)
    if change == 'endpoint':
        modifier.parameter_masks['intensity'].black_value = 20
    elif change == 'mask_record':
        mask.name = 'Renamed'
    elif change == 'source_name':
        other.name = 'Changed source'
    elif change == 'shared_owner_name':
        target.name = 'Changed owner'
    elif change == 'ancestor_geometry':
        page.bound = type(page.bound).rectangle(5, 0, 1075, 2048)
    elif change == 'same_value_replacement':
        from comic_editor.core.models import modifier_from_dict
        canvas.chapter.modifiers[modifier.modifier_id] = modifier_from_dict(modifier.to_dict())
    else:
        target.x += 5
    controls.refresh()
    current = controls._cards[modifier.modifier_id]
    assert current is not old and len(calls) == 1
    if change == 'endpoint':
        assert current.findChild(DualEndpointSlider).black == 20
    if change == 'source_name':
        assert current.findChild(QLabel, 'halftoneTargetLayerName').text() == 'Changed source'
    if change == 'same_value_replacement':
        assert current.modifier is canvas.chapter.modifiers[modifier.modifier_id]
    controls.refresh()
    assert controls._cards[modifier.modifier_id] is current and len(calls) == 1


def test_changed_linked_owner_kind_updates_outline_applicability(scene):
    canvas, controls, page, target, _other, _image = scene
    modifier = SolidColorOverlayModifier()
    canvas.chapter.add_modifier(modifier, [('object', target.object_id)])
    controls.refresh()
    old = controls._cards[modifier.modifier_id]
    assert old.findChild(QCheckBox, 'overlayApplyToOutline').isHidden()
    page.modifier_ids.append(modifier.modifier_id)
    controls.refresh()
    current = controls._cards[modifier.modifier_id]
    assert current is not old
    assert not current.findChild(QCheckBox, 'overlayApplyToOutline').isHidden()


@pytest.mark.parametrize('shared', [False, True])
def test_append_builds_only_new_card_and_preserves_current_bindings(scene, monkeypatch, shared):
    canvas, controls, _page, target, other, _image = scene
    owners = [('object', target.object_id)]
    if shared:
        owners.append(('object', other.object_id))
        canvas.set_selection_set(owners)
    modifier = BrightnessContrastModifier(brightness=25)
    canvas.chapter.add_modifier(modifier, owners)
    controls.refresh()
    original = controls._cards[modifier.modifier_id]
    calls = track_cards(monkeypatch)
    controls.add_modifier('hsl')
    appended = canvas.chapter.modifiers[controls.common_ids()[-1]]
    assert calls == [(canvas.selected_id, appended.modifier_id)]
    assert controls._cards[modifier.modifier_id] is original
    assert original.modifier is canvas.chapter.modifiers[modifier.modifier_id]
    assert original._parameter_controls['brightness'][1].value() == 25
    assert controls.stack_layout.itemAt(0).widget() is original
    assert controls.stack_layout.itemAt(1).widget() is controls._cards[appended.modifier_id]
    # Reused controls still address the current original effect, not the new one.
    original._parameter_controls['brightness'][1].setValue(31)
    assert canvas.chapter.modifiers[modifier.modifier_id].brightness == 31
    assert appended.hue == 0
    controls.finish_parameter_drag()


@pytest.mark.parametrize('change', ['old_modifier', 'mask', 'ancestor', 'owner', 'replacement'])
def test_append_with_other_changed_dependencies_rebuilds_existing_card(scene, monkeypatch, change):
    canvas, controls, page, target, _other, _image = scene
    modifier = BrightnessContrastModifier(brightness=25)
    canvas.chapter.add_modifier(modifier, [('object', target.object_id)])
    mask = ToneMask(name='Original')
    canvas.chapter.masks[mask.mask_id] = mask
    controls.refresh()
    original = controls._cards[modifier.modifier_id]
    if change == 'old_modifier':
        modifier.brightness = 40
    elif change == 'mask':
        mask.name = 'Renamed'
    elif change == 'ancestor':
        page.bound = type(page.bound).rectangle(5, 0, 1075, 2048)
    elif change == 'owner':
        target.name = 'Renamed owner'
    else:
        from comic_editor.core.models import modifier_from_dict
        canvas.chapter.modifiers[modifier.modifier_id] = modifier_from_dict(modifier.to_dict())
    calls = track_cards(monkeypatch)
    controls.add_modifier('hsl')
    assert controls._cards[modifier.modifier_id] is not original
    assert len(calls) == 2
    current = controls._cards[modifier.modifier_id]
    assert current.modifier is canvas.chapter.modifiers[modifier.modifier_id]
    assert current._parameter_controls['brightness'][1].value() == (40 if change == 'old_modifier' else 25)


def test_append_undo_redo_rebinds_cards_to_restored_records(scene, monkeypatch):
    canvas, controls, _page, target, _other, _image = scene
    modifier = BrightnessContrastModifier(brightness=25)
    canvas.chapter.add_modifier(modifier, [('object', target.object_id)])
    controls.refresh()
    original = controls._cards[modifier.modifier_id]
    controls.add_modifier('hsl')
    assert controls._cards[modifier.modifier_id] is original
    calls = track_cards(monkeypatch)
    canvas.command_stack.undo()
    current = controls._cards[modifier.modifier_id]
    assert current is not original and len(calls) == 1
    assert current.modifier is canvas.chapter.modifiers[modifier.modifier_id]
    canvas.command_stack.redo()
    restored = controls._cards[modifier.modifier_id]
    assert restored is not current and len(calls) == 3
    assert restored.modifier is canvas.chapter.modifiers[modifier.modifier_id]


def test_append_preserves_posterize_statistics_and_existing_prefix_edits_refresh(scene, qapp):
    from comic_editor.core.models import PosterizeModifier
    from comic_editor.ui.posterize_controls import PosterizeControls, PosterizeSampler
    from test_posterize_deferred_statistics import spin, assert_same
    canvas, controls, _page, target, _other, _image = scene
    brightness, posterize = BrightnessContrastModifier(), PosterizeModifier()
    for modifier in (brightness, posterize):
        canvas.chapter.add_modifier(modifier, [('object', target.object_id)])
    controls.refresh()
    old = controls._cards[posterize.modifier_id]
    panel = old.findChild(PosterizeControls)
    spin(qapp, lambda: panel._statistics_ready)
    key = panel.sampler._key
    controls.add_modifier('hsl')
    assert controls._cards[posterize.modifier_id] is old
    assert old.findChild(PosterizeControls) is panel
    assert panel._statistics_ready and panel.sampler._key == key
    controls._cards[brightness.modifier_id]._parameter_controls['brightness'][1].setValue(35)
    controls.finish_parameter_drag()
    spin(qapp, lambda: panel._statistics_ready)
    expected = PosterizeSampler().sample(canvas, controls.targets(), posterize.modifier_id)
    assert_same(panel.wheel.statistics, expected)
    assert panel.sampler._key != key
