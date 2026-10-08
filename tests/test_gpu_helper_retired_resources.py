"""Real C++-deleted GL operands with valid local contexts retire safely."""
import importlib

import pytest
from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLTexture, QOpenGLVertexArrayObject
from shiboken6 import delete, isValid

KINDS = [
    ('texture', 'comic_editor.ui.gpu_textures', 'GpuTextureRenderer',
     ('texture', 'buffer', 'vao'),
     ('framebuffer', 'texture', 'program', 'buffer', 'vao', 'functions')),
    ('pattern', 'comic_editor.ui.gpu_pattern_effects', 'GpuPatternRenderer',
     ('source', 'gradient', 'color_source_texture', 'mask', 'buffer', 'triangle_buffer', 'vao'),
     ('source', 'gradient', 'color_source_texture', 'mask', 'output', 'blur_x', 'blur_y',
      'triangle_output', 'program', 'blur_program', 'triangle_program', 'buffer',
      'triangle_buffer', 'vao', 'functions')),
]
CASES = [(kind, module, name, resources, fields, victim)
    for kind, module, name, resources, fields in KINDS for victim in resources]


def native_context():
    format = QSurfaceFormat()
    format.setVersion(3, 3)
    format.setProfile(QSurfaceFormat.CoreProfile)
    context = QOpenGLContext()
    context.setFormat(format)
    assert context.create(), 'Actual native OpenGL context creation required'
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    assert surface.isValid() and context.isValid()
    return context, surface


@pytest.mark.parametrize('kind,module,name,resource_fields,fields,victim_field', CASES,
    ids=[kind+'-'+victim for kind, _module, _name, _resources, _fields, victim in CASES])
def test_cpp_deleted_resource_is_skipped_with_valid_context_and_other_resources_retired(
        qapp, kind, module, name, resource_fields, fields, victim_field):
    before = QOpenGLContext.currentContext()
    before_surface = before.surface() if before is not None and isValid(before) else None
    context = surface = previous = previous_surface = None
    renderer = None
    resources, calls = {}, []
    try:
        context, surface = native_context()
        previous, previous_surface = native_context()
        assert context.makeCurrent(surface)
        renderer_type = getattr(importlib.import_module(module), name)
        renderer = renderer_type.__new__(renderer_type)
        renderer.context, renderer.surface = context, surface
        renderer.available = True
        renderer.triangle_key = None
        for field in fields:
            setattr(renderer, field, None)
        for field in resource_fields:
            base = (QOpenGLVertexArrayObject if field == 'vao' else
                    QOpenGLBuffer if field in {'buffer', 'triangle_buffer'} else QOpenGLTexture)
            def destroy(self, label=field, base_type=base):
                calls.append((label, isValid(self)))
                assert isValid(self), 'Invalid C++ operand reached a native destroy method'
                return base_type.destroy(self)
            watched = type('Observed'+field, (base,), {'destroy': destroy})
            resource = (watched(QOpenGLBuffer.VertexBuffer) if base is QOpenGLBuffer else
                        watched(QOpenGLTexture.Target2D) if base is QOpenGLTexture else watched())
            assert resource.create(), 'Real GL resource creation required'
            assert isValid(resource) and resource.isCreated()
            resources[field] = resource
            setattr(renderer, field, resource)
        victim = resources[victim_field]
        delete(victim)
        assert not isValid(victim)
        assert isValid(context) and isValid(surface) and context.isValid() and surface.isValid()
        context.doneCurrent()
        assert previous.makeCurrent(previous_surface)
        assert QOpenGLContext.currentContext() is previous
        renderer.close()
        assert renderer.context is None and renderer.surface is None and not renderer.available
        assert all(getattr(renderer, field) is None for field in fields)
        assert calls == [(field, True) for field in resource_fields if field != victim_field]
        assert QOpenGLContext.currentContext() is previous
        assert previous.surface() is previous_surface
        assert isValid(context) and isValid(surface)
        assert all(not resource.isCreated() for field, resource in resources.items() if field != victim_field)
        renderer.close()
        assert calls == [(field, True) for field in resource_fields if field != victim_field]
        assert renderer.context is None and renderer.surface is None
        assert all(getattr(renderer, field) is None for field in fields)
        assert QOpenGLContext.currentContext() is previous
    finally:
        if renderer is not None:
            renderer.close()
        if context is not None and surface is not None and isValid(context) and isValid(surface):
            context.makeCurrent(surface)
            for resource in resources.values():
                if isValid(resource):
                    delete(resource)
            context.doneCurrent()
        if previous is not None and isValid(previous):
            previous.doneCurrent()
        for obj in (context, surface, previous, previous_surface):
            if obj is not None and isValid(obj):
                delete(obj)
        if before is not None and before_surface is not None and isValid(before) and isValid(before_surface):
            assert before.makeCurrent(before_surface)
