"""Slot-aware Action access shared by Blender 4.5 and 5.2.

An Action can contain channels for several data-blocks. Reading its first slot
would silently use somebody else's animation, even in Blender versions that
still expose the deprecated Action.fcurves shortcut.
"""
from __future__ import annotations

from typing import Iterator


def is_layered_action(action: object) -> bool:
    return bool(getattr(action, "is_action_layered", False))


def iter_slot_channelbags(action: object, slot: object | None) -> Iterator[object]:
    """Read every channelbag belonging to the assigned slot without creating any."""
    if slot is None:
        return
    for layer in action.layers:
        for strip in layer.strips:
            if strip.type != "KEYFRAME":
                continue
            bag = strip.channelbag(slot)
            if bag is not None:
                yield bag


def iter_action_curves(action: object) -> Iterator[object]:
    """Visit all slots, including ones not assigned to an active data-block."""
    if not is_layered_action(action):
        yield from getattr(action, "fcurves", ())
        return
    for layer in action.layers:
        for strip in layer.strips:
            if strip.type == "KEYFRAME":
                for bag in strip.channelbags:
                    yield from bag.fcurves


def find_action_curve(
    owner: object, data_path: str, index: int = 0,
) -> object | None:
    animation = getattr(owner, "animation_data", None)
    action = getattr(animation, "action", None)
    if action is None:
        return None
    if not is_layered_action(action):
        curves = getattr(action, "fcurves", None)
        return curves.find(data_path, index=index) if curves is not None else None
    for bag in iter_slot_channelbags(action, animation.action_slot):
        curve = bag.fcurves.find(data_path, index=index)
        if curve is not None:
            return curve
    return None


def action_curve_index(owner: object) -> dict[tuple[str, int], object]:
    """Index only the owner's assigned slot for one read/write operation.

    Keep the first match, just like ``find_action_curve`` when an Action has
    several keyframe strips. Callers must rebuild this after replacing an
    Action or changing the assigned slot; this is deliberately not cached here.
    """
    animation = getattr(owner, "animation_data", None)
    action = getattr(animation, "action", None)
    if action is None:
        return {}
    result: dict[tuple[str, int], object] = {}
    if is_layered_action(action):
        collections = (
            bag.fcurves
            for bag in iter_slot_channelbags(action, animation.action_slot)
        )
    else:
        collections = (getattr(action, "fcurves", ()),)
    for curves in collections:
        for curve in curves:
            result.setdefault((curve.data_path, int(curve.array_index)), curve)
    return result
