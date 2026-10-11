"""Keep a hardware workaround isolated from explicit choices and other builds."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from comic_editor.ui import canvas as canvas_module
from comic_editor.ui import canvas_presentation_policy as policy


IDENTITY = policy.PresentationIdentity('NVIDIA Corporation',
    'NVIDIA GeForce RTX 5060/PCIe/SSE2', '3.3.0 NVIDIA 591.44', 'Kamvas Pro 16', '6.11.1')
ROUTE = ('Kamvas Pro 16', r'\\?\PCI#VEN_1002&DEV_164E&SUBSYS_D0001458&REV_C3#example')


@pytest.fixture
def affected_build(monkeypatch):
    monkeypatch.setattr(policy, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(policy, '_active_display_routes', lambda: (ROUTE,))


def test_measured_build_uses_existing_raster_presenter(affected_build):
    assert policy.prefer_raster_presentation(IDENTITY, 'Kamvas Pro 16')


@pytest.mark.parametrize('change', [
    {'vendor': 'ATI Technologies Inc.'}, {'vendor': 'Intel'},
    {'renderer': 'NVIDIA GeForce RTX 5060 Ti/PCIe/SSE2'},
    {'renderer': 'NVIDIA GeForce RTX 5070/PCIe/SSE2'},
    {'version': '3.3.0 NVIDIA 592.00'}, {'version': ''},
    {'qt_version': '6.10.1'}, {'screen_name': 'Other display'},
])
def test_other_builds_keep_gpu(affected_build, change):
    assert not policy.prefer_raster_presentation(replace(IDENTITY, **change), 'Kamvas Pro 16')


@pytest.mark.parametrize('routes', [(), (ROUTE, ROUTE),
    (('Other display', ROUTE[1]),),
    ((ROUTE[0], r'\\?\PCI#VEN_10DE&DEV_2D05&SUBSYS_TEST'),),
    ((ROUTE[0], r'\\?\PCI#VEN_1002&DEV_1681&SUBSYS_TEST'),),
])
def test_unknown_or_different_display_route_keeps_gpu(affected_build, monkeypatch, routes):
    monkeypatch.setattr(policy, '_active_display_routes', lambda: routes)
    assert not policy.prefer_raster_presentation(IDENTITY, 'Kamvas Pro 16')


@pytest.mark.parametrize('screen', ['', 'Other display'])
def test_probe_and_widget_must_share_identified_screen(affected_build, screen):
    assert not policy.prefer_raster_presentation(IDENTITY, screen)


def test_unknown_identity_or_platform_does_not_query_routes(monkeypatch):
    def forbidden():
        raise AssertionError('Do not query routes for an unrelated presentation build')
    monkeypatch.setattr(policy, '_active_display_routes', forbidden)
    assert not policy.prefer_raster_presentation(None, 'Kamvas Pro 16')
    monkeypatch.setattr(policy, 'sys', SimpleNamespace(platform='linux'))
    assert not policy.prefer_raster_presentation(IDENTITY, 'Kamvas Pro 16')
    assert policy._active_display_routes is forbidden


@pytest.mark.parametrize('requested,available,expected', [
    ('auto', True, 'raster'), ('gpu', True, 'gpu'), ('raster', True, 'raster'),
    ('auto', False, 'raster'), ('gpu', False, 'raster'),
])
def test_factory_preserves_explicit_renderer_and_unavailable_fallback(
        qapp, affected_build, monkeypatch, requested, available, expected):
    monkeypatch.setattr(canvas_module, '_GPU_PRESENTATION_IDENTITY', IDENTITY)
    monkeypatch.setattr(canvas_module, 'gpu_available', lambda: available)
    monkeypatch.setattr(canvas_module, 'GpuCanvasWidget', lambda *_: 'gpu')
    monkeypatch.setattr(canvas_module, 'RasterCanvasWidget', lambda *_: 'raster')
    if requested != 'auto' or not available:
        monkeypatch.setattr(policy, 'prefer_raster_presentation',
            lambda *_: pytest.fail('Only available auto presentation needs the policy'))
    parent = SimpleNamespace(screen=lambda: SimpleNamespace(name=lambda: 'Kamvas Pro 16'))
    assert canvas_module.create_canvas(SimpleNamespace(canvas_renderer=requested), parent) == expected


def test_secondary_parent_does_not_inherit_primary_workaround(qapp, affected_build, monkeypatch):
    monkeypatch.setattr(canvas_module, '_GPU_PRESENTATION_IDENTITY', IDENTITY)
    monkeypatch.setattr(canvas_module, 'gpu_available', lambda: True)
    monkeypatch.setattr(canvas_module, 'GpuCanvasWidget', lambda *_: 'gpu')
    monkeypatch.setattr(canvas_module, 'RasterCanvasWidget', lambda *_: 'raster')
    parent = SimpleNamespace(screen=lambda: SimpleNamespace(name=lambda: 'Other display'))
    assert canvas_module.create_canvas(SimpleNamespace(canvas_renderer='auto'), parent) == 'gpu'
