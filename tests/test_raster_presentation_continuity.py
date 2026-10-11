"""A newly displayed native contact must not fall back to older artwork."""
from dataclasses import replace

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry
from comic_editor.core.tools import ToolKind
from test_raster_contact_feedback import scene, ready_paint
from test_raster_contact_feedback_boundaries import block_existing_scene_demand
from test_raster_feedback_bootstrap import drain_native


@pytest.mark.parametrize('transition', ['selection', 'next-chunk', 'partial-coverage', 'old-preview'])
@pytest.mark.parametrize('predictive', [False, True])
def test_latest_contact_survives_pending_exact_selection_and_next_chunk(
        scene, wait_scene, monkeypatch, qapp, transition, predictive):
    canvas, obj, front = scene
    canvas.chapter.width = 768
    canvas.setFixedSize(768, 128)
    canvas.center_x, canvas.center_y = 384., 64.
    page = canvas.chapter.layers[obj.parent_layer_id]
    page.bound = BoundGeometry.rectangle(8, 8, 752, 112)
    page.children = [ref for ref in page.children if ref.entity_id != front.object_id]
    del canvas.chapter.objects[front.object_id]
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    canvas.settings.predictive_ink = predictive
    ordinary_request = canvas._scene_controller.request
    release = block_existing_scene_demand(canvas, monkeypatch)
    try:
        canvas._begin_stroke(QPointF(32, 64), 1.)
        canvas._continue_stroke(QPointF(100, 64), 1.)
        drain_native(canvas, qapp)
        assert ready_paint(canvas).pixelColor(80, 64) == QColor('red')
        canvas._end_stroke()
        drain_native(canvas, qapp)
        if not predictive:
            assert ready_paint(canvas).pixelColor(80, 64) == QColor('red')
        if transition == 'selection':
            canvas.set_selection('layer', page.layer_id, activate_default_tool=False)
        elif transition == 'old-preview':
            canvas.set_selection('layer', page.layer_id, activate_default_tool=False)
            controller = canvas._scene_controller
            exact = canvas._projection_completed_view
            # A completed viewport preview from before this stroke is still
            # same-configuration work. It cannot replace newer displayed ink.
            from comic_editor.ui.document_presentation import PresentedTile
            from PySide6.QtCore import QRectF
            older = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
            older.fill(QColor('blue'))
            controller.preview = replace(canvas._render_document_state(), revision=exact[2]), PresentedTile(
                ('preview', -1), older, QRectF(0., 0., 768., 128.))
            monkeypatch.setattr(controller, 'request', lambda *_args: setattr(controller, 'preview_mode', True))
        else:
            # Use a different source cell before the previous exact work has
            # completed. Selection transitions happen implicitly in the UI.
            canvas.set_selection('layer', page.layer_id, activate_default_tool=False)
            canvas.set_selection('object', obj.object_id, activate_default_tool=False)
            canvas.set_tool(ToolKind.RASTER_PENCIL)
            canvas._begin_stroke(QPointF(550, 64), 1.)
            canvas._continue_stroke(QPointF(610, 64), 1.)
            drain_native(canvas, qapp)
            if transition == 'partial-coverage':
                # A bounded/progressively prepared owner may replace the
                # earlier owner's coverage with patches at the new contact.
                # Its journal still contains both changed source addresses.
                controller = canvas._scene_controller
                assert controller.feedback is not None
                prepared = controller.feedback
                controller.feedback = replace(prepared, tiles=tuple(tile for tile in prepared.tiles if tile.key == (2, 0)))
                monkeypatch.setattr(controller, '_ensure_bootstrap', lambda *_args: None)
        assert ready_paint(canvas).pixelColor(80, 64) == QColor('red')
        assert canvas.tiles.object_tiles(obj.object_id)[(0, 0)].pixelColor(80, 64) == QColor('red')
    finally:
        if canvas._drawing:
            canvas._end_stroke()
        release.set()
        if transition == 'old-preview':
            monkeypatch.setattr(canvas._scene_controller, 'request', ordinary_request)
    wait_scene(canvas)
    assert ready_paint(canvas).pixelColor(80, 64) == QColor('red')


