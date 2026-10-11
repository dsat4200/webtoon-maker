"""Offscreen owners must follow the supplied display, before native creation."""
import importlib

import numpy as np
import pytest
from PySide6.QtGui import QOpenGLContext, QSurfaceFormat


MODULES = (
    ('comic_editor.render.gpu.point_chain', 'GpuPointChain'),
    ('comic_editor.ui.gpu_textures', 'GpuTextureRenderer'),
    ('comic_editor.ui.gpu_pattern_effects', 'GpuPatternRenderer'),
)


@pytest.fixture
def displays(qapp):
    # Distinct displays are needed even on CI with only one physical screen.
    # Native creation rejects the wrong adapter rather than silently accepting
    # the primary display and making the assertion tautological.
    primary, target = object(), object()
    events = []
    fmt = QSurfaceFormat()
    fmt.setVersion(4, 3)

    class Surface:
        def __init__(self, screen=primary):
            self.display = screen

        def screen(self):
            return self.display

        def setScreen(self, screen):
            self.display = screen
            events.append(('surface_screen', screen))

        def setFormat(self, value):
            pass

        def format(self):
            return fmt

        def create(self):
            events.append(('surface_create', self.display))

        def isValid(self):
            return self.display is target

        def destroy(self):
            events.append(('surface_destroy', self.display))

    class Context:
        def __init__(self):
            self.display = primary
            self.created = False

        @staticmethod
        def supportsThreadedOpenGL():
            return True

        @staticmethod
        def currentContext():
            return None

        def screen(self):
            return target if self.created else self.display

        def setScreen(self, screen):
            assert not self.created, 'An existing context cannot change adapters'
            self.display = screen
            events.append(('context_screen', screen))

        def setFormat(self, value):
            pass

        def format(self):
            return fmt

        def setShareContext(self, value):
            events.append(('share', value))

        def create(self):
            events.append(('context_create', self.display))
            self.created = self.display is target
            return self.created

        def makeCurrent(self, surface):
            events.append(('current', surface.screen()))
            # Stop before driver calls: this check covers initialization and
            # ordinary activation failure, not fake shader rendering.
            return False

        def doneCurrent(self):
            pass

        def moveToThread(self, thread):
            events.append(('move', thread))

    return primary, target, events, Surface, Context


@pytest.mark.parametrize('module_name,class_name', MODULES)
def test_supplied_secondary_surface_selects_context_before_creation(
        qapp, monkeypatch, displays, module_name, class_name):
    primary, target, events, Surface, Context = displays
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, 'QOpenGLContext', Context)
    renderer = getattr(module, class_name)(surface=Surface(target))
    assert ('context_create', target) in events
    assert ('context_create', primary) not in events
    assert events.index(('context_screen', target)) < events.index(('context_create', target))
    assert ('current', target) in events
    assert not renderer.available
    renderer.close()


@pytest.mark.parametrize('module_name,class_name', MODULES)
def test_owned_surface_uses_created_context_display(
        qapp, monkeypatch, displays, module_name, class_name):
    _primary, target, events, Surface, Context = displays
    # Simulate a driver selecting a display different from Qt's primary screen.
    def create(context):
        context.created = True
        events.append(('context_create', target))
        return True
    monkeypatch.setattr(Context, 'create', create)
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, 'QOpenGLContext', Context)
    monkeypatch.setattr(module, 'QOffscreenSurface', Surface)
    renderer = getattr(module, class_name)()
    assert ('surface_create', target) in events
    assert events.index(('surface_screen', target)) < events.index(('surface_create', target))
    assert not renderer.available
    renderer.close()


def test_point_chain_uses_share_display_before_creation(qapp, monkeypatch, displays):
    from comic_editor.render.gpu import point_chain
    _primary, target, events, Surface, Context = displays
    monkeypatch.setattr(point_chain, 'QOpenGLContext', Context)
    monkeypatch.setattr(point_chain, 'QOffscreenSurface', Surface)
    shared = Context()
    shared.created = True
    renderer = point_chain.GpuPointChain(share_context=shared)
    assert ('context_create', target) in events
    assert ('surface_create', target) in events
    renderer.close()


def test_prepared_context_is_never_reconfigured(qapp, monkeypatch, displays):
    from comic_editor.render.gpu import point_chain
    _primary, target, events, Surface, Context = displays
    monkeypatch.setattr(point_chain, 'QOpenGLContext', Context)
    prepared = Context()
    prepared.created = True
    renderer = point_chain.GpuPointChain(context=prepared, surface=Surface(target))
    assert not any(event[0] in ('context_screen', 'context_create', 'share') for event in events)
    assert ('current', target) in events
    renderer.close()


def test_worker_prepares_secondary_surface_and_shared_context_on_gui(
        qapp, monkeypatch, displays):
    from comic_editor.render.gpu import worker
    _primary, target, events, Surface, Context = displays
    class Signal:
        def connect(self, callback):
            pass
    class Thread:
        def __init__(self, service):
            self.finished = Signal()
        def start(self):
            events.append(('start', target))
    monkeypatch.setattr(worker, 'QOpenGLContext', Context)
    monkeypatch.setattr(worker, 'QOffscreenSurface', Surface)
    monkeypatch.setattr(worker, '_GraphicsThread', Thread)
    shared = Context()
    shared.created = True
    service = worker.GpuWorker(share_context=shared)
    assert service.surface.screen() is target
    assert service.prepared_context.screen() is target
    assert events.index(('surface_create', target)) < events.index(('context_create', target))
    assert events.index(('context_screen', target)) < events.index(('context_create', target))
    assert events[-1] == ('start', target)


def test_native_shared_owner_keeps_display_and_exact_point_pixels(qapp):
    from comic_editor.render.gpu.point_chain import GpuPointChain
    from comic_editor.render.gpu.worker import GpuWorker
    from test_gpu_worker import palette, wait_until
    renderer = GpuPointChain()
    if not renderer.available:
        reason = renderer.reason
        renderer.close()
        pytest.skip(reason)
    service = GpuWorker(share_context=renderer.context)
    try:
        wait_until(qapp, service.ready.is_set)
        assert service.available, service.reason
        assert service.surface.screen() is renderer.context.screen()
        assert service.render_context.screen() is renderer.context.screen()
        assert QOpenGLContext.areSharing(renderer.context, service.render_context)
        pixels = np.array([[[.4, .2, 0., .8]], [[.6, .4, .2, 1.]]], np.float32)
        future = service.submit_lut(pixels, palette(), source_key='native-affinity', palette_key='identity')
        assert future is not None
        wait_until(qapp, lambda: future.done() and service.queued_bytes == 0)
        np.testing.assert_array_equal(future.result(), pixels)
    finally:
        service.close()
        renderer.close()
    assert service.surface is None and not service.worker_thread.isRunning()
