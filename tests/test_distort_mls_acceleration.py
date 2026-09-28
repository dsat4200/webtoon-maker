"""Pin-warp optimization preserves coordinates and finished pixels exactly."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import DistortModifier
from comic_editor.ui import distort_rendering as rendering
from test_effect_regions import image


def reference(query, source, destination, mode="rigid"):
    # Frozen implementation, deliberately retaining its array reductions.
    if len(source) == 0:
        return query.copy()
    if len(source) == 1:
        return query + destination[0] - source[0]
    shape = query.shape
    query = query.reshape(-1, 2)
    total = np.zeros(len(query))
    pstar, qstar = np.zeros_like(query), np.zeros_like(query)
    for p, q in zip(source, destination):
        weights = 1. / np.maximum(np.sum((query - p) ** 2, axis=1), 1e-10)
        total += weights
        pstar += weights[:, None] * p
        qstar += weights[:, None] * q
    pstar /= total[:, None]
    qstar /= total[:, None]
    real, imag, denominator = np.zeros(len(query)), np.zeros(len(query)), np.zeros(len(query))
    for p, q in zip(source, destination):
        weights = 1. / np.maximum(np.sum((query - p) ** 2, axis=1), 1e-10)
        pp, qq = p - pstar, q - qstar
        real += weights * np.sum(pp * qq, axis=1)
        imag += weights * (pp[:, 0] * qq[:, 1] - pp[:, 1] * qq[:, 0])
        denominator += weights * np.sum(pp * pp, axis=1)
    normal = denominator if mode == "similarity" else np.hypot(real, imag)
    real = np.divide(real, normal, out=np.ones_like(real), where=normal > 1e-14)
    imag = np.divide(imag, normal, out=np.zeros_like(imag), where=normal > 1e-14)
    delta = query - pstar
    result = qstar + np.stack((real * delta[:, 0] - imag * delta[:, 1],
                              imag * delta[:, 0] + real * delta[:, 1]), axis=-1)
    return result.reshape(shape)


@pytest.mark.parametrize("mode", ["rigid", "similarity"])
@pytest.mark.parametrize("count", [0, 1, 2, 13])
@pytest.mark.parametrize("types", [(np.float64, np.float64, np.float64),
                                  (np.float32, np.float64, np.float64),
                                  (np.float32, np.float32, np.float64)])
def test_pin_coordinates_are_identical(mode, count, types):
    rng = np.random.default_rng(3281)
    query = (rng.random((27, 31, 2)) * 1000 - 100).astype(types[0])
    source = (rng.random((count, 2)) * 800).astype(types[1])
    destination = (source + rng.random((count, 2)) * 100 - 50).astype(types[2])
    if count:
        query[0, 0] = source[0]
    np.testing.assert_array_equal(rendering._mls(query, source, destination, mode),
                                  reference(query, source, destination, mode))


@pytest.mark.parametrize("mode", ["rigid", "similarity"])
def test_finished_deform_pixels_are_identical(qapp, monkeypatch, mode):
    modifier = DistortModifier(modifier_type="distort_deform", frame=(0, 0, 320, 240),
        source_points=[(0, 0), (1, 0), (0, 1), (1, 1), (.3, .4), (.8, .6)],
        points=[(0, 0), (1, 0), (0, 1), (1, 1), (.1, .6), (.85, .4)],
        parameters={"mode": mode})
    source = image(320, 240)
    bounds = QRectF(-20, -30, 320, 240)
    mapping = QTransform().translate(40, 50).rotate(17).scale(1.2, .8)
    actual = rendering.render_distort(source, bounds, modifier, mapping, bounds)
    monkeypatch.setattr(rendering, "_mls", reference)
    assert actual == rendering.render_distort(source, bounds, modifier, mapping, bounds)


def test_list_pins_and_coincident_constraints_keep_previous_behavior():
    query = np.array([[[0., 0.], [1., 4.]], [[-.5, 2.], [8., 12.]]])[:, ::-1]
    source = [[0., 0.], [0., 0.], [1., 1.]]
    destination = [[0., 1.], [2., 1.], [4., 5.]]
    for mode in ("rigid", "similarity"):
        np.testing.assert_array_equal(rendering._mls(query, source, destination, mode),
                                      reference(query, source, destination, mode))


def test_large_pin_sets_recompute_weights_when_retaining_them_exceeds_budget(monkeypatch):
    monkeypatch.setattr(rendering, "_MLS_WEIGHT_CACHE_LIMIT", 0)
    query = np.array([[[1., 2.], [3., 4.]]])
    source = np.array([[0., 0.], [5., 0.], [0., 5.]])
    destination = source + np.array([2., -1.])
    np.testing.assert_array_equal(rendering._mls(query, source, destination),
                                  reference(query, source, destination))
