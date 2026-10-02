"""Read local CSP material catalogs and installed subtools without changing them.

Downloaded .tool containers and installed .todb rows are normalized directly;
no SUT export, source database writes, or extraction into CSP storage is needed.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tarfile
import xml.etree.ElementTree as ET

from .brushes import BrushDefinition
from .brush_units import DEFAULT_IMPORT_DPI, validate_import_dpi
from .sut_import import (
    MAX_FILE_BYTES, SutImportError, _SQLITE, _build_definition, _c2f_payload,
    _c2f_rows, _json_value, _references, _sqlite_record, _u32, _varint,
)


@dataclass(frozen=True)
class CspLibrary:
    common: Path
    material: Path


@dataclass(frozen=True)
class CspBrushEntry:
    name: str
    kind: str  # "material" or "installed"
    path: Path
    material_root: Path
    node_id: int | None = None
    material_uuid: str = ""
    thumbnail: Path | None = None


@contextmanager
def _readonly(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        _configure_reader(connection)
        # Pin one consistent view while CSP may be saving its own settings.
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.close()


def _configure_reader(connection):
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA trusted_schema=OFF')
    connection.set_progress_handler(lambda: 1, 5000000)


def _tables(connection):
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _within(root: Path, relative: str, *, colon=False) -> Path:
    """Resolve catalog paths within their storage root, including symlinks."""
    parts = relative.split(':') if colon else relative.replace('\\', '/').split('/')
    if colon and parts and parts[0] == '.':
        parts = parts[1:]
    if not parts or any(not p or p in ('.', '..') or ':' in p or '/' in p or '\\' in p for p in parts):
        raise SutImportError('The CSP catalog contains an invalid material path.')
    root = root.resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise SutImportError('The CSP material path is outside its library.')
    return target


def _default_library_folders():
    candidates = []
    if os.environ.get('APPDATA'):
        candidates.append(Path(os.environ['APPDATA']) / 'CELSYSUserData/CELSYS')
    candidates.append(Path.home() / 'Documents/CELSYS')
    for key in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
        if os.environ.get(key):
            candidates.append(Path(os.environ[key]) / 'Documents/CELSYS')
    return candidates


def resolve_library(path: str | Path) -> CspLibrary:
    """Accept CELSYSUserData, CELSYS, CLIPStudioCommon, or Material folders."""
    path = Path(path).expanduser().resolve()
    for common in (path, path / 'CLIPStudioCommon', path / 'CELSYS/CLIPStudioCommon',
                   path / 'CELSYSUserData/CELSYS/CLIPStudioCommon', path.parent):
        if (common / 'MaterialDB/CatalogMaterial.cmdb').is_file() or (
                common.name == 'CLIPStudioCommon' and common.is_dir()):
            material = path if path.name == 'Material' else common / 'Material'
            config = common / 'Preference/Config.sqlite'
            if config.is_file() and path.name != 'Material':
                try:
                    with _readonly(config) as c:
                        if 'CategoryFolderPath' in _tables(c):
                            row = c.execute('SELECT DataFolderPath FROM CategoryFolderPath LIMIT 1').fetchone()
                            if row and row[0]:
                                moved = Path(os.path.expandvars(row[0])).expanduser()
                                for candidate in (moved / 'Material', moved / 'CLIPStudioCommon/Material',
                                                  moved / 'CELSYS/CLIPStudioCommon/Material', moved):
                                    if candidate.is_dir() and candidate.name == 'Material':
                                        material = candidate
                                        break
                except (sqlite3.Error, OSError, TypeError):
                    pass  # The folder picker still permits manual selection.
            return CspLibrary(common, material.resolve())
    if path.name == 'Material' and path.is_dir():
        # CSP may move only material storage while keeping its catalog and
        # tool settings in the normal user-data folder.
        for default in _default_library_folders():
            try:
                library = resolve_library(default)
            except (OSError, ValueError):
                continue
            return CspLibrary(library.common, path)
    raise SutImportError('Choose the CELSYS or CLIPStudioCommon folder containing your CSP library.')


def discover_libraries() -> list[CspLibrary]:
    libraries = []
    for path in _default_library_folders():
        try:
            library = resolve_library(path)
        except (OSError, ValueError):
            continue
        if library not in libraries:
            libraries.append(library)
    return libraries


def list_library_brushes(library: CspLibrary) -> tuple[list[CspBrushEntry], list[str]]:
    entries, notes = [], []
    catalog = library.common / 'MaterialDB/CatalogMaterial.cmdb'
    if catalog.is_file():
        try:
            with _readonly(catalog) as c:
                required = {'MaterialModifier', 'TableSystemTagElementMap', 'SystemTag'}
                if not required <= _tables(c):
                    raise SutImportError('This CSP material catalog version is unsupported.')
                rows = c.execute("""SELECT DISTINCT m.MaterialName, m.MaterialUuid, m.Path
                    FROM MaterialModifier m
                    JOIN TableSystemTagElementMap e ON e.ElementId=m._PW_ID
                    JOIN SystemTag s ON s._PW_ID=e.TagId
                    WHERE s.Title='BrushTool' LIMIT 20001""").fetchall()
            if len(rows) > 20000:
                raise SutImportError('This CSP catalog exceeds the 20,000-brush browsing limit.')
            xml_cache = {}
            skipped = 0
            for row in rows:
                try:
                    folder = _within(library.material, row['Path'], colon=True)
                    if folder not in xml_cache:
                        xml_cache[folder] = ET.fromstring(_read_file(folder / 'catalog.xml', limit=4*1024*1024))
                    xml = xml_cache[folder]
                    item = next((i for i in xml.findall('.//item')
                                 if i.get('uuid') == row['MaterialUuid'] and i.findtext('type') == 'brush'), None)
                    if item is None:
                        raise SutImportError('Brush metadata is unavailable.')
                    files = {f.get('id'): f.findtext('path') for f in xml.findall('./files/file')}

                    def item_file(kind):
                        ref = item.find(f'{kind}/fileref')
                        relative = files.get(ref.get('idref')) if ref is not None else None
                        return _within(folder, relative) if relative else None

                    data = item_file('data')
                    if data is None:
                        raise SutImportError('Brush data is unavailable.')
                    entries.append(CspBrushEntry(str(row['MaterialName'] or item.findtext('name') or 'Brush'),
                        'material', data, library.material, material_uuid=str(row['MaterialUuid']),
                        thumbnail=item_file('thumbnail')))
                except (OSError, ValueError, TypeError, ET.ParseError):
                    skipped += 1
            if skipped:
                notes.append(f'{skipped} material entries have missing or unreadable catalog metadata.')
        except (OSError, sqlite3.Error, ValueError) as error:
            notes.append(f'Downloaded materials could not be read: {error}')

    for path in sorted(library.common.parent.glob('CLIPStudioPaint*/Tool/EditImageTool.todb')):
        try:
            with _readonly(path) as c:
                if not {'Node', 'Variant'} <= _tables(c):
                    raise SutImportError('This installed tool database version is unsupported.')
                rows = c.execute("""SELECT n._PW_ID, n.NodeName FROM Node n
                    JOIN Variant v ON v.VariantID=n.NodeVariantID
                    WHERE n.NodeOutputOp IN (10,11) AND v.BrushSize IS NOT NULL
                    LIMIT 20001""").fetchall()
                if len(rows) > 20000:
                    raise SutImportError('This CSP tool library exceeds the browsing limit.')
                entries.extend(CspBrushEntry(str(r['NodeName'] or 'Brush'), 'installed', path,
                               library.material, node_id=r['_PW_ID']) for r in rows)
        except (OSError, sqlite3.Error, ValueError) as error:
            notes.append(f'Installed sub tools could not be read: {error}')
    entries.sort(key=lambda entry: (entry.name.casefold(), entry.kind, str(entry.path)))
    return entries, notes


def _read_file(path: Path, *, limit=MAX_FILE_BYTES) -> bytes:
    if path.stat().st_size > limit:
        raise SutImportError('This CSP library file exceeds the import size limit.')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise SutImportError('This CSP library file exceeds the import size limit.')
    return data


def _settings_rows(c, node_id=None):
    if not {'Node', 'Variant'} <= _tables(c):
        raise SutImportError('The CSP library file does not contain brush settings.')
    if node_id is None:
        nodes = [dict(r) for r in c.execute('SELECT * FROM Node LIMIT 1025')]
        if len(nodes) != 1:
            raise SutImportError('This material contains a tool group; choose an individual brush.')
        node = nodes[0]
    else:
        node = dict(c.execute('SELECT * FROM Node WHERE _PW_ID=?', (node_id,)).fetchone() or {})
    if not node:
        raise SutImportError('This brush is no longer in the CSP library. Refresh the list.')
    manager = dict(c.execute('SELECT * FROM Manager LIMIT 1').fetchone() or {}) if 'Manager' in _tables(c) else {}
    active = dict(c.execute('SELECT * FROM Variant WHERE VariantID=?', (node.get('NodeVariantID'),)).fetchone() or {})
    if not active:
        raise SutImportError("The brush's active settings are missing.")
    common = dict(c.execute('SELECT * FROM Variant WHERE VariantID=?', (manager.get('CommonVariantID'),)).fetchone() or {})
    variant = {**common, **{k: v for k, v in active.items() if v is not None}}
    if variant.get('BrushSize') is None:
        raise SutImportError('This CSP subtool is not a drawing brush.')
    return node, variant, manager


def _read_cell(data, start, page_size, origin=0):
    length, pos = _varint(data, start)
    rowid, pos = _varint(data, pos)
    if not 0 < length <= MAX_FILE_BYTES:
        raise SutImportError('Unsupported CSP tool record size.')
    minimum = ((page_size-12)*32//255)-23
    local = length if length <= page_size-35 else minimum+(length-minimum) % (page_size-4)
    if local > page_size-35:
        local = minimum
    page_end = origin + ((start-origin)//page_size+1)*page_size
    remaining = length-local
    if pos+local+(4 if remaining else 0) > min(len(data), page_end):
        raise SutImportError('Truncated CSP tool record.')
    pieces = [data[pos:pos+local]]
    next_page = _u32(data, pos+local) if remaining else 0
    visited = set()
    while remaining:
        at = (next_page-1)*page_size+origin
        if next_page <= 0 or next_page in visited or at < 0 or at+page_size > len(data):
            raise SutImportError('Invalid CSP tool overflow pages.')
        visited.add(next_page)
        next_page = _u32(data, at)
        take = min(remaining, page_size-4)
        pieces.append(data[at+4:at+4+take])
        remaining -= take
    return rowid, _sqlite_record(b''.join(pieces))


def _fragment_settings(logical, expected_name):
    """Recover the known older 1024-page layout without executing its SQL.

    Its outer overflow stream begins on page 6. Node and Variant are intact;
    the original Manager is in the unavailable prefix. Other layouts fail.
    """
    ps, page, visited, pieces = 1024, 6, set(), []
    while page:
        at = (page-1)*ps
        if page in visited or at < 5120 or at+ps > len(logical):
            raise SutImportError('Unsupported CSP downloaded brush layout.')
        visited.add(page)
        pieces.append(logical[at+4:at+ps])
        page = _u32(logical, at)
    stream = b''.join(pieces)
    direct = []
    for pos in range(len(stream)-ps+1):
        if stream[pos:pos+3] != b'\x0d\x00\x00':
            continue
        count = int.from_bytes(stream[pos+3:pos+5], 'big')
        if not 1 <= count <= 4:
            continue
        for i in range(count):
            cell = int.from_bytes(stream[pos+8+i*2:pos+10+i*2], 'big')
            if not 8+count*2 <= cell < ps:
                continue
            try:
                rowid, values = _read_cell(stream, pos+cell, ps, pos)
                direct.append((pos, rowid, values))
            except (ValueError, IndexError):
                continue
    schema = next((v for _, _, v in direct if len(v) == 5 and v[:2] == ['table', 'Node']), None)
    candidates = [(pos, v) for pos, _, v in direct if len(v) >= 17
                  and isinstance(v[1], bytes) and len(v[1]) == 16 and isinstance(v[2], str)]
    if not candidates and schema is not None:
        # A custom icon can move the Node record onto overflow pages. Its
        # readable title anchors the page; validate the entire row below.
        title = expected_name.encode('utf-8')
        candidates = [(pos, None) for pos in range(len(stream)-ps+1)
                      if stream[pos:pos+3] == b'\x0d\x00\x00'
                      and int.from_bytes(stream[pos+3:pos+5], 'big') == 1
                      and title in stream[pos:pos+ps]]
    if schema is None or len(candidates) != 1:
        raise SutImportError('This downloaded brush layout is unsupported. Export it from CSP as a .sut to import it.')
    origin = candidates[0][0]-(schema[3]-1)*ps
    schemas = {}
    # The long Variant CREATE record itself uses overflow pages. Now that
    # the page origin is known, recover those pages with their actual offsets.
    for pos in range(origin % ps, len(stream)-ps+1, ps):
        if stream[pos] != 13:
            continue
        count = int.from_bytes(stream[pos+3:pos+5], 'big')
        if count > (ps-8)//2:
            continue
        for i in range(count):
            cell = int.from_bytes(stream[pos+8+i*2:pos+10+i*2], 'big')
            if not 8+count*2 <= cell < ps:
                continue
            try:
                _, values = _read_cell(stream, pos+cell, ps, origin)
            except (ValueError, IndexError):
                continue
            if len(values) == 5 and values[0] == 'table' and values[1] in ('Node', 'Variant', 'Manager'):
                schemas[values[1]] = values

    def table_rows(name):
        if name not in schemas:
            raise SutImportError('Incomplete CSP brush settings schema.')
        sql = schemas[name][4]
        # Supported schemas consist of simple typed columns. Interpret names;
        # never run CREATE statements copied from source files.
        body = sql[sql.find('(')+1:sql.rfind(')')]
        columns = []
        for column in body.split(','):
            match = re.fullmatch(r'\s*([A-Za-z_][A-Za-z_0-9]*)\s+(?:INTEGER|TEXT|BLOB|REAL|NULL)'
                                 r'(?:\s+PRIMARY KEY AUTOINCREMENT|\s+DEFAULT NULL)?\s*', column)
            if match is None:
                raise SutImportError('Unsupported CSP brush column layout.')
            columns.append(match[1])
        pending, seen, rows = [schemas[name][3]], set(), []
        while pending:
            pg = pending.pop()
            at = (pg-1)*ps+origin
            if pg <= 0 or pg in seen or at < 0 or at+ps > len(stream):
                raise SutImportError('Invalid CSP brush settings pages.')
            seen.add(pg)
            block = stream[at:at+ps]
            count = int.from_bytes(block[3:5], 'big')
            header = 8 if block[0] == 13 else 12
            if block[0] not in (5, 13) or count > (ps-header)//2:
                raise SutImportError('Unsupported CSP brush settings page.')
            if block[0] == 5:
                pending.append(_u32(block, 8))
            for i in range(count):
                cell = int.from_bytes(block[header+i*2:header+2+i*2], 'big')
                if not header+count*2 <= cell < ps:
                    raise SutImportError('Invalid CSP brush settings cell.')
                if block[0] == 5:
                    pending.append(_u32(block, cell))
                else:
                    rowid, values = _read_cell(stream, at+cell, ps, origin)
                    if len(values) != len(columns):
                        raise SutImportError('Incomplete CSP brush settings record.')
                    row = dict(zip(columns, values))
                    row['_PW_ID'] = rowid
                    rows.append(row)
        return rows

    nodes = table_rows('Node')
    if len(nodes) != 1 or not isinstance(nodes[0].get('NodeName'), str):
        raise SutImportError('Ambiguous CSP brush identity.')
    node = nodes[0]
    variant = next((v for v in table_rows('Variant') if v.get('VariantID') == node.get('NodeVariantID')), None)
    if variant is None or variant.get('BrushSize') is None:
        raise SutImportError('The downloaded material has no drawing brush settings.')
    manager = {'Version': node.get('NodeInstalledVersion')}
    return node, variant, manager


def _downloaded_settings(data, expected_name):
    logical_size, spans = _c2f_payload(data)
    logical = bytearray(logical_size)
    for start, contents in spans:
        logical[start:start+len(contents)] = contents
    # The complete inner database is a typed blob in the newer outer layouts.
    header = logical.find(_SQLITE, 5120)
    if header >= 0:
        for ps in (4096, 1024):
            for start in range(max(5120, header-160), header):
                try:
                    _, values = _read_cell(logical, start, ps)
                except (ValueError, IndexError):
                    continue
                blobs = [v for v in values if isinstance(v, bytes) and v.startswith(_SQLITE)]
                for blob in blobs:
                    c = sqlite3.connect(':memory:')
                    try:
                        c.deserialize(blob)
                        _configure_reader(c)
                        if {'Node', 'Variant'} <= _tables(c):
                            return (*_settings_rows(c), [])
                    except sqlite3.Error:
                        continue
                    finally:
                        c.close()
    node, variant, manager = _fragment_settings(logical, expected_name)
    return node, variant, manager, [
        'The downloaded material retains its brush settings, but its original global pressure calibration '
        'is unavailable in this CSP format; an identity input curve is used.'
    ]


def _required_materials(variant, root, embedded):
    keys = ['TextureImage']
    if variant.get('BrushUsePatternImage'):
        keys.append('BrushPatternImageArray')
    if variant.get('UseDualBrush'):
        keys.append('DualTextureImage')
        if variant.get('DualUsePatternImage'):
            keys.append('DualPatternImageArray')
    materials, total = {}, 0
    for key in keys:
        references = _references(variant.get(key))
        if key.endswith('PatternImageArray') and not references:
            which = 'secondary' if key.startswith('Dual') else 'primary'
            raise SutImportError(f"The brush's {which} image-tip list is empty. "
                                 'Check its tip settings in CSP or export it as a .sut.')
        for ref in references:
            original = ref['path']
            if original in materials:
                continue
            blob = embedded.get(original)
            if blob is None:
                # Catalog path selects the correct installed/downloaded material;
                # the data filename is separate from the material display name.
                relative = ref['catalog'] or ':'.join(original.split(':')[:-2])
                folder = _within(root, relative, colon=True)
                path = _within(folder, 'data/' + original.rsplit(':', 1)[-1])
                try:
                    blob = _read_file(path)
                except FileNotFoundError as error:
                    raise SutImportError(f"The brush needs a missing material: {ref['name']}. "
                                         'Download it in CSP before importing this brush.') from error
                if path.suffix.lower() == '.png':
                    out = io.BytesIO()
                    with tarfile.open(fileobj=out, mode='w') as archive:
                        member = tarfile.TarInfo('data/' + path.name)
                        member.size = len(blob)
                        archive.addfile(member, io.BytesIO(blob))
                    blob = out.getvalue()
            total += len(blob)
            if total > MAX_FILE_BYTES or len(materials) >= 1024:
                raise SutImportError('Combined CSP brush materials exceed the import limit.')
            materials[original] = blob
    return [{'OriginalPath': key, 'FileData': value} for key, value in materials.items()]


def import_library_brush(entry: CspBrushEntry, *, dpi=DEFAULT_IMPORT_DPI) -> BrushDefinition:
    dpi = validate_import_dpi(dpi)
    embedded, notes = {}, []
    try:
        if entry.kind == 'installed':
            with _readonly(entry.path) as c:
                node, variant, manager = _settings_rows(c, entry.node_id)
        elif entry.kind == 'material':
            data = _read_file(entry.path)
            if data.startswith(_SQLITE):
                with _readonly(entry.path) as c:
                    node, variant, manager = _settings_rows(c)
                    if 'MaterialFile' in _tables(c):
                        embedded = {r['OriginalPath']: r['FileData'] for r in
                                    c.execute('SELECT OriginalPath, FileData FROM MaterialFile LIMIT 1025')}
            else:
                node, variant, manager, notes = _downloaded_settings(data, entry.name)
                embedded = {v[2]: v[5] for _, _, v in _c2f_rows(data)
                            if len(v) == 8 and isinstance(v[2], str) and isinstance(v[5], bytes)}
        else:
            raise SutImportError('Unknown CSP brush library source.')
        # Palette selection, sibling links and global save counters change
        # while drawing with other brushes. They are not part of this preset.
        manager = {k: v for k, v in manager.items() if k not in {
            '_PW_ID', 'RootUuid', 'CurrentNodeUuid', 'MaxVariantID',
            'CommonVariantID', 'ObjectNodeUuid', 'SavedCount',
        }}
        node = {k: v for k, v in node.items() if k not in {
            'NodeNextUuid', 'NodeFirstChildUuid', 'NodeSelectedUuid',
        }}
        materials = _required_materials(variant, entry.material_root, embedded)
        metadata = json.dumps([node, variant, manager], sort_keys=True,
                              default=_json_value, ensure_ascii=True).encode('utf-8')
        digest = hashlib.sha256(metadata)
        for row in materials:
            digest.update(row['OriginalPath'].encode('utf-8'))
            digest.update(row['FileData'])
        checksum = digest.hexdigest()
        definition = _build_definition(entry.path.name, checksum, node, variant, manager, materials, dpi=dpi)
    except sqlite3.Error as error:
        raise SutImportError('The CSP library database is busy, damaged, or unsupported. '
                             'Try closing CSP and refreshing the library.') from error
    identity = f'{entry.path.resolve()}:{entry.node_id}:{entry.material_uuid}'
    identifier = 'csp-library-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]

    def provenance(brush, secondary=False):
        source = dict(brush.source, format='csp-library', library={
            'kind': entry.kind, 'path': str(entry.path), 'node_id': entry.node_id,
            'material_uuid': entry.material_uuid,
        })
        return replace(brush, id=identifier + ('-secondary' if secondary else ''), source=source,
                       warnings=tuple(dict.fromkeys([*notes, *brush.warnings])),
                       dual=provenance(brush.dual, True) if brush.dual is not None else None)

    return replace(provenance(definition), name=entry.name)
