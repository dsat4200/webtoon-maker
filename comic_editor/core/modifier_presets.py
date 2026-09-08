"""Portable modifier settings, independent of instance and chapter bindings."""
from __future__ import annotations

import copy
import json

from comic_editor.core.models import ModifierPreset, modifier_from_dict


INSTANCE_FIELDS = frozenset({
    "id", "type", "name", "expanded", "muted", "parameter_masks", "target_layer_id",
})


def _validated_modifier(data):
    try:
        json.dumps(data, allow_nan=False)
        return modifier_from_dict(copy.deepcopy(data))
    except (TypeError, ValueError, KeyError, IndexError, AttributeError, OverflowError) as error:
        raise ValueError(f"Invalid modifier preset settings: {error}") from error


def validate_modifier_preset_settings(modifier_type: str, settings: dict) -> dict:
    """Canonicalize settings for exactly one known modifier kind.

    Unknown fields and instance bindings are rejected, so a malformed preset
    cannot silently change type or import a mask, object, or layer reference.
    """
    if not isinstance(modifier_type, str) or not modifier_type:
        raise ValueError("Modifier preset requires a modifier type")
    if not isinstance(settings, dict) or any(not isinstance(key, str) for key in settings):
        raise ValueError("Modifier preset settings must be an object with named fields")
    default = _validated_modifier({"type": modifier_type}).to_dict()
    permitted = set(default) - INSTANCE_FIELDS
    unexpected = set(settings) - permitted
    if unexpected:
        raise ValueError("Unsupported modifier preset settings: " + ", ".join(sorted(unexpected)))
    result = _validated_modifier({"type": modifier_type, **copy.deepcopy(settings)})
    serialized = result.to_dict()
    if serialized.get("type") != modifier_type:
        raise ValueError("Modifier preset settings changed the modifier type")
    return {key: copy.deepcopy(value) for key, value in serialized.items() if key in permitted}


def modifier_preset_settings(modifier) -> dict:
    """Snapshot every effect parameter, without modifying the current instance."""
    try:
        serialized = copy.deepcopy(modifier).to_dict()
        modifier_type = serialized["type"]
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
        raise ValueError(f"Cannot save modifier preset: {error}") from error
    settings = {key: value for key, value in serialized.items() if key not in INSTANCE_FIELDS}
    return validate_modifier_preset_settings(modifier_type, settings)


def preset_from_modifier(name: str, modifier) -> ModifierPreset:
    """Create an independent preset from the modifier's current parameters."""
    settings = modifier_preset_settings(modifier)
    serialized = copy.deepcopy(modifier).to_dict()
    result = ModifierPreset(name=name, modifier_type=serialized["type"], settings=settings)
    result.validate()
    return result


def apply_modifier_preset(modifier, preset: ModifierPreset):
    """Return a new same-kind modifier retaining all instance-local bindings."""
    if not isinstance(preset, ModifierPreset):
        raise ValueError("Expected a modifier preset")
    candidate = copy.deepcopy(preset)
    candidate.validate()
    try:
        original = copy.deepcopy(modifier).to_dict()
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
        raise ValueError(f"Cannot load modifier preset: {error}") from error
    if original.get("type") != candidate.modifier_type:
        raise ValueError("Modifier presets can only be loaded into the same modifier type")
    return _validated_modifier({**original, **copy.deepcopy(candidate.settings)})
