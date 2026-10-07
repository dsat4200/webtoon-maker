"""Capture-local reuse of pure geometry and exact settings strings.

The scope is thread-local, so copying an effect worker's context never retains
the live model or an owning-thread memo. Every lookup snapshots current model
fields; unsignaled mutable changes therefore remain visible within a capture.
"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from functools import lru_cache
import math
from threading import local

from PySide6.QtCore import QRectF


_local = local()
_LIMIT = 2048
_BUDGET = 16 * 1024 * 1024
_MISSING = object()


@lru_cache(maxsize=128)
def _field_names(value_type):
    return tuple(field.name for field in fields(value_type))


def _fingerprint(value, seen=None):
    """Typed immutable snapshot without calling validating serializers."""
    value_type = type(value)
    if value is None:
        return (type(None),), 64
    if value_type is bool:
        return (bool, value), 96
    if value_type is int:
        return (int, value), 96 + 4 * ((value.bit_length() + 29) // 30)
    if value_type is float:
        if not math.isfinite(value):
            return None
        # Finite floats have exact equality; the explicit tag separates int
        # and bool, while this discriminator preserves signed zero. These
        # capture-local tokens never become serialized artwork keys.
        negative_zero = value == 0. and math.copysign(1., value) < 0.
        return (float, value, negative_zero), 160
    if value_type is str:
        return (str, value), 112 + 4 * len(value)
    if value_type in (tuple, list, dict):
        dataclass_value = False
    else:
        dataclass_value = is_dataclass(value)
        if not dataclass_value:
            return None
    seen = set() if seen is None else seen
    if id(value) in seen:
        return None
    seen.add(id(value))
    try:
        if dataclass_value:
            names = _field_names(value_type)
            tokens, size = [], 112 + 8 * len(names)
            for name in names:
                result = _fingerprint(getattr(value, name), seen)
                if result is None:
                    return None
                token, retained = result
                tokens.append(token)
                # The fixed type/order identifies field names. Keep the old
                # name-token and pair-container charge despite omitting their
                # temporary tuples from this capture-local token.
                size += 240 + 4 * len(name) + retained
            return (value_type, tuple(tokens)), size
        if value_type is dict:
            if not all(type(key) is str for key in value):
                return None
            items = tuple((key, value[key]) for key in sorted(value))
        else:
            items = value
        tokens, size = [], 112 + 8 * len(items)
        for item in items:
            result = _fingerprint(item, seen)
            if result is None:
                return None
            token, retained = result
            tokens.append(token)
            size += retained
        return (value_type, tuple(tokens)), size
    finally:
        seen.remove(id(value))


class _Memo:
    def __init__(self, context):
        self.context = context
        self.entries = OrderedDict()
        self.bytes = 0

    def get(self, key):
        entry = self.entries.get(key)
        if entry is None:
            return _MISSING
        self.entries.move_to_end(key)
        return entry[0]

    def put(self, key, value, size):
        size += 256
        if size > _BUDGET:
            return
        previous = self.entries.pop(key, None)
        if previous is not None:
            self.bytes -= previous[1]
        self.entries[key] = value, size
        self.bytes += size
        while len(self.entries) > _LIMIT or self.bytes > _BUDGET:
            _, (_, retained) = self.entries.popitem(last=False)
            self.bytes -= retained

    def clear(self):
        self.entries.clear()
        self.bytes = 0


@contextmanager
def geometry_scope(context=()):
    previous = getattr(_local, 'memo', None)
    _local.memo = _Memo(context)
    try:
        yield _local.memo
    finally:
        _local.memo = previous
        if previous is not None:
            # Reentrant captures may change an unsignaled model. Do not keep
            # the outer capture's earlier settings/geometry after restoration.
            previous.clear()


def settings_signature(modifier, serializer):
    memo = getattr(_local, 'memo', None)
    state = _fingerprint(modifier) if memo is not None else None
    if state is None:
        return repr(serializer(modifier))
    token, size = state
    key = ('settings', serializer, token)
    previous = memo.get(key)
    if previous is not _MISSING:
        return previous
    result = repr(serializer(modifier))
    # The established serializer can normalize model fields. Memoize the
    # resulting current state, without changing that first-call behavior.
    state = _fingerprint(modifier)
    if state is not None:
        token, size = state
        memo.put(('settings', serializer, token), result, size + 4 * len(result))
    return result


def cached_bounds(bounds, modifiers, mapping, compute):
    memo = getattr(_local, 'memo', None)
    state = _fingerprint(tuple(modifiers)) if memo is not None else None
    if state is None:
        return compute()
    token, size = state
    rectangle = tuple(float(value).hex() for value in bounds.getRect())
    transform = (() if mapping is None else tuple(
        float(getattr(mapping, f'm{i}{j}')()).hex() for i in range(1, 4) for j in range(1, 4)))
    key = ('bounds', token, rectangle, transform)
    previous = memo.get(key)
    if previous is not _MISSING:
        return QRectF(previous)
    result = compute()
    memo.put(key, QRectF(result), size + 2048)
    return result
