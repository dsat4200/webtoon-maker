"""Hue and grayscale statistics and palette construction for Posterize."""
from __future__ import annotations

import numpy as np
from comic_editor.core.models import (
    POSTERIZE_MAX_COLORS, POSTERIZE_MIN_SPAN, POSTERIZE_VALUE_MIN_SPAN, PosterizeRange,
)


def rgb_hues(rgb: np.ndarray) -> np.ndarray:
    """HSV hue in degrees; achromatic pixels have the conventional hue zero."""
    maximum, minimum = rgb.max(axis=-1), rgb.min(axis=-1)
    delta = maximum - minimum
    denominator = np.where(delta > 1e-7, delta, 1.)
    r, g, b = np.moveaxis(rgb, -1, 0)
    hue = np.where(maximum == r, (g - b) / denominator,
                   np.where(maximum == g, (b - r) / denominator + 2.,
                            (r - g) / denominator + 4.))
    return np.where(delta > 1e-7, hue * 60. % 360., 0.)


def range_indices(hues, ranges):
    return (np.searchsorted([item.start for item in ranges], hues, side="right") - 1) % len(ranges)


def rgb_values(rgb: np.ndarray) -> np.ndarray:
    """8-bit grayscale brightness using display-referred Rec. 709 luma."""
    return np.rint(np.clip(np.sum(rgb * np.array([.2126, .7152, .0722]), axis=-1), 0., 1.) * 255.)


def grayscale_source(rgba: np.ndarray) -> np.ndarray:
    result = rgba.copy()
    result[..., :3] = (rgb_values(rgba[..., :3]) / 255.)[..., None]
    return result


def value_range_indices(values, ranges):
    return np.clip(np.searchsorted([item.start for item in ranges], values, side="right") - 1,
                   0, len(ranges) - 1)


class ValueStatistics:
    """A black-to-white histogram, weighted by alpha and sampled area."""

    def __init__(self):
        self.counts = np.zeros(256, dtype=np.float64)

    def add(self, rgba: np.ndarray, area_weight=1.) -> None:
        pixels = rgba.reshape(-1, 4)
        pixels = pixels[pixels[:, 3] > 0]
        if len(pixels):
            bins = rgb_values(pixels[:, :3]).astype(int)
            self.counts += np.bincount(bins, weights=pixels[:, 3] * area_weight, minlength=256)

    def average(self, start, span, fallback="#FF808080") -> str:
        values = np.arange(256)
        selected = (values >= start) & (values < start + span)
        weight = self.counts[selected].sum()
        if not weight:
            return fallback
        value = int(np.rint(np.dot(values[selected], self.counts[selected]) / weight))
        return "#FF" + f"{value:02X}" * 3

    def initialize(self, count: int) -> list[PosterizeRange]:
        if not 1 <= count <= POSTERIZE_MAX_COLORS:
            raise ValueError(f"Choose 1 to {POSTERIZE_MAX_COLORS} colors")
        occupied = np.flatnonzero(self.counts)
        proposals = []
        if len(occupied) > 1:
            values, weights = occupied.astype(float), self.counts[occupied]
            centers = [values[int(np.argmax(weights))]]
            for _ in range(1, min(count, len(values))):
                distance = np.min((values[:, None] - np.array(centers)) ** 2, axis=1)
                centers.append(values[int(np.argmax(distance * weights))])
            centers = np.sort(centers)
            for _ in range(32):
                labels = np.argmin((values[:, None] - centers) ** 2, axis=1)
                updated = np.array([np.average(values[labels == i], weights=weights[labels == i])
                                    if np.any(labels == i) else center
                                    for i, center in enumerate(centers)])
                if np.max(np.abs(updated - centers)) < 1e-5:
                    break
                centers = updated
            proposals = list((centers[:-1] + centers[1:]) / 2.)
        while len(proposals) < count - 1:
            edges = [0., *sorted(proposals), 256.]
            index = int(np.argmax(np.diff(edges)))
            proposals.append((edges[index] + edges[index + 1]) / 2.)
        boundaries = [0.]
        for i, proposal in enumerate(sorted(proposals), 1):
            boundaries.append(float(np.clip(proposal if len(occupied) else 256. * i / count,
                boundaries[-1] + POSTERIZE_VALUE_MIN_SPAN,
                256. - (count - i) * POSTERIZE_VALUE_MIN_SPAN)))
        fallback = self.average(0., 256.)
        return [PosterizeRange(start, self.average(start,
                    (boundaries[i + 1] if i + 1 < count else 256.) - start, fallback))
                for i, start in enumerate(boundaries)]