@pytest.mark.parametrize('prune', [False, True])
def test_erased_native_pixels_survive_selection_without_exact_repopulation(
        scene, wait_scene, monkeypatch, qapp, prune):
    canvas, obj, front = scene
    if prune:
        canvas.chapter.height = 1024
    page = canvas.chapter.layers[obj.parent_layer_id]
    page.children = [ref for ref in page.children if ref.entity_id != front.object_id]
    del canvas.chapter.objects[front.object_id]
    source = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor('red'))
    canvas.tiles.set_tile(obj.object_id, (0, 0), source)
    canvas.settings.eraser_size_px = dict.fromkeys(('small', 'medium', 'large'), 800 if prune else 20)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    release = block_existing_scene_demand(canvas, monkeypatch)
    try:
        canvas._begin_stroke(QPointF(64, 64), 1.)
        drain_native(canvas, qapp)
        assert ready_paint(canvas).pixelColor(64, 64) == QColor('blue')
        canvas._end_stroke()
        drain_native(canvas, qapp)
        canvas.set_selection('layer', page.layer_id, activate_default_tool=False)
        assert ready_paint(canvas).pixelColor(64, 64) == QColor('blue')
        if prune:
            assert not canvas.tiles.object_tiles(obj.object_id)
        canvas.command_stack.undo()
        assert canvas._scene_controller.raster_continuity.document is None
    finally:
        release.set()
    wait_scene(canvas)
    assert ready_paint(canvas).pixelColor(64, 64) == QColor('red')


def test_old_contact_preview_cannot_reappear_after_undo_then_selection(
        scene, wait_scene, monkeypatch, qapp):
    from threading import Event

    canvas, obj, _front = scene
    controller = canvas._scene_controller
    wait_scene(canvas)
    controller.retire_feedback()
    monkeypatch.setattr(controller, '_ensure_bootstrap', lambda *_args: None)
    canvas._begin_stroke(QPointF(32, 64), 1.)
    drain_native(canvas, qapp)
    wait_scene(canvas)
    assert ready_paint(canvas).pixelColor(32, 64) == QColor('red')
    old_preview = controller.preview
    assert old_preview[0].contact_only
    release = Event()
    evaluate = controller.scheduler._evaluate_admitted
    def blocked(demand, token):
        assert release.wait(20.)
        return evaluate(demand, token)
    monkeypatch.setattr(controller.scheduler, '_evaluate_admitted', blocked)
    request = controller.request
    try:
        canvas._end_stroke()
        drain_native(canvas, qapp)
        canvas.command_stack.undo()
        assert controller.raster_continuity.document is None
        canvas.set_selection('layer', obj.parent_layer_id, activate_default_tool=False)
        controller.preview = old_preview
        monkeypatch.setattr(controller, 'request', lambda *_args: setattr(controller, 'preview_mode', True))
        assert ready_paint(canvas).pixelColor(32, 64) == QColor('blue')
        assert controller.raster_continuity.document is None
        # A second selection must not extend that canceled source history.
        canvas.set_selection('object', obj.object_id, activate_default_tool=False)
        assert ready_paint(canvas).pixelColor(32, 64) == QColor('blue')
    finally:
        release.set()
        monkeypatch.setattr(controller, 'request', request)
    wait_scene(canvas)
    assert ready_paint(canvas).pixelColor(32, 64) == QColor('blue')


