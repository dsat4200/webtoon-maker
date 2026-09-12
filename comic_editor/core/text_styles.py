"""Sparse text color ranges using Python string indexes, independent of Qt."""
from __future__ import annotations

import heapq
from typing import TYPE_CHECKING, Any, Iterable

from comic_editor.core.models import canonical_argb

if TYPE_CHECKING:
    from comic_editor.core.models import TextObject


def normalized_color_runs(
    text: str, runs: object, base_color: str,
) -> list[dict[str, Any]]:
    """Clamp and merge sparse overrides; later overlapping entries win.

    A sweep avoids allocating a color for every character, including when a
    large pasted text object only has one styled word.
    """
    if not isinstance(runs, (list, tuple)) or not text:
        return []
    base = canonical_argb(base_color, "#FF111111")
    events: dict[int, list[tuple[int, int, str]]] = {}
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            continue
        try:
            start = max(0, min(len(text), int(run["start"])))
            end = max(start, min(len(text), int(run["end"])))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if end <= start:
            continue
        color = canonical_argb(run.get("color"), base)
        events.setdefault(start, []).append((-index, end, color))
        events.setdefault(end, [])
    positions = sorted(events)
    active: list[tuple[int, int, str]] = []
    result: list[dict[str, Any]] = []
    for position, end in zip(positions, positions[1:]):
        for item in events[position]:
            heapq.heappush(active, item)
        while active and active[0][1] <= position:
            heapq.heappop(active)
        if not active or active[0][2] == base:
            continue
        color = active[0][2]
        if result and result[-1]["end"] == position and result[-1]["color"] == color:
            result[-1]["end"] = end
        else:
            result.append({"start": position, "end": end, "color": color})
    return result


def text_color_at(obj: TextObject, position: int) -> str:
    """Read the color of a character, clamping an end caret to the last one."""
    base = canonical_argb(obj.text_color, "#FF111111")
    position = max(0, min(max(0, len(obj.text) - 1), int(position)))
    for run in reversed(obj.color_runs):
        if run["start"] <= position < run["end"]:
            return canonical_argb(run["color"], base)
    return base


def apply_text_color(obj: TextObject, start: int, end: int, color: str) -> bool:
    """Color a range, or replace the base color when the whole object is used."""
    start, end = sorted((max(0, min(len(obj.text), int(start))),
                         max(0, min(len(obj.text), int(end)))))
    color = canonical_argb(color, obj.text_color)
    before = (obj.text_color, obj.color_runs)
    if start == 0 and end == len(obj.text):
        obj.text_color, obj.color_runs = color, []
    elif start != end:
        obj.color_runs = normalized_color_runs(
            obj.text, [*obj.color_runs, {"start": start, "end": end, "color": color}],
            obj.text_color,
        )
    return before != (obj.text_color, obj.color_runs)


def replace_text_range(obj: TextObject, start: int, end: int, value: str) -> None:
    """Edit text while retaining neighboring colors and the insertion style."""
    start, end = sorted((max(0, min(len(obj.text), int(start))),
                         max(0, min(len(obj.text), int(end)))))
    value = str(value)
    color = text_color_at(obj, start if start != end else max(0, start - 1))
    delta = len(value) - (end - start)
    runs = []
    for run in obj.color_runs:
        if run["start"] < start:
            runs.append({**run, "end": min(start, run["end"])})
        if run["end"] > end:
            runs.append({**run, "start": max(end, run["start"]) + delta,
                         "end": run["end"] + delta})
    if value:
        runs.append({"start": start, "end": start + len(value), "color": color})
    obj.text = obj.text[:start] + value + obj.text[end:]
    obj.color_runs = normalized_color_runs(obj.text, runs, obj.text_color)


def text_index_to_qt_position(text: str, index: int) -> int:
    """Convert a Python codepoint index to QTextCursor's UTF-16 position."""
    index = max(0, min(len(text), int(index)))
    return len(text[:index].encode("utf-16-le", errors="surrogatepass")) // 2


def text_indexes_to_qt_positions(text: str, indexes: Iterable[int]) -> dict[int, int]:
    """Convert many range boundaries with a single pass over the text bytes."""
    indexes = sorted({max(0, min(len(text), int(index))) for index in indexes})
    if text.isascii():
        return {index: index for index in indexes}
    result: dict[int, int] = {}
    previous, position = 0, 0
    for index in indexes:
        position += len(text[previous:index].encode("utf-16-le", errors="surrogatepass")) // 2
        result[index] = position
        previous = index
    return result


def qt_position_to_text_index(text: str, position: int) -> int:
    """Convert a Qt position, snapping a split surrogate pair to its start."""
    position = max(0, int(position))
    consumed = 0
    for index, character in enumerate(text):
        units = 2 if ord(character) > 0xFFFF else 1
        if consumed + units > position:
            return index
        consumed += units
    return len(text)
