"""Posterize UI waits for ordinary exact native effects without freezing a gesture."""
from threading import Event, get_ident
import time

import numpy as np
import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog

from comic_editor.core.models import (BoundGeometry, ChapterDocument, DistortModifier,
    HueSaturationLightnessModifier, ParameterMaskBinding, PosterizeModifier, ShapeStyle, ToneMask)
from comic_editor.core.settings import EditorSettings
from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed, projection_deferred
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.posterize_controls import PosterizeControls, PosterizeSampler, PosterizeSampleRequest
from test_posterize import editor
from test_posterize_gradient_sampling import gradient_scene, gradient, ramp


def spin(qapp, predicate, seconds=5.):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.005)
    assert predicate(), 'Exact statistics did not settle'


def heavy_prefix(canvas, targets):
    twist = DistortModifier(parameters={'angle': 65}, frame=(0, 0, 256, 256),
                            center=(128, 128), radius=120)
    canvas.chapter.add_modifier(twist, targets)
    canvas.chapter.add_modifier(HueSaturationLightnessModifier(hue=35), targets)
    return twist


def gated_jobs(canvas, monkeypatch):
    entered, release = Event(), Event()
    original = canvas._effect_jobs.request
    gui, calls = get_ident(), []
    def request(scope, key, compute, size, **kwargs):
        assert kwargs.get('require_exact'), 'Statistics must never accept a draft'
        assert scope[2] == 'posterize-statistics'
        calls.append((scope, key, size))
        def guarded(cancelled):
            assert get_ident() != gui, 'Native effect executed in the GUI thread'
            entered.set()
            assert release.wait(5.)
            return compute(cancelled)
        return original(scope, key, guarded, size, **kwargs)
    monkeypatch.setattr(canvas._effect_jobs, 'request', request)
    return entered, release, calls


def oracle(canvas, targets, before_id=None, value_mode=False):
    copy = CanvasWidget(EditorSettings(canvas_renderer='raster', snap_to_grid=False))
    copy.set_document(ChapterDocument.from_dict(canvas.chapter.to_dict()), canvas.tiles, canvas.images)
    sampler = PosterizeSampler(value_mode)
    statistics = sampler.sample(copy, targets, before_id)
    pixels = [item[0].copy() for item in sampler._samples]
    copy._effect_jobs.cancel()
    copy.deleteLater()
    return statistics, pixels


def assert_same(first, second):
    np.testing.assert_array_equal(first.counts, second.counts)
    if hasattr(first, 'rgb_sums'):
        np.testing.assert_array_equal(first.rgb_sums, second.rgb_sums)


