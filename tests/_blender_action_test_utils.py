"""Independent inspection of Blender's slot-based animation in test probes."""
from __future__ import annotations


def owner_curves(owner):
    animation = owner.animation_data
    if animation is None or animation.action is None:
        return []
    slot = animation.action_slot
    if slot is None:
        return []
    return [
        curve
        for layer in animation.action.layers
        for strip in layer.strips if strip.type == "KEYFRAME"
        for bag in strip.channelbags if bag.slot_handle == slot.handle
        for curve in bag.fcurves
    ]


def owner_curve(owner, data_path, index=0):
    return next((
        curve for curve in owner_curves(owner)
        if curve.data_path == data_path and curve.array_index == index
    ), None)


def action_contents(action):
    """Capture keys and styles in every slot, including ones with no user."""
    return [
        (
            slot.handle, slot.identifier,
            [
                (
                    curve.data_path, curve.array_index,
                    [
                        (
                            tuple(point.co), point.interpolation, point.easing,
                            point.handle_left_type, tuple(point.handle_left),
                            point.handle_right_type, tuple(point.handle_right),
                        )
                        for point in curve.keyframe_points
                    ],
                )
                for layer in action.layers
                for strip in layer.strips if strip.type == "KEYFRAME"
                for bag in strip.channelbags if bag.slot_handle == slot.handle
                for curve in bag.fcurves
            ],
        )
        for slot in action.slots
    ]