def test_rotated_translucent_contact_selection_preserves_presented_alpha(
        scene, wait_scene, monkeypatch, qapp):
    import numpy as np
    from test_raster_contact_feedback import pixels

    canvas, obj, _front = scene
    canvas.scale, canvas.rotation = 1.37, 17.
    canvas.primary_color = '#80ff0000'
    monkeypatch.setattr('comic_editor.ui.raster_feedback.present_pending_raster_gesture', lambda *_args: False)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    release = block_existing_scene_demand(canvas, monkeypatch)
    try:
        canvas._begin_stroke(QPointF(32, 64), 1.)
        canvas._continue_stroke(QPointF(42, 64), 1.)
        drain_native(canvas, qapp)
        held = ready_paint(canvas)
        canvas._end_stroke()
        drain_native(canvas, qapp)
        canvas.set_selection('layer', obj.parent_layer_id, activate_default_tool=False)
        np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(held))
    finally:
        release.set()
    wait_scene(canvas)


def test_continuity_bounds_storage_and_rejects_scene_or_view_changes(scene):
    from PySide6.QtCore import QRectF
    from comic_editor.core.changes import ChangeSet, EntityChange
    from comic_editor.ui.document_presentation import PresentedTile
    from comic_editor.ui.raster_presentation_continuity import RasterPresentationContinuity

    canvas, obj, _front = scene
    document = canvas._render_document_state()
    image = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    tile = PresentedTile(('native', 0), image, QRectF(0., 0., 32., 32.))
    alias = replace(tile, key=('native', 1), world_rect=QRectF(32., 0., 32., 32.))
    owner = RasterPresentationContinuity(budget=image.sizeInBytes())
    view = canvas._scene_controller.presentation_view()
    assert owner.remember(document, ((None, (tile, alias)),), (tile,), view=view)
    assert owner.byte_count == image.sizeInBytes()
    additional = replace(alias, image=image.copy())
    assert not owner.remember(document, ((None, (tile, additional)),), view=view)
    assert not owner.remember(document, ((None, (replace(tile, image=QImage()),)),), view=view)
    assert owner.byte_count == image.sizeInBytes()
    assert owner.fallback(document, revision=document.revision, view=view) is not None
    assert owner.fallback(document, revision=document.revision, view=('changed-camera',)) is None
    assert owner.remember(document, ((None, (tile,)),), view=view)
    changed = ChangeSet(entities=(EntityChange(('object', obj.object_id), frozenset({'opacity'})),))
    owner.changed(changed, 'push', document)
    assert not owner.remember(document, ((None, (tile,)),), view=view)
    newer = replace(document, revision=document.revision+1)
    assert owner.remember(newer, ((None, (tile,)),), view=view)
    assert owner.fallback(replace(newer, configuration=(*document.configuration, 'new-profile')),
        revision=document.revision, view=view) is None
    assert owner.byte_count == 0


