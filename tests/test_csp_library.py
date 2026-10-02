"""Local CSP imports use generated data and never rewrite library storage."""
from copy import deepcopy
from dataclasses import replace
import io
import sqlite3
import struct
import time
from threading import Event
import xml.etree.ElementTree as ET
import zlib

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog

from comic_editor.core import csp_library
from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.csp_library import (
    CspBrushEntry, CspLibrary, discover_libraries, import_library_brush,
    list_library_brushes, resolve_library,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.sut_import import SutImportError
from comic_editor.ui import csp_library_dialog
from comic_editor.ui.brush_controls import BrushControls
from comic_editor.ui.csp_library_dialog import CspLibraryDialog


def settings_db(path, *, name='Original brush', fragment=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        if fragment:
            c.execute('PRAGMA page_size=1024')
            # Give sqlite_master enough records to use ordinary table leaves.
            for i in range(14):
                c.execute(f'CREATE TABLE Padding{i}(' + ','.join(f'Field{j} TEXT DEFAULT NULL' for j in range(20)) + ')')
        c.execute('CREATE TABLE Manager(Version INTEGER, CommonVariantID INTEGER)')
        c.execute('INSERT INTO Manager VALUES(138, 99)')
        extras = ', ' + ', '.join(f'Extra{i} INTEGER DEFAULT NULL' for i in range(16)) if fragment else ''
        c.execute('CREATE TABLE Node(_PW_ID INTEGER PRIMARY KEY AUTOINCREMENT, NodeUuid BLOB DEFAULT NULL, '
                  'NodeName TEXT DEFAULT NULL, NodeVariantID INTEGER DEFAULT NULL, NodeOutputOp INTEGER DEFAULT NULL'
                  + extras + ')')
        c.execute('INSERT INTO Node(NodeUuid,NodeName,NodeVariantID,NodeOutputOp) VALUES(?,?,?,?)',
                  (bytes(range(16)), name, 1, 10))
        c.execute('CREATE TABLE Variant(_PW_ID INTEGER PRIMARY KEY AUTOINCREMENT, VariantID INTEGER DEFAULT NULL, '
                  'BrushSize REAL DEFAULT NULL, Opacity INTEGER DEFAULT NULL, BrushSizeUnit INTEGER DEFAULT NULL, '
                  'BrushUsePatternImage INTEGER DEFAULT NULL, BrushPatternImageArray BLOB DEFAULT NULL, '
                  'UseDualBrush INTEGER DEFAULT NULL, DualSize REAL DEFAULT NULL, '
                  'DualUsePatternImage INTEGER DEFAULT NULL, DualPatternImageArray BLOB DEFAULT NULL)')
        c.execute('INSERT INTO Variant(VariantID,BrushSize,Opacity) VALUES(1,24,73)')
        c.execute('INSERT INTO Variant(VariantID,BrushSize,Opacity) VALUES(99,7,85)')
    return path


def reference(path='.:tip:data:tip.png'):
    def uint(n):
        return struct.pack('>I', n)
    def string(s):
        b = s.encode('utf-16-le')
        return uint(len(b)) + b
    body = string(path)+uint(2)+string('Actual tip')+uint(1)+string('.:tip')+uint(0)
    return uint(8)+uint(1)+uint(len(body)+4)+body


def container(logical):
    def chunk(kind, payload):
        return struct.pack('<I', len(payload))+kind+payload+struct.pack('<I', zlib.crc32(kind+payload) & 0xffffffff)
    return (b'\x89C2F\r\n\x1a\n' + chunk(b'dATA', b'\x01\x00'+bytes(5128))
            + chunk(b'dATA', b'\x00\x00'+logical[5120:]) + chunk(b'TAIL', b''))


def tool_container(tmp_path, *, fragment=False):
    inner = settings_db(tmp_path/'inner.sqlite', fragment=fragment).read_bytes()
    if fragment:
        # Older materials expose an overflow stream with an unavailable header.
        stream = bytes(100)+inner[100:]
        stream += bytes((-len(stream)) % 1020)
        pages = len(stream)//1020
        logical = bytes(5120)+b''.join(struct.pack('>I', i+7 if i+1 < pages else 0)
                    + stream[i*1020:(i+1)*1020] for i in range(pages))
    else:
        outer = tmp_path/'outer.sqlite'
        with sqlite3.connect(outer) as c:
            c.execute('PRAGMA page_size=4096')
            c.execute('CREATE TABLE Padding(Data BLOB)')
            c.execute('INSERT INTO Padding VALUES(?)', (bytes(10000),))
            c.execute('CREATE TABLE Material(ToolData BLOB)')
            c.execute('INSERT INTO Material VALUES(?)', (inner,))
        logical = outer.read_bytes()
    path = tmp_path/'material.tool'
    path.write_bytes(container(logical))
    return path


def catalog_library(tmp_path):
    common = tmp_path/'CELSYS/CLIPStudioCommon'
    material = common/'Material'
    folder = material/'03/7f/pack'
    folder.mkdir(parents=True)
    root = ET.Element('archive')
    catalog = ET.SubElement(root, 'catalog')
    items = ET.SubElement(ET.SubElement(catalog, 'groups'), 'items')
    files = ET.SubElement(root, 'files')
    for uuid, name in [('one', 'Brush Alpha'), ('two', 'Brush Beta')]:
        item = ET.SubElement(items, 'item', uuid=uuid)
        ET.SubElement(item, 'type').text = 'brush'
        ET.SubElement(item, 'name').text = name
        ET.SubElement(ET.SubElement(item, 'data'), 'fileref', idref=uuid)
        ET.SubElement(ET.SubElement(files, 'file', id=uuid), 'path').text = f'{uuid}/data/material_0.tool'
        settings_db(folder/f'{uuid}/data/material_0.tool', name=name)
    ET.ElementTree(root).write(folder/'catalog.xml', encoding='utf-8')
    db = common/'MaterialDB/CatalogMaterial.cmdb'
    db.parent.mkdir()
    with sqlite3.connect(db) as c:
        c.executescript("""CREATE TABLE MaterialModifier(_PW_ID INTEGER, MaterialName TEXT, MaterialUuid TEXT, Path TEXT);
            CREATE TABLE SystemTag(_PW_ID INTEGER, Title TEXT);
            CREATE TABLE TableSystemTagElementMap(ElementId INTEGER, TagId INTEGER);
            INSERT INTO SystemTag VALUES(1,'BrushTool'), (2,'BrushPattern');
            INSERT INTO MaterialModifier VALUES(1,'Brush Alpha','one','.:03:7f:pack'),
                (2,'Brush Beta','two','.:03:7f:pack'), (3,'Only a tip','tip','.:other');
            INSERT INTO TableSystemTagElementMap VALUES(1,1),(2,1),(3,2);
        """)
    return CspLibrary(common, material)


def test_catalog_names_and_pack_paths_only_show_brushes(tmp_path):
    library = catalog_library(tmp_path)
    before = {p: p.read_bytes() for p in library.common.rglob('*') if p.is_file()}
    entries, notes = list_library_brushes(library)
    assert notes == []
    assert [e.name for e in entries] == ['Brush Alpha', 'Brush Beta']
    assert entries[1].path == library.material/'03/7f/pack/two/data/material_0.tool'
    brush = import_library_brush(entries[1])
    assert brush.name == 'Brush Beta' and brush.size == 24 and brush.opacity == .73
    assert brush.source['format'] == 'csp-library'
    assert before == {p: p.read_bytes() for p in library.common.rglob('*') if p.is_file()}


@pytest.mark.parametrize('fragment', [False, True])
def test_downloaded_tool_container_retains_settings_and_source(tmp_path, fragment):
    path = tool_container(tmp_path, fragment=fragment)
    before = path.read_bytes()
    entry = CspBrushEntry('Catalog brush name', 'material', path, tmp_path, material_uuid='brush')
    brush = import_library_brush(entry)
    assert brush.name == 'Catalog brush name' and brush.size == 24 and brush.opacity == .73
    assert path.read_bytes() == before
    assert brush.source['node']['NodeName'] == 'Original brush'
    assert any('pressure calibration' in n for n in brush.warnings) == fragment
    assert import_library_brush(entry) == brush


def test_installed_active_variant_inheritance_dual_tips_and_portable_copy(tmp_path):
    library = catalog_library(tmp_path)
    path = settings_db(library.common.parent/'CLIPStudioPaintVer1_5_0/Tool/EditImageTool.todb')
    tip = library.material/'tip/data/tip.png'
    tip.parent.mkdir(parents=True)
    Image.new('RGBA', (3, 5), (123, 75, 20, 255)).save(tip)
    with sqlite3.connect(path) as c:
        c.execute('UPDATE Variant SET Opacity=NULL, BrushUsePatternImage=1, BrushPatternImageArray=?, '
                  'UseDualBrush=1, DualSize=9, DualUsePatternImage=1, DualPatternImageArray=? WHERE VariantID=1',
                  (reference(), reference()))
        c.execute("INSERT INTO Node(NodeName,NodeVariantID,NodeOutputOp) VALUES('Fill',1,2)")
    before = path.read_bytes()
    entries, notes = list_library_brushes(library)
    entry = next(e for e in entries if e.kind == 'installed')
    assert sum(e.kind == 'installed' for e in entries) == 1 and not notes
    brush = import_library_brush(entry)
    assert brush.size == 24 and brush.opacity == .85
    assert brush.tips[0].width == 3 and brush.tips[0].height == 5 and brush.tips[0].png
    assert brush.dual is not None and brush.dual.size == 9 and brush.dual.tips[0].png
    assert brush.dual.source['format'] == 'csp-library'
    assert path.read_bytes() == before
    # Changes to another brush must not alter the selected brush's fingerprint.
    with sqlite3.connect(path) as c:
        c.execute('INSERT INTO Variant(VariantID,BrushSize) VALUES(345,101)')
        c.execute('ALTER TABLE Manager ADD COLUMN SavedCount INTEGER')
        c.execute('UPDATE Manager SET SavedCount=999')
    assert import_library_brush(entry) == brush
    tip.unlink()
    roundtrip = BrushDefinition.from_dict(brush.to_dict())
    assert roundtrip == brush
    with pytest.raises(SutImportError, match='missing material'):
        import_library_brush(entry)


def test_discovery_and_manual_folders_honor_configured_material_storage(tmp_path, monkeypatch):
    library = catalog_library(tmp_path/'Roaming/CELSYSUserData')
    monkeypatch.setenv('APPDATA', str(tmp_path/'Roaming'))
    monkeypatch.setattr(csp_library.Path, 'home', lambda: tmp_path/'NoOtherInstall')
    assert discover_libraries() == [library]
    for path in (library.common, library.common.parent, library.common.parent.parent, library.material):
        assert resolve_library(path) == library
    moved = tmp_path/'Moved/Material'
    moved.mkdir(parents=True)
    config = library.common/'Preference/Config.sqlite'
    config.parent.mkdir()
    with sqlite3.connect(config) as c:
        c.execute('CREATE TABLE CategoryFolderPath(DataFolderPath TEXT)')
        c.execute('INSERT INTO CategoryFolderPath VALUES(?)', (str(moved.parent),))
    assert resolve_library(library.common).material == moved.resolve()
    assert resolve_library(library.material).material == library.material.resolve()
    assert resolve_library(moved) == CspLibrary(library.common, moved.resolve())


@pytest.mark.parametrize('relative', ['.:..:secret', '.:C:/secret', 'C:\\secret', '/secret', '../secret'])
def test_catalog_paths_cannot_escape_storage_root(tmp_path, relative):
    with pytest.raises(SutImportError, match='path'):
        csp_library._within(tmp_path, relative, colon=relative.startswith('.:'))


def test_bad_catalog_does_not_hide_installed_brushes(tmp_path):
    library = catalog_library(tmp_path)
    (library.common/'MaterialDB/CatalogMaterial.cmdb').write_bytes(b'not a database')
    settings_db(library.common.parent/'CLIPStudioPaintVer1_5_0/Tool/EditImageTool.todb')
    entries, notes = list_library_brushes(library)
    assert len(entries) == 1 and entries[0].kind == 'installed'
    assert 'Downloaded materials' in notes[0]


def test_missing_secondary_tips_are_rejected_before_decoding(tmp_path, monkeypatch):
    path = settings_db(tmp_path/'tools.todb')
    with sqlite3.connect(path) as c:
        c.execute('UPDATE Variant SET UseDualBrush=1, DualUsePatternImage=1 WHERE VariantID=1')
    monkeypatch.setattr(csp_library, '_build_definition', lambda *a, **kw: pytest.fail('Decoded incomplete brush'))
    entry = CspBrushEntry('Incomplete brush', 'installed', path, tmp_path, node_id=1)
    with pytest.raises(SutImportError, match='secondary image-tip list is empty'):
        import_library_brush(entry)


def wait_for(qapp, predicate):
    deadline = time.monotonic()+8
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(10)
    assert predicate()


def test_picker_filters_selects_and_imports_without_blocking_ui(qapp, tmp_path, monkeypatch):
    library = catalog_library(tmp_path)
    settings_db(library.common.parent/'CLIPStudioPaintVer1_5_0/Tool/EditImageTool.todb')
    monkeypatch.setattr(csp_library_dialog, 'discover_libraries', lambda: [library])
    dialog = CspLibraryDialog()
    dialog.show()
    try:
        wait_for(qapp, lambda: len(dialog.entries) == 3)
        assert not dialog.import_button.isEnabled()
        dialog.source.setCurrentIndex(1)
        dialog.search.setText('beta')
        assert dialog.count.text() == '1 of 3 brushes'
        index = next(i for i, e in enumerate(dialog.entries) if e.name == 'Brush Beta')
        dialog.brushes.setCurrentRow(index)
        assert dialog.import_button.isEnabled()
        dialog.search.setText('alpha')
        assert dialog.selected_entry() is None and not dialog.import_button.isEnabled()
        dialog.search.setText('beta')
        dialog.brushes.setCurrentRow(index)
        dialog.import_button.click()
        wait_for(qapp, lambda: dialog.result() == QDialog.Accepted)
        assert dialog.definition.name == 'Brush Beta'
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_sidebar_button_opens_picker_and_preserves_edited_reimports(qapp, monkeypatch):
    brush = BrushDefinition(id='library-test', name='Library brush', size=13)
    original = replace(brush, size=71)
    settings = EditorSettings(brush_presets=[original.to_dict()], active_brush_id=original.id)
    controls = BrushControls(settings)
    controls.resize(198, 520)
    controls.show()
    qapp.processEvents()
    left = controls.import_button.mapTo(controls, QPoint())
    right = controls.library_import_button.mapTo(controls, QPoint())
    assert left.y() == right.y() and left.x()+controls.import_button.width() <= right.x()
    assert right.x()+controls.library_import_button.width() <= controls.width()

    class Picker:
        definition = brush
        def __init__(self, parent):
            assert parent is controls
        def exec(self):
            return QDialog.Accepted
        def deleteLater(self):
            pass

    monkeypatch.setattr(csp_library_dialog, 'CspLibraryDialog', Picker)
    before = deepcopy(settings.brush_presets)
    controls.library_import_button.click()
    assert settings.brush_presets[0] == before[0]
    assert len(settings.brush_presets) == 2 and settings.active_paint_brush().size == 13
    controls.library_import_button.click()
    assert len(settings.brush_presets) == 2
    Picker.exec = lambda self: QDialog.Rejected
    after = deepcopy(settings.brush_presets)
    controls.library_import_button.click()
    assert settings.brush_presets == after
    controls.deleteLater()


def test_cancel_before_initial_scan_and_missing_library_are_safe(qapp, monkeypatch):
    monkeypatch.setattr(csp_library_dialog, 'discover_libraries', lambda: [])
    dialog = CspLibraryDialog()
    dialog.reject()
    qapp.processEvents()
    assert dialog._future is None and dialog.definition is None
    dialog.deleteLater()
    dialog = CspLibraryDialog()
    dialog.show()
    qapp.processEvents()
    assert 'No Clip Studio Paint library' in dialog.status.text()
    assert dialog.browse.isEnabled() and not dialog.import_button.isEnabled()
    dialog.reject()
    dialog.deleteLater()


def test_failed_import_leaves_picker_open_and_does_not_publish(qapp, tmp_path, monkeypatch):
    library = catalog_library(tmp_path)
    (library.material/'03/7f/pack/two/data/material_0.tool').write_bytes(b'bad brush')
    monkeypatch.setattr(csp_library_dialog, 'discover_libraries', lambda: [library])
    dialog = CspLibraryDialog()
    dialog.show()
    try:
        wait_for(qapp, lambda: len(dialog.entries) == 2)
        dialog.brushes.setCurrentRow(1)
        dialog.import_button.click()
        wait_for(qapp, lambda: 'import failed' in dialog.status.text())
        assert dialog.isVisible() and dialog.definition is None and dialog.import_button.isEnabled()
        dialog.brushes.setCurrentRow(0)
        dialog.import_button.click()
        wait_for(qapp, lambda: dialog.result() == QDialog.Accepted)
        assert dialog.definition.name == 'Brush Alpha'
    finally:
        dialog.reject()
        dialog.deleteLater()


def test_cancel_during_import_does_not_accept_late_result(qapp, tmp_path, monkeypatch):
    library = catalog_library(tmp_path)
    monkeypatch.setattr(csp_library_dialog, 'discover_libraries', lambda: [library])
    started, released = Event(), Event()
    def delayed_import(entry):
        started.set()
        released.wait(5)
        return BrushDefinition(name=entry.name)
    monkeypatch.setattr(csp_library_dialog, 'import_library_brush', delayed_import)
    dialog = CspLibraryDialog()
    dialog.show()
    try:
        wait_for(qapp, lambda: len(dialog.entries) == 2)
        dialog.brushes.setCurrentRow(0)
        dialog.import_button.click()
        wait_for(qapp, started.is_set)
        future = dialog._future
        dialog.reject()
        released.set()
        future.result(timeout=5)
        qapp.processEvents()
        assert dialog.result() == QDialog.Rejected and dialog.definition is None
    finally:
        released.set()
        dialog.reject()
        dialog.deleteLater()


@pytest.mark.parametrize('accepted', [False, True])
def test_library_import_resolution_is_chosen_before_publishing(qapp, tmp_path, monkeypatch, accepted):
    path = settings_db(tmp_path/'tools.todb')
    with sqlite3.connect(path) as c:
        c.execute('UPDATE Variant SET BrushSize=25.4, BrushSizeUnit=2 WHERE VariantID=1')
    brush = import_library_brush(CspBrushEntry('Millimeter brush', 'installed', path, tmp_path, node_id=1))
    controls = BrushControls(EditorSettings())
    before = deepcopy(controls.settings.brush_presets)
    class Picker:
        definition = brush
        def __init__(self, parent):
            pass
        def exec(self):
            return QDialog.Accepted
        def deleteLater(self):
            pass
    def resolution(*args):
        assert controls.settings.brush_presets == before
        return 600., accepted
    monkeypatch.setattr(csp_library_dialog, 'CspLibraryDialog', Picker)
    monkeypatch.setattr('comic_editor.ui.brush_controls.QInputDialog.getDouble', resolution)
    monkeypatch.setattr('comic_editor.ui.brush_controls.QMessageBox.exec', lambda self: 0)
    controls.library_import_button.click()
    if accepted:
        assert controls.settings.active_paint_brush().size == pytest.approx(600.)
    else:
        assert controls.settings.brush_presets == before
    controls.deleteLater()