class HueStatistics:
    """360 angular bins, weighted by alpha and sampled document area."""

    def __init__(self):
        self.counts = np.zeros(360, dtype=np.float64)
        self.rgb_sums = np.zeros((360, 3), dtype=np.float64)

    def add(self, rgba: np.ndarray, area_weight=1.) -> None:
        pixels = rgba.reshape(-1, 4)
        pixels = pixels[pixels[:, 3] > 0]
        if not len(pixels):
            return
        bins = np.floor(rgb_hues(pixels[:, :3])).astype(int) % 360
        weights = pixels[:, 3] * area_weight
        self.counts += np.bincount(bins, weights=weights, minlength=360)
        for channel in range(3):
            self.rgb_sums[:, channel] += np.bincount(
                bins, weights=weights * pixels[:, channel], minlength=360)

    def average(self, start, span, fallback="#FF808080") -> str:
        selected = ((np.arange(360) - start) % 360.) < span
        weight = self.counts[selected].sum()
        if weight <= 0:
            return fallback
        rgb = np.clip(np.rint(self.rgb_sums[selected].sum(axis=0) / weight * 255), 0, 255).astype(int)
        return "#FF" + "".join(f"{value:02X}" for value in rgb)

    def initialize(self, count: int) -> list[PosterizeRange]:
        if not 1 <= count <= POSTERIZE_MAX_COLORS:
            raise ValueError(f"Choose 1 to {POSTERIZE_MAX_COLORS} colors")
        occupied = np.flatnonzero(self.counts)
        # Put the seam in the largest empty gap so nearby reds stay together.
        if len(occupied):
            gaps = (np.roll(occupied, -1) - occupied) % 360
            index = int(np.argmax(gaps))
            gap = float(gaps[index]) if len(occupied) > 1 else 360.
            start = float((occupied[index] + gap / 2.) % 360.)
        else:
            start = 0.
        offsets = (np.arange(360) - start) % 360.
        proposals = []
        if len(occupied) > 1:
            # Weighted 1D clustering keeps rare, distinct hues represented even
            # when one color covers most of the artwork. Initialization is
            # deterministic, and intervals remain contiguous on the circle.
            values, weights = offsets[occupied], self.counts[occupied]
            centers = [values[int(np.argmax(weights))]]
            for _ in range(1, min(count, len(occupied))):
                distance = np.min((values[:, None] - np.array(centers)) ** 2, axis=1)
                centers.append(values[int(np.argmax(distance * weights))])
            centers = np.sort(centers)
            for _ in range(32):
                labels = np.argmin((values[:, None] - centers) ** 2, axis=1)
                updated = np.array([np.average(values[labels == i], weights=weights[labels == i])
                                    if np.any(labels == i) else center
                                    for i, center in enumerate(centers)])
                if np.max(np.abs(updated - centers)) < 1e-5:
                    break
                centers = updated
            proposals = list((centers[:-1] + centers[1:]) / 2.)
        # With fewer distinct hues than requested, fill the remaining gaps;
        # empty intervals inherit the artwork's average instead of invented hues.
        while len(proposals) < count - 1:
            edges = [0., *sorted(proposals), 360.]
            index = int(np.argmax(np.diff(edges)))
            proposals.append((edges[index] + edges[index + 1]) / 2.)
        proposals.sort()
        boundaries = [0.]
        for i in range(1, count):
            proposal = proposals[i - 1] if len(occupied) else 360. * i / count
            boundaries.append(float(np.clip(proposal,
                boundaries[-1] + POSTERIZE_MIN_SPAN,
                360. - (count - i) * POSTERIZE_MIN_SPAN)))
        fallback = self.average(0., 360.)
        ranges = []
        for i, boundary in enumerate(boundaries):
            end = boundaries[i + 1] if i + 1 < count else 360.
            hue = (start + boundary) % 360.
            ranges.append(PosterizeRange(hue, self.average(hue, end - boundary, fallback)))
        return sorted(ranges, key=lambda item: item.start)