def test_forward_contacts_on_two_rasters_keep_both_chunks_before_exact_completion(
        scene, wait_scene, monkeypatch, qapp):
    import time
    from threading import Event
    from comic_editor.core.models import RasterObject
    from test_raster_contact_feedback import _queue_blocked_native_scene

    canvas, first_obj, front = scene
    canvas.chapter.width = 768
    canvas.setFixedSize(768, 128)
    canvas.center_x, canvas.center_y = 384., 64.
    page = canvas.chapter.layers[first_obj.parent_layer_id]
    page.bound = BoundGeometry.rectangle(8, 8, 752, 112)
    page.children = [ref for ref in page.children if ref.entity_id != front.object_id]
    del canvas.chapter.objects[front.object_id]
    second_obj = canvas.chapter.add_object(page.layer_id, RasterObject())
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    # Delay the exact scene before global worker admission, so a newly chosen
    # source can still obtain its legitimate independent native preparation.
    release, entered = Event(), Event()
    scheduler = canvas._scene_controller.scheduler
    evaluate = scheduler._evaluate
    def blocked(demand, token):
        entered.set()
        assert release.wait(20.)
        return evaluate(demand, token)
    monkeypatch.setattr(scheduler, '_evaluate', blocked)
    _queue_blocked_native_scene(canvas)
    assert entered.wait(3.)
    try:
        canvas._begin_stroke(QPointF(64, 64), 1.)
        drain_native(canvas, qapp)
        assert ready_paint(canvas).pixelColor(64, 64) == QColor('red')
        canvas._end_stroke()
        drain_native(canvas, qapp)
        first_source = bytes(canvas.tiles.object_tiles(first_obj.object_id)[(0, 0)].constBits())
        canvas.set_selection('object', second_obj.object_id, activate_default_tool=False)
        canvas.set_tool(ToolKind.RASTER_PENCIL)
        canvas._begin_stroke(QPointF(550, 64), 1.)
        drain_native(canvas, qapp)
        deadline = time.monotonic()+5.
        while time.monotonic() < deadline:
            held = ready_paint(canvas)
            assert held.pixelColor(64, 64) == QColor('red')
            if held.pixelColor(550, 64) == QColor('red'):
                break
            qapp.processEvents()
            time.sleep(.002)
        assert held.pixelColor(550, 64) == QColor('red')
        canvas._end_stroke()
        drain_native(canvas, qapp)
        assert bytes(canvas.tiles.object_tiles(first_obj.object_id)[(0, 0)].constBits()) == first_source
        canvas.set_selection('layer', page.layer_id, activate_default_tool=False)
        selected = ready_paint(canvas)
        assert selected.pixelColor(64, 64) == QColor('red')
        assert selected.pixelColor(550, 64) == QColor('red')
    finally:
        if canvas._drawing:
            canvas._end_stroke()
        release.set()
    wait_scene(canvas)
    assert ready_paint(canvas).pixelColor(64, 64) == QColor('red')
    assert ready_paint(canvas).pixelColor(550, 64) == QColor('red')


