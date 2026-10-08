"""Deleted C++ Qt context/surface wrappers retire helper guards safely."""
import importlib
import weakref

import pytest
from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
from shiboken6 import delete, isValid


KINDS = [
    ('comic_editor.ui.gpu_textures', 'GpuTextureRenderer',
     ('framebuffer', 'texture', 'program', 'buffer', 'vao', 'functions')),
    ('comic_editor.ui.gpu_pattern_effects', 'GpuPatternRenderer',
     ('source', 'gradient', 'color_source_texture', 'mask', 'output', 'blur_x',
      'blur_y', 'triangle_output', 'program', 'blur_program', 'triangle_program',
      'buffer', 'triangle_buffer', 'vao', 'functions')),
]


@pytest.mark.parametrize('module,name,fields', KINDS)
@pytest.mark.parametrize('deleted', ['context', 'surface'])
def test_actual_cpp_deleted_wrapper_retires_helper_without_gl_calls(qapp, module, name, fields, deleted):
    renderer_type = getattr(importlib.import_module(module), name)
    renderer = renderer_type.__new__(renderer_type)
    context, surface = QOpenGLContext(), QOffscreenSurface()
    renderer.context, renderer.surface = context, surface
    renderer.available = True
    renderer.triangle_key = None
    retired = []
    class Guard:
        def destroy(self):
            pytest.fail('An invalid context/surface must not destroy native GL guards')
        def __del__(self):
            retired.append('retired')
    references = []
    for field in fields:
        guard = Guard()
        references.append(weakref.ref(guard))
        setattr(renderer, field, guard)
    del guard
    victim = context if deleted == 'context' else surface
    assert isValid(victim)
    delete(victim)
    assert not isValid(victim)
    try:
        renderer.close()
        assert renderer.context is None and renderer.surface is None
        assert not renderer.available
        assert all(getattr(renderer, field) is None for field in fields)
        assert all(reference() is None for reference in references)
        assert len(retired) == len(fields)
        renderer.close()
        assert renderer.context is None and renderer.surface is None
        assert len(retired) == len(fields)
    finally:
        renderer.close()
        for obj in (context, surface):
            if isValid(obj):
                delete(obj)
