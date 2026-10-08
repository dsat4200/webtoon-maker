"""Detached local GL helpers have explicit lifetime, with native output intact."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QRectF
from PySide6.QtGui import QColor, QImage, QTransform
from PySide6.QtWidgets import QWidget

from comic_editor.core.cage import CageGrid
from comic_editor.core.models import BoundGeometry, ChapterDocument, HalftoneModifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.pixels import FLOAT_PIXELS, pixel_scope
from comic_editor.render.scene import DetachedSceneBackend, SceneSnapshotCompiler
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def backend(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='gpu'))
    chapter = ChapterDocument(width=64, height=48, document_kind='asset')
    chapter.add_page('Local helper', BoundGeometry.rectangle(0, 0, 64, 48))
    canvas.set_document(chapter, TileStore())
    canvas.setUpdatesEnabled(False)
    capture = SceneSnapshotCompiler().capture(canvas, canvas._render_document_state())
    while not capture.advance(.001):
        pass
    assert not capture.stale and capture.result is not None
    result = DetachedSceneBackend(capture.result)
    assert not isinstance(result.scene, QObject)
    yield result
    result.close()
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.close()
    canvas.deleteLater()


def module_for(kind):
    if kind == 'pattern':
        from comic_editor.ui import gpu_pattern_effects
        return gpu_pattern_effects, 'GpuPatternRenderer', '_gpu_pattern_renderer'
    from comic_editor.ui import gpu_textures
    return gpu_textures, 'GpuTextureRenderer', '_gpu_texture_renderer'


@pytest.mark.parametrize('kind', ['pattern', 'texture'])
def test_real_nonwidget_factory_reuses_and_retires_local_renderer(backend, kind):
    module, _class, attribute = module_for(kind)
    result = module.renderer_for(backend.scene)
    local = getattr(backend.scene, attribute)
    assert local is not None
    assert result is local if local.available else result is None
    assert module.renderer_for(backend.scene) is result
    backend.close()
    assert attribute not in backend.scene.__dict__
    assert local.context is None and local.surface is None and not local.available
    backend.close()


@pytest.mark.parametrize('kind', ['pattern', 'texture'])
def test_nonwidget_local_native_pixels_match_fresh_independent_helper(backend, kind):
    module, class_name, attribute = module_for(kind)
    local = module.renderer_for(backend.scene)
    if local is None:
        pytest.skip(getattr(backend.scene, attribute).reason)
    reference = getattr(module, class_name)()
    if not reference.available:
        reference.close()
        pytest.skip(reference.reason)
    source = QImage(64, 48, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(90, 40, 170, 177))
    original = bytes(source.constBits())
    try:
        if kind == 'pattern':
            modifier = HalftoneModifier(base_resolution=100, spacing=12, blur=0,
                grid_type='square', dot_style='circle', transparent_background=True)
            actual, expected = local.render(source, modifier), reference.render(source, modifier)
        else:
            bounds = QRectF(0, 0, 64, 48)
            grid = CageGrid(frame=(0, 0, 64, 48), interpolation='bilinear')
            grid.validate_grid()
            grid.points = [(x+2.25, y+1.75) for x, y in grid.points]
            target = QRectF(-2, -2, 70, 54)
            actual = local.cage(source, bounds, grid, QTransform(), target)
            expected = reference.cage(source, bounds, grid, QTransform(), target)
        assert actual is not None and expected is not None
        assert not actual.isNull() and not expected.isNull()
        assert actual.size() == expected.size() and actual.format() == expected.format()
        assert bytes(actual.constBits()) == bytes(expected.constBits())
        assert any(bytes(actual.constBits()))
        assert bytes(source.constBits()) == original
        retained = QImage(actual)
        backend.close()
        assert bytes(retained.constBits()) == bytes(expected.constBits())
    finally:
        reference.close()


@pytest.mark.parametrize('failed', ['pattern', 'texture'])
def test_cleanup_failure_still_retires_other_local_helper_and_backing(backend, failed):
    calls = []

    class Local:
        def __init__(self, name):
            self.name = name
        def close(self):
            calls.append(self.name)
            if self.name == failed:
                raise RuntimeError('local cleanup '+failed)

    backend.scene._gpu_pattern_renderer = Local('pattern')
    backend.scene._gpu_texture_renderer = Local('texture')
    backend.scene._persistent_render_cache = SimpleNamespace(close=lambda: calls.append('backing'))
    backend.scene._graphics_worker = SimpleNamespace(close=lambda: pytest.fail('Borrowed graphics owner was closed'))
    with pytest.raises(RuntimeError, match='local cleanup '+failed):
        backend.close()
    assert calls == ['pattern', 'texture', 'backing']
    assert '_gpu_pattern_renderer' not in backend.scene.__dict__
    assert '_gpu_texture_renderer' not in backend.scene.__dict__
    backend.scene._persistent_render_cache = None
    backend.close()
    assert calls == ['pattern', 'texture', 'backing']


@pytest.mark.parametrize('kind', ['pattern', 'texture'])
def test_actual_widget_destroyed_signal_keeps_original_helper_lifetime(qapp, monkeypatch, kind):
    import shiboken6
    module, class_name, attribute = module_for(kind)
    calls = []

    class Local:
        available = False
        def close(self):
            calls.append('close')

    monkeypatch.setattr(module, class_name, Local)
    widget = QWidget()
    widget.settings = EditorSettings(canvas_renderer='gpu')
    assert module.renderer_for(widget) is None
    assert getattr(widget, attribute) is not None
    shiboken6.delete(widget)
    assert calls == ['close']


@pytest.mark.parametrize('kind', ['pattern', 'texture'])
@pytest.mark.parametrize('precision', ['float16', 'float32'])
def test_float_policy_preserves_fallback_without_allocating_local_helper(backend, kind, precision):
    module, _class_name, attribute = module_for(kind)
    contract = replace(FLOAT_PIXELS, precision=precision)
    with pixel_scope(contract):
        assert module.renderer_for(backend.scene) is None
    assert attribute not in backend.scene.__dict__
