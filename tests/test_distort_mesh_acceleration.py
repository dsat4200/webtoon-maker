"""Spatial rejection retains the exact mesh mapping, including overlaps."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import DistortModifier
from comic_editor.ui import distort_rendering as rendering
from test_effect_regions import image


def reference(query, source, destination, faces):
    # Preserved exhaustive implementation is the oracle for the new index.
    result = np.full_like(query, np.nan)
    flat, output = query.reshape(-1, 2), result.reshape(-1, 2)
    qlow, qhigh = flat.min(axis=0), flat.max(axis=0)
    for face in faces:
        tri = destination[face]
        low, high = tri.min(axis=0), tri.max(axis=0)
        if np.any(high < qlow) or np.any(low > qhigh):
            continue
        selected = np.flatnonzero(np.all((flat >= low - 1e-8) & (flat <= high + 1e-8), axis=1))
        if not len(selected):
            continue
        matrix = np.column_stack((tri[1] - tri[0], tri[2] - tri[0]))
        if abs(np.linalg.det(matrix)) < 1e-12:
            continue
        uv = (flat[selected] - tri[0]) @ np.linalg.inv(matrix).T
        inside = (uv[:, 0] >= -1e-8) & (uv[:, 1] >= -1e-8) & (uv.sum(axis=1) <= 1. + 1e-8)
        weights, src = uv[inside], source[face]
        output[selected[inside]] = src[0] + weights[:, :1] * (src[1] - src[0]) + weights[:, 1:] * (src[2] - src[0])
    return result


def mesh():
    modifier = DistortModifier(modifier_type="distort_mesh_warp", frame=(0, 0, 200, 200),
                              parameters={"rows": 4, "columns": 4, "smoothness": 50})
    points = np.stack(np.meshgrid(np.linspace(0, 1, 4), np.linspace(0, 1, 4)), axis=-1).reshape(-1, 2)
    points[5] += (.25, -.15)
    points[10] += (-.35, .3)
    modifier.points = points.tolist()
    return modifier, rendering._mesh(modifier, np.array(modifier.frame), modifier.parameters)


@pytest.mark.parametrize("projective", [False, True])
@pytest.mark.parametrize("overlap", [False, True])
def test_grid_rejection_matches_exhaustive_faces(projective, overlap):
    _, (source, destination, faces) = mesh()
    if overlap:
        faces = np.concatenate((faces, faces[:20, ::-1], [[0, 0, 0]]))
    xx, yy = np.meshgrid(np.linspace(-12, 212, 121), np.linspace(-20, 230, 99))
    query = np.stack((xx, yy), axis=-1)
    if projective:
        query = rendering._transform(QTransform(1, .1, .0003, -.07, 1, -.0002, 8, -4, 1), query)
    actual = rendering._triangle_map(query, source, destination, faces)
    expected = reference(query, source, destination, faces)
    np.testing.assert_array_equal(actual, expected)


def test_finished_pixels_identical_to_exhaustive_mesh(qapp, monkeypatch):
    modifier, _ = mesh()
    source = image(240, 220)
    bounds = QRectF(0, 0, 240, 220)
    actual = rendering.render_distort(source, bounds, modifier, QTransform(), bounds)
    monkeypatch.setattr(rendering, "_triangle_map", reference)
    expected = rendering.render_distort(source, bounds, modifier, QTransform(), bounds)
    assert actual == expected


@pytest.mark.parametrize("offset", [(30., 40.), (-300., 400.), (180., 195.)])
def test_small_regions_preserve_face_rejection_and_overlap_order(offset):
    _, (source, destination, faces) = mesh()
    faces = np.concatenate((faces, faces[:20, ::-1], [[0, 0, 0]]))
    xx, yy = np.meshgrid(np.linspace(0, 12, 80), np.linspace(0, 9, 70))
    query = np.stack((xx, yy), axis=-1) + offset
    np.testing.assert_array_equal(rendering._triangle_map(query, source, destination, faces),
                                  reference(query, source, destination, faces))


def test_empty_mesh_has_transparent_unmapped_coordinates():
    _, (source, destination, faces) = mesh()
    query = np.zeros((2, 3, 2))
    assert np.isnan(rendering._triangle_map(query, source, destination, faces[:0])).all()
