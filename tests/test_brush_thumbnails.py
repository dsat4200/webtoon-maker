"""Grid browsing reads persistent previews; imports/edits prepare them ahead."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QListView, QWidget

from comic_editor.core import brush_thumbnails as thumbs, settings as settings_module
from comic_editor.core.brush_preview import render_brush_preview
from comic_editor.core.brushes import BrushDefinition, BrushTip, default_brushes
from comic_editor.core.settings import EditorSettings
from comic_editor.ui import brush_preview_queue as previews, brush_thumbnail_store as stores
from comic_editor.ui.brush_controls import BrushControls, BrushPresetCombo
from comic_editor.ui.brush_thumbnail_store import BrushThumbnailStore


@pytest.fixture
def prepared_queue(qapp, monkeypatch, tmp_path):
    queue = previews.BrushPreviewQueue()
    calls = []
    def render(definition, width, height, *colors):
        calls.append((definition, colors))
        yield None
        image = QImage(width, height, QImage.Format_ARGB32)
        image.fill(QColor.fromRgb(round(255*definition.opacity), 40, 80))
        yield image
    monkeypatch.setattr(previews, 'iter_brush_preview', render)
    monkeypatch.setattr(stores, 'preview_queue', lambda: queue)
    monkeypatch.setattr(settings_module, 'settings_path', lambda: tmp_path/'settings.json')
    monkeypatch.setattr(thumbs, 'BUNDLED_THUMBNAILS', tmp_path/'no-bundled-thumbnails')
    previews.PREVIEW_CACHE.clear()
    yield queue, calls
    queue.timer.stop()
    if queue.active:
        queue._close(queue.active)
    queue.pending.clear()
    queue.deleteLater()
    previews.PREVIEW_CACHE.clear()


def finish(store, queue):
    for _ in range(10000):
        store._timer.stop()
        queue.timer.stop()
        if not store.pending and store.current is None:
            return
        store._prepare_next()
        queue._tick()
    pytest.fail('Thumbnail warming did not finish')


def test_fingerprint_is_stable_across_sessions_and_ignores_labels_and_notes(tmp_path):
    brush = BrushDefinition(name='Original name', source={'private': 'provenance'},
                            warnings=('Some note',), dual=BrushDefinition(name='Secondary'),
                            tips=(default_brushes()[5].tips[0],))
    key = thumbs.thumbnail_key(brush)
    assert thumbs.thumbnail_key(replace(brush, id='different', name='Renamed', source={}, warnings=(),
                                      dual=replace(brush.dual, name='Another name', id='another'))) == key
    assert thumbs.thumbnail_key(replace(brush, opacity=.3)) != key
    assert thumbs.thumbnail_key(replace(brush, dual=replace(brush.dual, angle=45))) != key
    assert thumbs.thumbnail_key(replace(brush, tips=(BrushTip(png='different pixels'),))) != key
    source = tmp_path/'brush.json'
    source.write_text(json.dumps(brush.to_dict()), encoding='utf-8')
    code = ('import json,sys; from comic_editor.core.brushes import BrushDefinition; '
            'from comic_editor.core.brush_thumbnails import thumbnail_key; '
            'print(thumbnail_key(BrushDefinition.from_dict(json.load(open(sys.argv[1],encoding="utf-8")))))')
    assert subprocess.check_output([sys.executable, '-c', code, str(source)], text=True).strip() == key


def test_75_previews_survive_memory_cache_eviction_and_new_app_session(qapp, prepared_queue, tmp_path):
    queue, calls = prepared_queue
    brushes = [BrushDefinition(id=str(i), name=f'Brush {i}', size=12+i) for i in range(75)]
    presets = [b.to_dict() for b in brushes]
    before = deepcopy(presets)
    store = BrushThumbnailStore(preferences=tmp_path/'settings.json')
    store.sync(presets)
    finish(store, queue)
    assert len(calls) == 75 and len(previews.PREVIEW_CACHE) == 64
    assert len(list((tmp_path/'brush-thumbnails').glob('*.png'))) == 75
    assert presets == before
    store.stop()
    previews.PREVIEW_CACHE.clear()
    fresh = BrushThumbnailStore(preferences=tmp_path/'settings.json')
    fresh.sync(presets)
    assert not fresh.pending and len(fresh.pixmaps) == 75
    assert all(not fresh.icon(b.id).isNull() for b in brushes)
    assert len(calls) == 75
    store.deleteLater()
    fresh.deleteLater()


def test_edit_replaces_just_its_thumbnail_and_rename_reuses_pixels(qapp, prepared_queue, tmp_path):
    queue, calls = prepared_queue
    a, b = BrushDefinition(id='a', opacity=.7), BrushDefinition(id='b', size=37)
    presets = [a.to_dict(), b.to_dict()]
    store = BrushThumbnailStore(preferences=tmp_path/'settings.json')
    store.sync(presets)
    finish(store, queue)
    first = dict(store.keys)
    unchanged_stamp = thumbs.thumbnail_path(first['b'], store.preferences).stat().st_mtime_ns
    # Dicts in settings are mutable: detect an in-place parameter edit too.
    presets[0]['opacity'] = .23
    store.sync(presets)
    assert len(store.pending) == 1
    finish(store, queue)
    assert store.keys['a'] != first['a'] and store.keys['b'] == first['b'] and len(calls) == 3
    assert thumbs.thumbnail_path(first['b'], store.preferences).stat().st_mtime_ns == unchanged_stamp
    assert store.pixmaps[store.keys['a']].toImage().pixelColor(0, 0).red() == round(255*.23)
    presets[0]['name'] = 'My renamed brush'
    store.sync(presets)
    assert not store.pending and len(calls) == 3
    store.stop()
    store.deleteLater()


def test_superseded_prerender_cannot_publish_old_pixels(qapp, prepared_queue, tmp_path):
    queue, calls = prepared_queue
    store = BrushThumbnailStore(preferences=tmp_path/'settings.json')
    a = BrushDefinition(opacity=.71)
    store.sync([a.to_dict()])
    store._prepare_next()
    queue.max_steps = 1
    queue._tick()
    old = store.current
    store.sync([replace(a, opacity=.25).to_dict()])
    assert queue.active is None and store.current is None
    finish(store, queue)
    assert old not in store.pixmaps and not thumbs.thumbnail_path(old, store.preferences).exists()
    assert store.pixmaps[store.keys[a.id]].toImage().pixelColor(0, 0).red() == round(255*.25)
    store.deleteLater()


def test_corrupt_derived_cache_regenerates_once_without_changing_brush(qapp, prepared_queue, tmp_path):
    queue, calls = prepared_queue
    brush = BrushDefinition(size=23)
    presets = [brush.to_dict()]
    store = BrushThumbnailStore(preferences=tmp_path/'settings.json')
    store.sync(presets)
    finish(store, queue)
    path = thumbs.thumbnail_path(store.keys[brush.id], store.preferences)
    path.write_bytes(b'corrupt thumbnail')
    previews.PREVIEW_CACHE.clear()
    fresh = BrushThumbnailStore(preferences=store.preferences)
    fresh.sync(presets)
    finish(fresh, queue)
    assert len(calls) == 2 and not QImage(str(path)).isNull()
    assert presets == [brush.to_dict()]
    fresh.sync(presets)
    assert not fresh.pending
    store.deleteLater()
    fresh.deleteLater()


def test_import_and_edit_prepare_thumbnails_before_picker_opens(qapp, prepared_queue):
    queue, calls = prepared_queue
    controls = BrushControls(EditorSettings(brush_presets=[BrushDefinition(size=17).to_dict()]))
    store = controls.presets.thumbnails
    finish(store, queue)
    imported = BrushDefinition(id='imported', name='Imported brush', size=35)
    controls._accept_import(imported)
    assert controls.presets._grid is None and store.pending
    finish(store, queue)
    assert not controls.presets.itemIcon(controls.presets.findData(imported.id)).isNull()
    controls._publish(replace(imported, density=.3))
    assert store.pending
    finish(store, queue)
    assert len(calls) == 3
    controls.presets.set_colors(QColor('red'), QColor('green'))
    controls.refresh()
    assert not store.pending and len(calls) == 3
    store.stop()
    controls.deleteLater()


def test_large_grid_scroll_filter_selection_and_reopening_never_render(qapp, prepared_queue):
    queue, calls = prepared_queue
    brushes = [BrushDefinition(id=str(i), name=f'Brush {i:02d}', size=12+i) for i in range(75)]
    settings = EditorSettings(brush_presets=[b.to_dict() for b in brushes], active_brush_id='0')
    # A real owner window restores native focus after the popup closes.
    from PySide6.QtWidgets import QWidget
    window = QWidget()
    combo = BrushPresetCombo(settings, window)
    for b in brushes:
        combo.addItem(b.name, b.id)
    combo.refresh_thumbnails()
    finish(combo.thumbnails, queue)
    combo.resize(180, 30)
    window.resize(combo.size())
    window.show()
    combo.show()
    if qapp.platformName() not in {"offscreen", "minimal"}:
        window.activateWindow()
        assert QTest.qWaitForWindowActive(window, 10000)
    assert combo.sizeHint().height() < thumbs.THUMBNAIL_HEIGHT
    count = len(calls)
    QTest.mouseClick(combo, Qt.LeftButton, pos=QPoint(combo.width()-12, combo.height()//2))
    qapp.processEvents()
    popup = combo._grid
    assert popup.width() > 3*combo.width()
    assert popup.grid.viewMode() == QListView.IconMode
    first, second = popup.grid.visualItemRect(popup.grid.item(0)), popup.grid.visualItemRect(popup.grid.item(1))
    assert first.y() == second.y() and first.x() < second.x()
    assert popup.grid.visualItemRect(popup.grid.item(10)).y() > first.y()
    assert popup.grid.item(0).text() == 'Brush 00' and not popup.grid.item(0).icon().isNull()
    popup.grid.verticalScrollBar().setValue(popup.grid.verticalScrollBar().maximum())
    qapp.processEvents()
    popup.search.setText('brush 74')
    assert popup.count.text() == '1 / 75'
    popup.search.setFocus()
    QTest.keyClick(popup.search, Qt.Key_Return)
    assert combo.currentData() == '74' and not popup.isVisible()
    if qapp.platformName() not in {"offscreen", "minimal"}:
        window.activateWindow()
        assert QTest.qWaitForWindowActive(window, 10000)
    combo.setFocus()
    QTest.keyClick(combo, Qt.Key_Down, Qt.AltModifier)
    qapp.processEvents()
    assert popup.isVisible()
    QTest.keyClick(popup.grid, Qt.Key_Escape)
    assert not combo._popup_open and not popup.isVisible()
    combo.set_colors(QColor('blue'), QColor('yellow'))
    combo.showPopup()
    qapp.processEvents()
    combo.hidePopup()
    assert len(calls) == count and not combo.thumbnails.pending
    combo.thumbnails.stop()
    combo.close()
    combo.deleteLater()
    window.close()
    window.deleteLater()


def test_hidden_cache_warming_pauses_during_live_drawing(qapp, prepared_queue):
    from types import SimpleNamespace
    queue, calls = prepared_queue
    owner = QWidget()
    owner.canvas = SimpleNamespace(_paint_brush_stroke=object())
    store = BrushThumbnailStore(owner)
    store.sync([BrushDefinition(size=27).to_dict()])
    store._prepare_next()
    queue._tick()
    assert not calls
    owner.canvas._paint_brush_stroke = None
    finish(store, queue)
    assert calls and store.pixmaps
    store.stop()
    owner.deleteLater()


def test_unchanged_library_sync_reuses_detached_records_without_hashing_or_disk_reads(
        qapp, prepared_queue, monkeypatch):
    queue, calls = prepared_queue
    presets = [BrushDefinition(id=str(i), size=12+i).to_dict() for i in range(75)]
    store = BrushThumbnailStore()
    store.sync(presets)
    finish(store, queue)
    original_keys = dict(store.keys)
    original_records = dict(store._input_records)
    before = deepcopy(presets)

    def unexpected(*args, **kwargs):
        pytest.fail('Unchanged presets must reuse their prepared thumbnails')
    monkeypatch.setattr(BrushDefinition, 'from_dict', unexpected)
    monkeypatch.setattr(thumbs, 'thumbnail_key', unexpected)
    monkeypatch.setattr(thumbs, 'load_thumbnail', unexpected)
    store.sync(deepcopy(presets))
    store.sync(list(reversed(presets)))
    assert store.keys == original_keys and not store.pending
    assert all(store._input_records[key] is record for key, record in original_records.items())
    assert presets == before and len(calls) == 75
    store.sync(presets[:3])
    assert set(store.keys) == {'0', '1', '2'}
    assert set(store._input_records) == set(store._records) == set(store.keys)
    assert len(store.pixmaps) == 3
    store.stop()
    store.deleteLater()


def test_rebuilt_outline_picker_borrows_same_window_library_without_rehash_or_disk_reads(
        qapp, prepared_queue, monkeypatch):
    from comic_editor.core.models import OutlineModifier
    from comic_editor.ui.outline_brush_controls import OutlineBrushCombo
    queue, calls = prepared_queue
    presets = [BrushDefinition(id=str(i), size=12+i).to_dict() for i in range(75)]
    settings = EditorSettings(brush_presets=presets)
    window = QWidget()
    library = BrushPresetCombo(settings, window)
    finish(library.thumbnails, queue)
    before = deepcopy(presets)
    modifier = OutlineModifier(brush=deepcopy(presets[5]))
    original_key = thumbs.thumbnail_key
    original_parse = BrushDefinition.from_dict
    hashed, parsed = [], []
    def fingerprint(definition):
        hashed.append(definition.id)
        return original_key(definition)
    def parse(data):
        parsed.append(data['id'])
        return original_parse(data)
    def unexpected_read(*args):
        pytest.fail('Prepared library pixels should not be read again from disk')
    monkeypatch.setattr(thumbs, 'thumbnail_key', fingerprint)
    monkeypatch.setattr(BrushDefinition, 'from_dict', parse)
    monkeypatch.setattr(thumbs, 'load_thumbnail', unexpected_read)
    for _ in range(4):
        combo = OutlineBrushCombo(modifier, settings, window)
        combo.refresh_choices()
        assert combo.thumbnails._source_store is library.thumbnails
        assert combo.thumbnails.keys[combo.saved_id] == library.thumbnails.keys['5']
        assert not combo.thumbnails.pending
        assert not combo.itemIcon(combo.findData(combo.saved_id)).isNull()
        for identifier, prepared in library.thumbnails._input_records.items():
            assert combo.thumbnails._input_records[identifier] is prepared
            key = library.thumbnails.keys[identifier]
            assert combo.thumbnails.pixmaps[key].toImage() == library.thumbnails.pixmaps[key].toImage()
        combo.thumbnails.stop()
        combo.deleteLater()
    assert hashed == parsed == [f'outline-saved-{modifier.modifier_id}'] * 4
    assert presets == before and modifier.brush == before[5] and len(calls) == 75
    library.thumbnails.stop()
    window.deleteLater()


@pytest.mark.parametrize('part', ['tip', 'texture', 'dynamics', 'dual', 'taper'])
def test_borrowed_snapshots_detect_nested_in_place_material_and_response_edits(
        qapp, prepared_queue, part):
    from comic_editor.core.brushes import BrushDynamics, BrushTexture
    queue, _ = prepared_queue
    brush = BrushDefinition(id='nested', tips=(BrushTip(png='original-tip'),),
                            texture=BrushTexture(png='original-texture'),
                            dynamics={'size': BrushDynamics(pressure=True)},
                            dual=BrushDefinition(angle=10), taper_minima={'size': .2})
    presets = [brush.to_dict()]
    source = BrushThumbnailStore()
    source.sync(presets)
    finish(source, queue)
    borrowed = BrushThumbnailStore(source_store=source)
    borrowed.sync(presets)
    old_record = source._input_records[brush.id]
    old_key = source.keys[brush.id]
    if part == 'tip':
        presets[0]['tips'][0]['png'] = 'edited-tip'
    elif part == 'texture':
        presets[0]['texture']['png'] = 'edited-texture'
    elif part == 'dynamics':
        presets[0]['dynamics']['size']['pressure_curve'] = [(0, 0), (.5, .25), (1, 1)]
    elif part == 'dual':
        presets[0]['dual']['angle'] = 45
    else:
        presets[0]['taper_minima']['size'] = .31
    expected = thumbs.thumbnail_key(BrushDefinition.from_dict(presets[0]))
    borrowed.sync(presets)
    assert borrowed.keys[brush.id] == expected != old_key
    assert borrowed._input_records[brush.id] is not old_record
    assert old_record[0] == brush.to_dict()
    assert source.keys[brush.id] == old_key
    finish(borrowed, queue)
    source.sync(presets)
    finish(source, queue)
    assert source.keys == borrowed.keys
    assert source.pixmaps[expected].toImage() == borrowed.pixmaps[expected].toImage()
    source.stop()
    borrowed.stop()
    source.deleteLater()
    borrowed.deleteLater()


def test_thumbnail_borrowing_does_not_cross_preferences_and_observes_renderer_version(
        qapp, prepared_queue, monkeypatch, tmp_path):
    queue, _ = prepared_queue
    presets = [BrushDefinition(size=29).to_dict()]
    source = BrushThumbnailStore(preferences=tmp_path/'first'/'settings.json')
    source.sync(presets)
    finish(source, queue)
    old_key = source.keys[presets[0]['id']]
    other = BrushThumbnailStore(preferences=tmp_path/'second'/'settings.json', source_store=source)
    other.sync(presets)
    assert other.pending and not other.pixmaps
    assert other._input_records[presets[0]['id']] is not source._input_records[presets[0]['id']]
    finish(other, queue)
    monkeypatch.setattr(thumbs, 'THUMBNAIL_RENDER_VERSION', thumbs.THUMBNAIL_RENDER_VERSION + 1)
    source.sync(presets)
    assert source.keys[presets[0]['id']] != old_key
    assert source.pending and not source.pixmaps
    source.stop()
    other.stop()
    source.deleteLater()
    other.deleteLater()


def test_bundled_brush_set_is_prerendered_and_matches_current_renderer(qapp):
    for brush in default_brushes():
        path = thumbs.BUNDLED_THUMBNAILS / (thumbs.thumbnail_key(brush)+'.png')
        assert path.is_file(), f'Built-in thumbnail missing for {brush.name}'
        image = QImage(str(path))
        assert image.size().width() == thumbs.THUMBNAIL_WIDTH and image.size().height() == thumbs.THUMBNAIL_HEIGHT
        expected = render_brush_preview(brush, thumbs.THUMBNAIL_WIDTH, thumbs.THUMBNAIL_HEIGHT, thumbs.THUMBNAIL_COLOR)
        assert image.convertToFormat(QImage.Format_ARGB32_Premultiplied) == expected, brush.name