@pytest.mark.parametrize('case', ['plain', 'ordinary'])
def test_saved_pocket_boyfriend_native_chunks_and_selection(qapp, monkeypatch, wait_scene, tmp_path, case):
    """Opt-in complete copied-project proof; no user project/preferences writes."""
    import hashlib
    import json
    import os
    from pathlib import Path

    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.core.settings import EditorSettings
    from comic_editor.ui.canvas import CanvasWidget

    project = os.environ.get('POCKET_PROJECT_COPY')
    if not project:
        pytest.skip('Set POCKET_PROJECT_COPY to the private complete Pocket Boyfriend copy')
    root = Path(project).resolve()
    assert '.artifacts' in root.parts and (root / 'series.json').is_file()
    if case == 'plain':
        chapter_id, identifier = 'f596d3fbbde94000a6a6a066100e83e3', 'f0b6d6fa026c43b69d098f02fa91cc4f'
        first_local, second_local = QPointF(100., 200.), QPointF(500., 200.)
    else:
        chapter_id, identifier = '0a72f08009294aa0a3d14e6a38e22bbb', '06be89900eb540ee83a26514822d32ec'
        first_local, second_local = QPointF(1020., 1000.), QPointF(1400., 1000.)
    directory = root / 'chapters' / chapter_id
    paths = [root / 'series.json', directory / 'chapter.json', *sorted((directory / 'raster' / identifier).glob('*.png'))]
    original_files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    chapter, tiles, images = SeriesRepository(root).load_chapter(chapter_id, include_images=True)
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', predictive_ink=True,
        grid_overlay_visible=False, snap_to_grid=False))
    canvas.setFixedSize(768, 256)
    canvas.set_document(chapter, tiles, images)
    obj = chapter.objects[identifier]
    canvas.set_selection('object', identifier)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.settings.pencil_size_px = dict.fromkeys(('small', 'medium', 'large'), 18)
    canvas.primary_color = '#FFFF00FF'
    transform = canvas._drawing_local_to_world_transform(obj)
    first, second = transform.map(first_local), transform.map(second_local)
    first_key = int(first_local.x() // 256), int(first_local.y() // 256)
    canvas.center_x, canvas.center_y = (first.x() + second.x()) / 2, (first.y() + second.y()) / 2
    canvas.scale = 1.
    def saved_paint():
        # Match the real paint trigger, including this chapter's show-on-top
        # phase split only while an active drawing contact is held.
        from PySide6.QtGui import QPainter
        image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('#242428'))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        try:
            canvas._paint_ready_document_projection(painter, live_ink=canvas._drawing)
        finally:
            painter.end()
        return image
    source_state = lambda: {key: (image.format().value, bytes(image.constBits()))
        for key, image in tiles.object_tiles(identifier).items()}
    original = source_state()
    model = chapter.to_dict()
    report = {'case': case, 'chapter': chapter.name, 'graph': [len(chapter.layers), len(chapter.objects), len(chapter.modifiers), len(chapter.masks)],
        'raster': identifier, 'original_tile_count': len(original)}
    release = None
    try:
        wait_scene(canvas, timeout=120)
        if case == 'plain':
            assert canvas._scene_controller.feedback is not None
            release = block_existing_scene_demand(canvas, monkeypatch)
        else:
            # Exercise the production conservative contact-preview fallback
            # with the unchanged complete saved graph and native source data.
            canvas._scene_controller.retire_feedback()
            monkeypatch.setattr(canvas._scene_controller, '_ensure_bootstrap', lambda *_args: None)
        canvas._begin_stroke(first, 1.)
        drain_native(canvas, qapp)
        if case == 'ordinary':
            wait_scene(canvas, timeout=120)
        shown = saved_paint()
        point = canvas.camera_transform().map(first).toPoint()
        assert shown.pixelColor(point) == QColor('magenta')
        if case == 'ordinary':
            assert canvas._scene_controller.raster_continuity.document is not None, (
                canvas._render_document_state(), canvas._scene_controller.preview,
                canvas._projection_provisional_visible, canvas._scene_controller._contact_preview_owner)
            from threading import Event
            release = Event()
            scheduler = canvas._scene_controller.scheduler
            evaluate = scheduler._evaluate_admitted
            def blocked(demand, token):
                assert release.wait(20.), 'Copied-project release barrier was not released'
                return evaluate(demand, token)
            monkeypatch.setattr(scheduler, '_evaluate_admitted', blocked)
        canvas._end_stroke()
        drain_native(canvas, qapp)
        first_source = source_state()
        assert first_source != original
        canvas.set_selection('layer', obj.parent_layer_id, activate_default_tool=False)
        assert saved_paint().pixelColor(point) == QColor('magenta')
        canvas.set_selection('object', identifier, activate_default_tool=False)
        canvas.set_tool(ToolKind.RASTER_PENCIL)
        canvas._begin_stroke(second, 1.)
        drain_native(canvas, qapp)
        assert saved_paint().pixelColor(point) == QColor('magenta')
        canvas._end_stroke()
        drain_native(canvas, qapp)
        second_source = source_state()
        assert second_source[first_key] == first_source[first_key]
        assert second_source != first_source
        canvas.set_selection('layer', obj.parent_layer_id, activate_default_tool=False)
        assert saved_paint().pixelColor(point) == QColor('magenta')
        report['source_preserved_across_second_chunk'] = True
        report['selection_while_exact_blocked_preserves_latest_pixels'] = True
        release.set()
        wait_scene(canvas, timeout=120)
        assert saved_paint().pixelColor(point) == QColor('magenta')
        canvas.command_stack.undo()
        assert source_state() == first_source
        canvas.command_stack.undo()
        assert source_state() == original
        assert chapter.to_dict() == model
        report['exact_completion_and_two_native_undos'] = True
        assert {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths} == original_files
        report['copy_source_files_unchanged'] = True
        destination = os.environ.get('POCKET_FRESHNESS_REPORT')
        if destination:
            path = Path(destination)
            path.with_stem(path.stem + '-' + case).write_text(json.dumps(report, indent=2), encoding='utf-8')
    finally:
        if release is not None:
            release.set()
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()