@pytest.mark.parametrize('layer_target', [False, True])
def test_heavy_constructor_yields_native_effects_and_matches_full_oracle(editor, qapp, monkeypatch, layer_target):
    canvas, controls, raster = editor
    targets = controls.targets()
    heavy_prefix(canvas, targets)
    if layer_target:
        targets = [('layer', raster.parent_layer_id)]
        canvas.chapter.add_modifier(HueSaturationLightnessModifier(hue=-20), targets)
        canvas.set_selection(*targets[0])
    mask_owner = canvas.chapter.add_layer(canvas.chapter.layers[raster.parent_layer_id].parent_id,
        'Mask', BoundGeometry.rectangle(0, 0, 128, 256),
        style=ShapeStyle(primary_color='#FFFFFFFF', outline_thickness=0))
    mask = ToneMask(contributors=[('layer', mask_owner.layer_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    modifier = PosterizeModifier()
    canvas.chapter.add_modifier(modifier, targets)
    canvas.chapter.modifiers[raster.modifier_ids[0]].parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 15, 85)
    original = canvas.chapter.to_dict()
    entered, release, calls = gated_jobs(canvas, monkeypatch)
    try:
        panel = PosterizeControls(modifier, controls)
        assert entered.wait(1.) and calls
        assert not panel._statistics_ready
        assert not panel.add_button.isEnabled()
        assert 'Preparing colors' in panel.range_label.text()
        assert panel.color_button.isEnabled()
        assert canvas.chapter.to_dict() == original
        assert not canvas._interactive_render and not canvas._projection_exact
        assert canvas._effect_preview_channel == 'canvas'
        assert not getattr(canvas, '_posterize_statistics_capture', False)
        assert not canvas._render_modifier_sources
        release.set()
        spin(qapp, lambda: panel._statistics_ready)
        expected, pixels = oracle(canvas, targets, modifier.modifier_id)
        assert_same(panel.wheel.statistics, expected)
        for actual, reference in zip(panel.sampler._samples, pixels):
            np.testing.assert_array_equal(actual[0], reference)
        assert canvas.chapter.to_dict() == original
        panel.deleteLater()
    finally:
        release.set()


@pytest.mark.parametrize('failure', [ProjectionPending('test', 'key'), ProjectionFailed('test', 'key', 'failed')])
def test_pending_and_failure_restore_every_capture_flag_and_no_partial_statistics(editor, monkeypatch, failure):
    canvas, controls, raster = editor
    marker = object()
    canvas._effect_viewport_world = marker
    canvas._effect_region_requests = True
    canvas._bounded_effect_preview = True
    original = canvas.chapter.to_dict()
    sampler = PosterizeSampler()
    previous = sampler.statistics
    def fail(*args):
        assert projection_deferred(canvas)
        assert canvas._effect_viewport_world is None
        assert not canvas._effect_region_requests
        assert not canvas._bounded_effect_preview
        raise failure
    monkeypatch.setattr(canvas, '_render_object', fail)
    with pytest.raises(type(failure)):
        sampler.sample(canvas, controls.targets(), defer_effects=True)
    assert sampler.statistics is previous and not sampler._samples
    assert canvas.chapter.to_dict() == original
    assert canvas._effect_viewport_world is marker
    assert canvas._effect_region_requests and canvas._bounded_effect_preview
    assert not canvas._interactive_render and not canvas._projection_exact
    assert not getattr(canvas, '_posterize_statistics_capture', False)


@pytest.mark.parametrize('change', ['source', 'history', 'selection', 'chapter'])
def test_pending_statistics_reject_mutation_undo_selection_and_document_change(editor, qapp, monkeypatch, change):
    canvas, controls, raster = editor
    twist = heavy_prefix(canvas, controls.targets())
    entered, release, calls = gated_jobs(canvas, monkeypatch)
    request = PosterizeSampleRequest(canvas, controls.targets(), parent=controls)
    finished, cancelled = [], []
    request.finished.connect(finished.append)
    request.cancelled.connect(lambda: cancelled.append(True))
    try:
        request.start()
        assert entered.wait(1.)
        if change == 'source':
            twist.parameters['angle'] = -40  # Unsignaled mutable record change.
        elif change == 'history':
            canvas._history_generation = getattr(canvas, '_history_generation', 0) + 1
        elif change == 'selection':
            canvas.set_selection('layer', raster.parent_layer_id)
        else:
            canvas.set_document(ChapterDocument.from_dict(canvas.chapter.to_dict()), canvas.tiles, canvas.images)
        release.set()
        spin(qapp, lambda: not request.active)
        assert cancelled and not finished
        assert not request.sampler._samples and not request.sampler.statistics.counts.sum()
    finally:
        release.set()


def test_live_parameter_drag_defers_statistics_until_release(editor, qapp, monkeypatch):
    canvas, controls, raster = editor
    upstream = HueSaturationLightnessModifier(hue=0)
    modifier = PosterizeModifier()
    canvas.chapter.add_modifier(upstream, controls.targets())
    canvas.chapter.add_modifier(modifier, controls.targets())
    panel = PosterizeControls(modifier, controls)
    spin(qapp, lambda: panel._statistics_ready)
    calls = []
    original = panel.sampler.sample
    def sampled(*args, **kwargs):
        calls.append(True)
        assert not canvas._projection_has_live_preview()
        return original(*args, **kwargs)
    monkeypatch.setattr(panel.sampler, 'sample', sampled)
    controls.begin_parameter_drag(upstream.modifier_id)
    controls.set_parameter(upstream.modifier_id, 'hue', 80, False)
    panel.refresh_statistics()
    qapp.processEvents()
    assert not panel._statistics_ready and not calls
    controls.finish_parameter_drag()
    spin(qapp, lambda: panel._statistics_ready)
    assert calls and np.argmax(panel.wheel.statistics.counts) == 80
    panel.deleteLater()


def test_actual_add_dialog_pending_palette_keeps_editing_and_commits_one_undo(editor, qapp, monkeypatch):
    canvas, controls, raster = editor
    heavy_prefix(canvas, controls.targets())
    before = canvas.chapter.to_dict()
    expected, _ = oracle(canvas, controls.targets())
    entered, release, calls = gated_jobs(canvas, monkeypatch)
    def accept_count():
        dialog = qapp.activeModalWidget()
        assert isinstance(dialog, QInputDialog)
        dialog.setIntValue(3)
        dialog.accept()
    QTimer.singleShot(0, accept_count)
    try:
        controls.add_modifier('posterize')
        assert entered.wait(1.) and calls
        assert not any(isinstance(item, PosterizeModifier) for item in canvas.chapter.modifiers.values())
        assert 'keep editing' in controls._posterize_add_status.text()
        assert controls.add_button.isEnabled()
        release.set()
        spin(qapp, lambda: any(isinstance(item, PosterizeModifier) for item in canvas.chapter.modifiers.values()))
        modifier = next(item for item in canvas.chapter.modifiers.values() if isinstance(item, PosterizeModifier))
        palette = expected.initialize(3)
        assert [(item.start, item.color) for item in modifier.ranges] == [(item.start, item.color) for item in palette]
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before
    finally:
        release.set()


def test_pending_add_is_cancelled_on_intervening_edit_and_does_not_install(editor, qapp, monkeypatch):
    canvas, controls, raster = editor
    twist = heavy_prefix(canvas, controls.targets())
    entered, release, _ = gated_jobs(canvas, monkeypatch)
    monkeypatch.setattr(QInputDialog, 'getInt', lambda *_: (3, True))
    try:
        controls.add_modifier('posterize')
        assert entered.wait(1.)
        controls.set_parameter(twist.modifier_id, 'intensity', 50, True)
        release.set()
        spin(qapp, lambda: not controls._posterize_add_request.active)
        assert 'cancelled' in controls._posterize_add_status.text()
        assert not any(isinstance(item, PosterizeModifier) for item in canvas.chapter.modifiers.values())
        assert twist.intensity == 50
    finally:
        release.set()


def test_actual_modifier_undo_cancels_pending_statistics(editor, qapp, monkeypatch):
    canvas, controls, _ = editor
    twist = heavy_prefix(canvas, controls.targets())
    controls.set_parameter(twist.modifier_id, 'intensity', 70, True)
    entered, release, _ = gated_jobs(canvas, monkeypatch)
    request = PosterizeSampleRequest(canvas, controls.targets(), parent=controls)
    finished = []
    request.finished.connect(finished.append)
    try:
        request.start()
        assert entered.wait(1.)
        canvas.command_stack.undo()
        release.set()
        spin(qapp, lambda: not request.active)
        assert not finished and not request.sampler._samples
        assert canvas.chapter.modifiers[twist.modifier_id].intensity == 100
    finally:
        release.set()


def test_failed_native_worker_reports_pending_histogram_without_gui_fallback(editor, qapp, monkeypatch):
    canvas, controls, _ = editor
    heavy_prefix(canvas, controls.targets())
    modifier = PosterizeModifier()
    canvas.chapter.add_modifier(modifier, controls.targets())
    gui, calls = get_ident(), []
    from comic_editor.ui import distort_rendering
    def fail(*args, **kwargs):
        calls.append(get_ident())
        assert get_ident() != gui
        raise ValueError('Unavailable filter test')
    monkeypatch.setattr(distort_rendering, 'render_distort', fail)
    panel = PosterizeControls(modifier, controls)
    spin(qapp, lambda: 'Unavailable filter test' in panel.range_label.text())
    assert calls and all(thread != gui for thread in calls)
    assert not panel._statistics_ready and not panel.add_button.isEnabled()
    assert panel.color_button.isEnabled()
    assert not panel.sampler._samples
    assert not canvas._interactive_render and not canvas._projection_exact
    panel.deleteLater()


def test_prefix_context_does_not_consume_ordinary_or_other_target_signature_memo(editor):
    canvas, controls, raster = editor
    upstream = HueSaturationLightnessModifier(hue=30)
    own = PosterizeModifier()
    targets = [('layer', raster.parent_layer_id), ('object', raster.object_id)]
    for modifier in (upstream, own):
        canvas.chapter.add_modifier(modifier, targets)
    sampler = PosterizeSampler()
    expected = sampler.context_key(canvas, targets, own.modifier_id)
    original = canvas.chapter.to_dict()
    from comic_editor.ui.render_signatures import signature_scope
    with signature_scope(canvas):
        full_object = canvas._modifier_object_signature(raster)
        full_layer = canvas._modifier_layer_signature(raster.parent_layer_id)
        key = sampler.context_key(canvas, targets, own.modifier_id)
        assert key == expected
        assert key[0][-1][1][2] != full_object
        assert key[0][-1][0][2] != full_layer
        upstream.hue = 75
        assert sampler.context_key(canvas, targets, own.modifier_id) != key
        upstream.hue = 30
        assert canvas._modifier_object_signature(raster) == full_object
    assert canvas.chapter.to_dict() == original


def test_saved_exact_source_pending_retries_without_statistics_or_palette_fallback(editor, qapp, monkeypatch):
    canvas, controls, _ = editor
    from comic_editor.render.service import RenderPending
    original = canvas._render_object
    calls = []
    def pending_once(*args):
        calls.append(True)
        if len(calls) == 1:
            raise RenderPending('Saved artwork is loading')
        return original(*args)
    monkeypatch.setattr(canvas, '_render_object', pending_once)
    request = PosterizeSampleRequest(canvas, controls.targets(), parent=controls)
    finished = []
    request.finished.connect(finished.append)
    request.start()
    assert request.active and not finished and not request.sampler._samples
    assert not canvas._interactive_render and not canvas._projection_exact
    spin(qapp, lambda: not request.active)
    assert finished and len(calls) == 2
    assert finished[0].counts.sum() == 256 * 256


def test_blocked_job_retry_does_not_rebuild_prefix_until_capture_can_advance(editor, qapp, monkeypatch):
    canvas, controls, _ = editor
    twist = heavy_prefix(canvas, controls.targets())
    entered, release, _ = gated_jobs(canvas, monkeypatch)
    sampler = PosterizeSampler()
    request = PosterizeSampleRequest(canvas, controls.targets(), sampler=sampler, parent=controls)
    finished = []
    request.finished.connect(finished.append)
    try:
        request.start()
        assert entered.wait(1.)
        original = sampler.context_key
        checks = []
        def checked(*args, **kwargs):
            checks.append(True)
            return original(*args, **kwargs)
        monkeypatch.setattr(sampler, 'context_key', checked)
        for _ in range(8):
            request._retry()
        assert not checks
        twist.parameters['angle'] = -15
        release.set()
        spin(qapp, lambda: not request.active)
        assert checks and not finished and not sampler._samples
    finally:
        release.set()


@pytest.mark.parametrize('reverse', [False, True])
def test_deferred_gradient_isolation_has_same_pixels_and_histogram(gradient_scene, reverse):
    canvas, chapter, parent = gradient_scene
    obj = chapter.add_object(parent.layer_id, gradient('parent_shape', reverse, ramp('#FFFF0000', '#FF00FF00')))
    targets = [('object', obj.object_id)]
    first, second = PosterizeSampler(), PosterizeSampler()
    expected = first.sample(canvas, targets)
    actual = second.sample(canvas, targets, defer_effects=True)
    assert_same(actual, expected)
    np.testing.assert_array_equal(first._samples[0][0], second._samples[0][0])
