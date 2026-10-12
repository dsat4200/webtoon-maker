"""Viewport bookkeeping reuses immutable native grids without changing culling."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect, QRectF
from PySide6.QtGui import QTransform

from comic_editor.render.projection import DocumentProjection
from comic_editor.ui.document_projection_features import DocumentProjectionFeatures


class View(DocumentProjectionFeatures):
    scale = .5263217797814006
    ratio = 1.
    viewport = QRect(0, 0, 1042, 937)

    def __init__(self):
        self.camera = QTransform.fromTranslate(-350., -5000.)
        self.camera.scale(self.scale, self.scale)

    def camera_transform(self):
        return QTransform(self.camera)

    def rect(self):
        return QRect(self.viewport)

    def devicePixelRatioF(self):
        return self.ratio


def test_guard_window_reuses_native_requests_but_rejects_changed_grid(monkeypatch):
    view, projection = View(), DocumentProjection()
    coverage = QRectF(-256., 8192., 3072., 2560.)
    calls, ordinary = [], projection.requests

    def observed(rect, density):
        calls.append((tuple(rect.getRect()), density))
        return ordinary(rect, density)

    monkeypatch.setattr(projection, 'requests', observed)
    first = view._projection_requests_for_window(projection, coverage)
    assert first is view._projection_requests_for_window(projection, QRectF(coverage))
    assert len(calls) == 1 and calls[0][1] == 1.
    # Returned Qt values cannot mutate the cached frozen native request.
    rectangle = first[0].capture_rect
    expected = tuple(rectangle.getRect())
    rectangle.translate(100., 100.)
    assert tuple(first[0].capture_rect.getRect()) == expected
    assert all(request.scale == 1. and request.pixel_size == 260 for request in first)
    for changed in (coverage.translated(256., 0.), coverage):
        assert view._projection_requests_for_window(projection, changed) == tuple(ordinary(changed, 1.))
    projection.gutter = 3
    changed = view._projection_requests_for_window(projection, coverage)
    assert changed is not first and all(request.pixel_size == 262 for request in changed)
    assert len(calls) == 4


@pytest.mark.parametrize('angle', [0., -.663008407605119, -38.])
@pytest.mark.parametrize('ratio', [1., 1.25, 2.])
def test_immutable_cull_matches_original_qt_path_and_reuses_unchanged_view(monkeypatch, angle, ratio):
    import comic_editor.ui.document_projection_features as projection_ui
    view, projection = View(), DocumentProjection()
    view.ratio = ratio
    view.camera.rotate(angle)
    requests = view._projection_requests_for_window(projection, QRectF(-4096., 0., 16384., 16384.))
    calls, path = [], projection_ui.QPainterPath
    monkeypatch.setattr(projection_ui, 'QPainterPath', lambda: (calls.append(True), path())[1])
    result = view._projection_visible_requests(requests, 256.)
    count = len(calls)
    assert result is view._projection_visible_requests(requests, 256.)
    assert len(calls) == count
    assert result == tuple(view._projection_visible_requests(list(requests), 256.))
    for change in (
            lambda: view.camera.translate(.25, .5),
            lambda: setattr(view, 'ratio', ratio + .25),
            lambda: setattr(view, 'viewport', QRect(0, 0, 900, 700)),
            lambda: setattr(view, 'scale', view.scale + .01)):
        change()
        current = view._projection_visible_requests(requests, 256.)
        assert current == tuple(view._projection_visible_requests(list(requests), 256.))
        assert current is view._projection_visible_requests(requests, 256.)
    assert view._projection_visible_requests(requests, 257.) is not current


def test_foreign_mutable_request_metadata_always_uses_qt():
    view = View()
    request = SimpleNamespace(capture_rect=QRectF(512., 9500., 256., 256.))
    requests = (request,)
    first = view._projection_visible_requests(requests, 256.)
    request.capture_rect = QRectF(1e8, 1e8, 256., 256.)
    assert first and view._projection_visible_requests(requests, 256.) == []


def test_singular_camera_keeps_original_request_fallback():
    view = View()
    view.camera.scale(0., 0.)
    requests = tuple(DocumentProjection().requests(QRectF(0., 0., 512., 512.), 1.))
    assert view._projection_visible_requests(requests, 256.) is requests
