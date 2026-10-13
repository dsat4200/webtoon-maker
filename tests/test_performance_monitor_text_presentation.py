"""Sparse Text display diagnostics observe metadata, never glyph visibility."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.models import TextObject
from comic_editor.core.tools import ToolKind
from comic_editor.render.scene import SceneSnapshotCompiler
from test_performance_monitor_integration import editor, controller


class UnreadableArtwork:
    def __getattr__(self, name):
        raise AssertionError('Diagnostic read artwork/geometry: ' + name)


def group_draw(editor, monkeypatch, groups, *, backend='gpu', callback=None, calls=1,
               foreign=False):
    """Real canvas paint invokes the owned alias, without a GL/pixel renderer."""
    from PySide6.QtGui import QImage, QPainter
    from comic_editor.ui import document_projection_features as module
    from comic_editor.ui.document_presentation import PresentationStats
    canvas, text, raster = setup(editor, monkeypatch)
    box = {'groups': groups}
    def present(_painter, values, *_args, **_kwargs):
        if callback is not None:
            callback()
        return PresentationStats(backend, sum(len(group.tiles) for group in values))
    monkeypatch.setattr(module, 'draw_document_tile_groups', present)
    def draw(painter, _started):
        if foreign:
            image = QImage(2, 2, QImage.Format_ARGB32_Premultiplied)
            other = QPainter(image)
            try:
                module.draw_document_tile_groups(other, box['groups'], None, None, owner=canvas)
            finally:
                other.end()
        for _ in range(calls):
            canvas._document_presentation_stats = module.draw_document_tile_groups(
                painter, box['groups'], None, None, owner=canvas)
    monkeypatch.setattr(canvas, '_paint_canvas_frame', draw)
    return canvas, text, raster, box


def setup(editor, monkeypatch):
    window, layer, raster = editor
    canvas = window.canvas
    # The shared monitor fixture uses a reduced display width; real history
    # validation requires the chapter's canonical native document width.
    canvas.chapter.width = 1080
    text = canvas.chapter.add_object(layer.layer_id, TextObject(text='PRIVATE TEXT', font_size=42))
    # Real Qt paints exercise the installed virtual-dispatch wrapper. The
    # underlying frame is deliberately metadata-only, with no detached render
    # or presenter; this diagnostic makes no pixel correctness claim.
    def draw(painter, _started):
        painter.fillRect(canvas.rect(), QColor('white'))
        canvas._document_presentation_stats = SimpleNamespace(
            backend='cpu', tiles=1, uploads=0, texture_bytes=0)
    monkeypatch.setattr(canvas, '_paint_canvas_frame', draw)
    return canvas, text, raster


def frame(canvas, monkeypatch, *, pending=False, flags=None, configuration=None):
    document = canvas._render_document_state()
    if configuration is not None:
        document = replace(document, configuration=configuration)
    capture = SceneSnapshotCompiler().capture(canvas, document)
    while not capture.advance(.1):
        pass
    assert not capture.stale and capture.result is not None
    snapshot = capture.result
    assert type(snapshot.state) is dict
    if flags is not None:
        snapshot = replace(snapshot, state={**snapshot.state, **flags})
    monkeypatch.setattr(canvas._scene_controller, 'snapshot', snapshot)
    monkeypatch.setattr(canvas._scene_controller, 'desired', (document, (), (None,), None))
    monkeypatch.setattr(canvas, '_projection_frame_pending', pending)
    monkeypatch.setattr(canvas, '_projection_provisional_visible', False)
    monkeypatch.setattr(canvas, '_projection_presented_revision', document.revision, raising=False)
    monkeypatch.setattr(canvas, '_projection_completed_view', (document.configuration, (), document.revision))
    return snapshot


def rows(controller, suffix=None):
    events = [event for event in controller.snapshot()['critical_events']
              if event['name'].startswith('canvas.text_presentation.')]
    return events if suffix is None else [event for event in events
        if event['name'] == 'canvas.text_presentation.' + suffix]


def test_actual_selection_first_pending_paint_then_same_basis_settled_is_sparse(
        editor, controller, monkeypatch):
    canvas, text, _raster = setup(editor, monkeypatch)
    controller.start()
    try:
        canvas.set_selection(kind='object', entity_id=text.object_id)
        snapshot = frame(canvas, monkeypatch, pending=True,
            flags={'_render_exclude_text': True, '_render_excluded_object_id': text.object_id})
        assert not canvas.grab().isNull()
        assert len(rows(controller, 'action')) == len(rows(controller, 'first_paint')) == 1
        assert not rows(controller, 'first_settled')
        metadata = rows(controller, 'first_paint')[0]['details']
        assert metadata['frame_pending'] and metadata['snapshot_basis_current']
        assert metadata['live_flags']['exclude_text'] is False
        assert metadata['snapshot_flags']['exclude_text'] is True
        assert metadata['snapshot_flags']['excluded_object_id'] == text.object_id
        assert metadata['snapshot_owner'] == id(snapshot)
        assert metadata['source_owner_identity'] == [id(canvas.chapter), id(canvas.tiles), id(canvas.images)]
        assert metadata['snapshot_config_matches'] and metadata['completed_config_matches']
        canvas.grab()
        assert len(rows(controller, 'first_paint')) == 1
        frame(canvas, monkeypatch)
        canvas.grab()
        assert len(rows(controller, 'first_settled')) == 1
        assert len(rows(controller, 'first_settled.thread_cpu')) == 1
        for _ in range(10):
            canvas.grab()
        for _ in range(4000):
            controller.recorder.record_event('generic render flood', .01)
        assert len(rows(controller)) == 5
        assert len(controller.snapshot()['critical_events']) <= 512
        assert text.text == 'PRIVATE TEXT' and text.font_size == 42
    finally:
        controller.stop()


def test_floating_font_and_actual_undo_after_raster_selection_arm_changed_text(
        editor, controller, monkeypatch):
    canvas, text, raster = setup(editor, monkeypatch)
    controller.start()
    try:
        canvas.set_selection('object', text.object_id)
        frame(canvas, monkeypatch)
        canvas.grab()
        before = canvas.command_stack.revision
        canvas._text_gizmo_overlay.increase.click()
        assert text.font_size == 43 and canvas.command_stack.revision == before + 1
        assert rows(controller, 'action')[-1]['details']['text_action'] == 'floating_font'
        frame(canvas, monkeypatch)
        canvas.grab()
        canvas.set_selection('object', raster.object_id)
        assert controller._text_presentation_wait is None
        canvas.command_stack.undo()
        assert text.font_size == 42
        action = rows(controller, 'action')[-1]['details']
        assert action['text_action'] == 'undo' and action['text_target'] == text.object_id
        frame(canvas, monkeypatch)
        canvas.grab()
        final = rows(controller, 'first_settled')[-1]['details']
        assert final['text_action'] == 'undo' and final['text_target'] == text.object_id
        assert final['snapshot_selected_id'] == raster.object_id
        assert final['snapshot_flags']['text_editing'] is False
        assert not final['frame_pending'] and final['snapshot_basis_current']
        assert text.text == 'PRIVATE TEXT'
    finally:
        controller.stop()


@pytest.mark.parametrize('boundary', ['clear', 'restart', 'raster_selection', 'tool',
                                     'document_owner', 'text_owner', 'scene_owner', 'revision'])
def test_pending_text_wait_never_crosses_epoch_context_or_owner(
        editor, controller, monkeypatch, boundary):
    canvas, text, raster = setup(editor, monkeypatch)
    original_scene = canvas._scene_controller
    controller.start()
    try:
        canvas.set_selection('object', text.object_id)
        initial_count = len(rows(controller))
        if boundary == 'clear':
            controller.recorder.clear()
            initial_count = 0
        elif boundary == 'restart':
            controller.stop()
            controller.start()
            initial_count = 0
        elif boundary == 'raster_selection':
            canvas.set_selection('object', raster.object_id)
        elif boundary == 'tool':
            canvas.set_tool(ToolKind.OBJECT_SELECT)
        elif boundary == 'document_owner':
            monkeypatch.setattr(canvas, 'tiles', type(canvas.tiles)())
        elif boundary == 'text_owner':
            canvas.chapter.objects[text.object_id] = replace(text)
        elif boundary == 'scene_owner':
            monkeypatch.setattr(canvas, '_scene_controller', SimpleNamespace())
        elif boundary == 'revision':
            canvas._document_projection.invalidate()
        # Stale scope validation precedes any attempted scene/snapshot read.
        canvas.grab()
        assert len(rows(controller)) == initial_count
        assert controller._text_presentation_wait is None
    finally:
        canvas._scene_controller = original_scene
        controller.stop()


@pytest.mark.parametrize('mismatch', ['snapshot_revision', 'snapshot_identity', 'snapshot_config',
                                     'completed_config', 'gpu_pending', 'gpu_pending_flag', 'provisional'])
def test_first_settled_requires_positive_current_presentation_basis(
        editor, controller, monkeypatch, mismatch):
    canvas, text, _raster = setup(editor, monkeypatch)
    controller.start()
    try:
        canvas.set_selection('object', text.object_id)
        snapshot = frame(canvas, monkeypatch)
        scene = canvas._scene_controller
        if mismatch.startswith('snapshot_'):
            field, value = {'snapshot_revision': ('revision', snapshot.document.revision - 1),
                            'snapshot_identity': ('identity', (1, 2, 3)),
                            'snapshot_config': ('configuration', ('foreign',))}[mismatch]
            scene.snapshot = replace(snapshot, document=replace(snapshot.document, **{field: value}))
        elif mismatch == 'completed_config':
            canvas._projection_completed_view = (('foreign',), (), snapshot.document.revision)
        elif mismatch == 'provisional':
            canvas._projection_provisional_visible = True
        elif mismatch == 'gpu_pending_flag':
            monkeypatch.setattr(canvas, '_gpu_presentation_pending', True, raising=False)
        elif mismatch == 'gpu_pending':
            original = canvas._paint_canvas_frame
            def draw(painter, started):
                original(painter, started)
                canvas._document_presentation_stats.backend = 'gpu-pending'
            monkeypatch.setattr(canvas, '_paint_canvas_frame', draw)
        canvas.grab()
        assert len(rows(controller, 'first_paint')) == 1
        assert not rows(controller, 'first_settled')
    finally:
        controller.stop()


def test_foreign_history_and_noop_font_do_not_arm_and_disabled_has_no_hook(
        editor, controller, monkeypatch):
    canvas, text, raster = setup(editor, monkeypatch)
    original = canvas._change_selected_text_property
    assert not controller.enabled
    canvas.set_selection('object', text.object_id)
    assert not rows(controller) and canvas._change_selected_text_property == original
    controller.start()
    try:
        canvas._change_selected_text_property('font_size', 42, label='No change')
        assert not rows(controller)
        canvas._publish_change_set(ChangeSet((EntityChange(('object', raster.object_id),
            frozenset({'name'})),)), action='undo')
        assert not rows(controller)
        controller.recorder.stop()
        canvas.set_selection('object', text.object_id)
        assert not rows(controller)
    finally:
        controller.stop()
    assert canvas._change_selected_text_property == original


def test_configuration_change_after_first_paint_retires_frozen_wait(
        editor, controller, monkeypatch):
    canvas, text, _raster = setup(editor, monkeypatch)
    controller.start()
    try:
        canvas.set_selection('object', text.object_id)
        frame(canvas, monkeypatch, pending=True)
        canvas.grab()
        frame(canvas, monkeypatch, configuration=('different configuration',))
        canvas.grab()
        assert not rows(controller, 'first_settled')
        assert controller._text_presentation_wait is None
    finally:
        controller.stop()


def test_actual_draw_matches_older_preview_flags_instead_of_current_snapshot(
        editor, controller, monkeypatch):
    from comic_editor.ui.document_presentation import PresentedTile, PresentedTileGroup
    tile = PresentedTile(('preview', 1), UnreadableArtwork(), UnreadableArtwork())
    canvas, text, _raster, _box = group_draw(editor, monkeypatch,
        (PresentedTileGroup((tile,)),), foreign=True)
    controller.start()
    try:
        canvas.set_selection('object', text.object_id)
        snapshot = frame(canvas, monkeypatch, pending=True)
        older = replace(snapshot, document=replace(snapshot.document,
            revision=snapshot.document.revision - 1), state={**snapshot.state,
            '_render_exclude_text': True, '_render_excluded_object_id': text.object_id})
        canvas._scene_controller.preview = older.document, tile
        canvas._scene_controller._preview_snapshot = older
        canvas.grab()
        metadata = rows(controller, 'first_paint')[0]['details']
        assert metadata['snapshot_flags']['exclude_text'] is False
        draws = metadata['draw_group_provenance']
        assert draws[0]['call_count'] == 1  # The foreign QImage painter did not lend ownership.
        assert draws[1]['result_backend'] == 'gpu'
        origin = draws[1]
        assert origin['origin'] == 'preview' and origin['revision'] == older.document.revision
        assert origin['origin_flags_available'] and origin['origin_flags']['exclude_text']
        assert origin['origin_flags']['excluded_object_id'] == text.object_id
        assert origin['snapshot_owner'] == id(older)
        assert not rows(controller, 'first_settled')
    finally:
        controller.stop()


def test_completed_origin_is_honest_and_group_document_tag_does_not_relabel_it(
        editor, controller, monkeypatch):
    from comic_editor.ui.document_presentation import PresentedTile, PresentedTileGroup
    tile = PresentedTile(('old', 1), UnreadableArtwork(), UnreadableArtwork())
    canvas, text, _raster, box = group_draw(editor, monkeypatch, (), backend='gpu-pending')
    controller.start()
    try:
        canvas.set_selection('object', text.object_id)
        snapshot = frame(canvas, monkeypatch, pending=True)
        older_revision = snapshot.document.revision - 1
        canvas._projection_completed_view = snapshot.document.configuration, [(None, [tile])], older_revision
        box['groups'] = (PresentedTileGroup((tile,), native_context=(snapshot.document, None)),)
        canvas.grab()
        origin = rows(controller, 'first_paint')[0]['details']['draw_group_provenance'][1]
        assert origin['gpu_pending_replay']
        assert origin['document_tag_revision'] == snapshot.document.revision
        assert origin['origin'] == 'completed_view' and origin['revision'] == older_revision
        assert not origin['origin_flags_available'] and 'origin_flags' not in origin
        assert 'configuration and revision only' in origin['origin_flags_unavailable_reason']
        assert not rows(controller, 'first_settled')
    finally:
        controller.stop()


@pytest.mark.parametrize('boundary', ['clear', 'raster_selection'])
def test_actual_draw_cannot_cross_reentrant_epoch_or_text_action(
        editor, controller, monkeypatch, boundary):
    from comic_editor.ui.document_presentation import PresentedTile, PresentedTileGroup
    tile = PresentedTile(('old', 1), UnreadableArtwork(), UnreadableArtwork())
    callback = lambda: (controller.recorder.clear() if boundary == 'clear'
        else canvas.set_selection('object', raster.object_id))
    canvas, text, raster, _box = group_draw(editor, monkeypatch,
        (PresentedTileGroup((tile,)),), callback=callback)
    controller.start()
    try:
        canvas.set_selection('object', text.object_id)
        frame(canvas, monkeypatch, pending=True)
        canvas.grab()
        assert not rows(controller, 'first_paint') and not rows(controller, 'first_settled')
        assert controller._text_presentation_wait is None
    finally:
        controller.stop()


def test_draw_metadata_is_bounded_and_does_not_retain_groups_or_artwork(
        editor, controller, monkeypatch):
    import gc
    import weakref
    from comic_editor.ui.document_presentation import PresentedTile, PresentedTileGroup
    artwork = UnreadableArtwork()
    tile = PresentedTile(('private', 1), artwork, UnreadableArtwork())
    group = PresentedTileGroup((tile,) * 129)
    group_reference, tile_reference, image_reference = weakref.ref(group), weakref.ref(tile), weakref.ref(artwork)
    canvas, text, _raster, box = group_draw(editor, monkeypatch, (group,) * 9, calls=5)
    controller.start()
    try:
        canvas.set_selection('object', text.object_id)
        frame(canvas, monkeypatch, pending=True)
        canvas.grab()
        metadata = rows(controller, 'first_paint')[0]['details']
        summary, *draws = metadata['draw_group_provenance']
        assert summary['truncated'] and summary['call_count'] == 5
        assert len(draws) == 24
        assert all(item['reference_match_truncated'] and item['group_count'] == 9
                   for item in draws)
        box['groups'] = ()
        del group, tile, artwork
        gc.collect()
        assert group_reference() is tile_reference() is image_reference() is None
        for _ in range(10):
            canvas.grab()
        assert len(rows(controller, 'first_paint')) == 1
    finally:
        controller.stop()


def test_actual_draw_requires_tile_reference_identity_not_shared_image_alias(
        editor, controller, monkeypatch):
    from comic_editor.ui.document_presentation import PresentedTile, PresentedTileGroup
    artwork = UnreadableArtwork()
    original = PresentedTile(('private', 1), artwork, UnreadableArtwork())
    alias = replace(original)
    canvas, text, _raster, _box = group_draw(editor, monkeypatch,
        (PresentedTileGroup((alias,)),))
    controller.start()
    try:
        canvas.set_selection('object', text.object_id)
        snapshot = frame(canvas, monkeypatch, pending=True)
        canvas._scene_controller.overview = snapshot.document, original
        canvas.grab()
        group = rows(controller, 'first_paint')[0]['details']['draw_group_provenance'][1]
        assert group['origin'] == 'unmatched'
    finally:
        controller.stop()
