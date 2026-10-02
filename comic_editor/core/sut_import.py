"""Read CSP subtools without modifying the source or importing executable data.

The C2F material reader recovers intact SQLite pages after its opaque prefix and
follows SQLite overflow chains. It decodes actual Offscreen tiles, never the
material thumbnail or CanvasPreview. See docs/csp-sut-investigation.md.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
from pathlib import Path
import sqlite3
import struct
import tarfile
import zlib

from PIL import Image

from .brushes import BrushDefinition, BrushDynamics, BrushTexture, BrushTip, clamp, finite
from .brush_units import BrushLengthConversion, DEFAULT_IMPORT_DPI, validate_import_dpi


MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_IMAGE_PIXELS = 32 * 1024 * 1024
MAX_TOTAL_IMAGE_PIXELS = 256 * 1024 * 1024
MAX_SERIALIZED_IMAGE_BYTES = 128 * 1024 * 1024
_C2F = b"\x89C2F\r\n\x1a\n"
_SQLITE = b"SQLite format 3\x00"
_BEGIN = "BlockDataBeginChunk".encode("utf-16-be")


class SutImportError(ValueError):
    """A corrupt, unsupported or oversized subtool could not be imported."""


# Ink uses CSP's general compositing codes, not the separate 0..12 dual-brush
# menu. Cross-checked against named native layers in clipdecode's blend-modes
# fixture, default watercolor presets (2), and installed erasers (27).
_INK_BLEND_MODES = {
    0:'normal', 1:'darken', 2:'multiply', 3:'color_burn', 4:'linear_burn',
    5:'subtract', 6:'darker_color', 7:'lighten', 8:'screen', 9:'color_dodge',
    10:'glow_dodge', 11:'add', 12:'add_glow', 13:'lighter_color', 14:'overlay',
    15:'soft_light', 16:'hard_light', 17:'vivid_light', 18:'linear_light',
    19:'pin_light', 20:'hard_mix', 21:'difference', 22:'exclusion',
    23:'hue', 24:'saturation', 25:'color', 26:'luminosity', 27:'erase', 36:'divide',
}
# The official dual-brush guide identifies Wet wash as Height (Linear) and
# Glitter as Add (Glow); their installed settings store 12 and 2 respectively.
_DUAL_BLEND_MODES = {
    0:'normal', 1:'multiply', 2:'add_glow', 3:'subtract', 4:'darken', 5:'lighten',
    6:'screen', 7:'overlay', 8:'color_dodge', 9:'color_burn', 10:'linear_burn',
    11:'hard_mix', 12:'height_linear',
}
# The installed Texture menu resource has ten consecutive labels, matching
# the five legacy modes and the five appended in CSP 1.10.5. These are not
# Ink or Dual brush compositing codes. See docs/csp-texture-mode-evidence.md.
_TEXTURE_MODES = {
    0:'normal', 1:'multiply', 2:'subtract', 3:'compare', 4:'outline',
    5:'overlay', 6:'color_dodge', 7:'color_burn', 8:'hard_mix', 9:'height',
}


def _u32(data: bytes, offset: int, endian: str = "big") -> int:
    if offset < 0 or offset + 4 > len(data):
        raise SutImportError("Truncated brush data.")
    return int.from_bytes(data[offset:offset + 4], endian)


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(9):
        if offset >= len(data):
            raise SutImportError("Truncated SQLite record.")
        byte = data[offset]
        offset += 1
        if index == 8:
            return (value << 8) | byte, offset
        value = (value << 7) | (byte & 127)
        if byte < 128:
            return value, offset
    raise SutImportError("Invalid SQLite integer.")


def _sqlite_record(payload: bytes) -> list:
    header_size, cursor = _varint(payload, 0)
    if not 1 <= header_size <= len(payload):
        raise SutImportError("Invalid SQLite record header.")
    serials = []
    while cursor < header_size:
        serial, cursor = _varint(payload, cursor)
        serials.append(serial)
        if len(serials) > 4096:
            raise SutImportError("Too many SQLite columns.")
    if cursor != header_size:
        raise SutImportError("Invalid SQLite column header.")
    values = []
    cursor = header_size
    for serial in serials:
        size = 0
        if serial == 0:
            value = None
        elif serial in (8, 9):
            value = serial - 8
        elif 1 <= serial <= 6:
            size = (0, 1, 2, 3, 4, 6, 8)[serial]
            value = int.from_bytes(payload[cursor:cursor + size], "big", signed=True)
        elif serial == 7:
            size = 8
            if cursor + size > len(payload):
                raise SutImportError("Truncated SQLite float.")
            value = struct.unpack_from(">d", payload, cursor)[0]
        elif serial >= 12:
            size = (serial - 12) // 2
            value = payload[cursor:cursor + size]
            if serial & 1:
                value = value.decode("utf-8", errors="replace")
        else:
            raise SutImportError("Unsupported SQLite value.")
        cursor += size
        if cursor > len(payload):
            raise SutImportError("Truncated SQLite value.")
        values.append(value)
    if cursor != len(payload):
        raise SutImportError("Invalid SQLite record length.")
    return values


def _c2f_payload(data: bytes) -> tuple[int, list[tuple[int, bytes]]]:
    """Validate a material container and return its readable logical spans."""
    if not data.startswith(_C2F) or len(data) > MAX_FILE_BYTES:
        raise SutImportError("Unsupported CSP material container.")
    cursor = 8
    logical_size = 0
    spans = []
    while cursor + 12 <= len(data):
        length = _u32(data, cursor, "little")
        end = cursor + 12 + length
        if end > len(data):
            raise SutImportError("Truncated CSP material chunk.")
        kind = data[cursor + 4:cursor + 8]
        payload = data[cursor + 8:cursor + 8 + length]
        crc = _u32(data, cursor + 8 + length, "little")
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise SutImportError("CSP material checksum failed.")
        if kind == b"dATA":
            if len(payload) < 2:
                raise SutImportError("Truncated CSP material payload.")
            encoding = int.from_bytes(payload[:2], "little")
            if encoding == 0:
                spans.append((logical_size, payload[2:]))
                logical_size += len(payload) - 2
            elif encoding == 1 and logical_size == 0 and len(payload) == 5130:
                logical_size = 5120
            else:
                raise SutImportError("This CSP material uses an unsupported C2F encoding.")
        cursor = end
    if cursor != len(data) or not spans or logical_size % 512:
        raise SutImportError("Unsupported CSP material page layout.")
    return logical_size, spans


def _c2f_rows(data: bytes) -> list[tuple[int, int, list]]:
    """Recover intact table-leaf records from the supported C2F layout.

    This does not decrypt the opaque prefix. Its exact supported size is checked
    so unrecognized layouts fail explicitly instead of inventing page offsets.
    """
    logical_size, spans = _c2f_payload(data)

    def page(number: int, page_size: int) -> bytes:
        position = (number - 1) * page_size
        if number <= 0:
            raise SutImportError("Invalid material overflow page.")
        for start, contents in spans:
            if start <= position and position + page_size <= start + len(contents):
                return contents[position-start:position-start+page_size]
        raise SutImportError("The material record crosses an unavailable page.")

    candidates = []
    # The 2018 pack uses 1024-byte pages; installed CSP 1.12 materials use
    # 4096-byte pages. The opaque prefix hides the SQLite header's page size.
    # Select by valid raster records and schema records, not by PNG signatures.
    for page_size in (1024,4096,512,2048,8192,16384,32768,65536):
        if logical_size % page_size:
            continue
        rows = []
        maximum_local = page_size - 35
        minimum_local = ((page_size-12)*32//255)-23
        for number in range(1, logical_size // page_size + 1):
            try:
                contents = page(number,page_size)
            except SutImportError:
                continue
            if contents[0] != 13:
                continue
            count = int.from_bytes(contents[3:5], "big")
            if count > (page_size-8)//2:
                continue
            for index in range(count):
                try:
                    cell = int.from_bytes(contents[8 + index*2:10 + index*2], "big")
                    if not 8 + count*2 <= cell < page_size:
                        continue
                    length, local_pos = _varint(contents, cell)
                    rowid, local_pos = _varint(contents, local_pos)
                    if length > MAX_FILE_BYTES:
                        continue
                    local_size = length
                    if length > maximum_local:
                        local_size = minimum_local + (length-minimum_local) % (page_size-4)
                        if local_size > maximum_local:
                            local_size = minimum_local
                    if local_pos + local_size > page_size:
                        continue
                    pieces = [contents[local_pos:local_pos + local_size]]
                    remaining = length - local_size
                    next_page = _u32(contents, local_pos + local_size) if remaining else 0
                    visited = {number}
                    while remaining:
                        if next_page in visited:
                            raise SutImportError("Cyclic material overflow pages.")
                        visited.add(next_page)
                        overflow = page(next_page,page_size)
                        next_page = _u32(overflow, 0)
                        take = min(remaining,page_size-4)
                        pieces.append(overflow[4:4 + take])
                        remaining -= take
                    values = _sqlite_record(b"".join(pieces))
                    rows.append((number, rowid, values))
                except (SutImportError, UnicodeError, struct.error):
                    continue
        raster_count = sum(len(v)==6 and isinstance(v[5],bytes) and _BEGIN in v[5][:64] for _,_,v in rows)
        schema_count = sum(len(v)==5 and v[0]=='table' for _,_,v in rows)
        candidates.append(((raster_count,schema_count,len(rows)), rows))
        # Genuine typed raster records settle the page size. Avoid retaining
        # copies of every interpreted page layout for large material libraries.
        if raster_count:
            return rows
    return max(candidates,key=lambda item:item[0])[1] if candidates else []


def _check_image_size(width: int, height: int, remaining_pixels: int | None = None) -> None:
    if not width or not height or width * height > MAX_IMAGE_PIXELS:
        raise SutImportError("CSP material dimensions exceed the import limit.")
    if remaining_pixels is not None and width * height > remaining_pixels:
        raise SutImportError("Combined brush materials exceed the decoded-pixel import limit.")


def _offscreen_image(attribute: bytes, block_data: bytes, *, remaining_pixels: int | None = None) -> Image.Image:
    marker = "Parameter".encode("utf-16-be")
    position = attribute.find(marker)
    if position < 0:
        raise SutImportError("Unknown CSP raster attribute layout.")
    position += len(marker)
    values = [_u32(attribute, position + 4*i) for i in range(20)]
    width, height, columns, rows, order, depth, channels, pixel_bytes = values[:8]
    _check_image_size(width,height,remaining_pixels)
    if columns != math.ceil(width/256) or rows != math.ceil(height/256):
        raise SutImportError("Unsupported CSP raster tile dimensions.")
    if depth != 1 or channels not in (0, 1, 3, 4) or pixel_bytes not in (1, 2, 4, 5):
        raise SutImportError(f"Unsupported CSP raster channels ({order}, {depth}, {channels}, {pixel_bytes}).")
    packed_mono = (order == 17 and channels == 1 and pixel_bytes == 1
                   and values[8] == 8192 and values[10] == 32 and values[18] == 1)
    # Import NumPy only for the decoded raster operation, not for format parsing.
    import numpy as np
    output = np.zeros((height, width, 4), dtype=np.uint8)
    cursor = 0
    for tile_index in range(columns * rows):
        size = _u32(block_data, cursor)
        end = cursor + size
        if size < 104 or end > len(block_data) or block_data[cursor+8:cursor+46] != _BEGIN:
            raise SutImportError("Invalid CSP raster tile record.")
        stored_index = _u32(block_data, cursor+46)
        if stored_index != tile_index:
            raise SutImportError("Unexpected CSP raster tile order.")
        present = _u32(block_data, cursor+62)
        if present:
            compressed_size = _u32(block_data, cursor+70, "little")
            if _u32(block_data, cursor+66) != compressed_size + 4 or cursor + 74 + compressed_size > end:
                raise SutImportError("Invalid CSP raster compressed length.")
            decoder = zlib.decompressobj()
            expected = 16384 if packed_mono else 65536 * pixel_bytes
            raster = decoder.decompress(block_data[cursor+74:cursor+74+compressed_size], expected + 1)
            if len(raster) != expected or not decoder.eof or decoder.unused_data:
                raise SutImportError("Invalid CSP raster tile pixels.")
            if packed_mono:
                # Monochrome has separate black and white coverage planes,
                # not byte alpha plus luminance. Bits run MSB first per row.
                black = np.unpackbits(np.frombuffer(raster[:8192],np.uint8),bitorder='big').reshape(256,256)
                white = np.unpackbits(np.frombuffer(raster[8192:],np.uint8),bitorder='big').reshape(256,256)
                if np.any(black & white):
                    raise SutImportError("Overlapping CSP monochrome coverage planes.")
                alpha = (black | white)*255
                colors = np.repeat((white*255)[...,None],3,axis=2)
            else:
                alpha = np.frombuffer(raster[:65536], dtype=np.uint8).reshape(256,256)
                # Byte-depth rasters store alpha then interleaved image channels.
                colors = np.frombuffer(raster[65536:], dtype=np.uint8)
                if pixel_bytes == 1:
                    colors = np.zeros((256,256,3), dtype=np.uint8)
                elif pixel_bytes == 2:
                    colors = np.repeat(colors.reshape(256,256,1), 3, axis=2)
                elif pixel_bytes == 5:
                    colors = colors.reshape(256,256,4)[:, :, (2,1,0)]
                else:
                    colors = colors.reshape(256,256,3)[:, :, ::-1]
            x = (tile_index % columns)*256
            y = (tile_index // columns)*256
            w = min(256, width-x)
            h = min(256, height-y)
            output[y:y+h,x:x+w,:3] = colors[:h,:w]
            output[y:y+h,x:x+w,3] = alpha[:h,:w]
        cursor = end
    return Image.fromarray(output)


def _material_raster(data: bytes, *, remaining_pixels: int | None = None) -> tuple[Image.Image, dict]:
    """Decode the original registered image and its layer expression metadata."""
    rows = _c2f_rows(data)
    offscreens = {}
    for _, _, values in rows:
        if (len(values) == 6 and isinstance(values[4], bytes) and isinstance(values[5], bytes)
                and _BEGIN in values[5][:64]):
            offscreens[values[1]] = (values[4], values[5])
    if not offscreens:
        raise SutImportError("No decodable original raster exists in this material.")
    # Bind recovered records to table schemas, then follow the registered source
    # Layer -> ResizableOriginalMipmap -> MipmapInfo -> Offscreen relationship.
    schemas = {}
    for _, _, values in rows:
        if (len(values) == 5 and values[0] == 'table' and isinstance(values[4], str)
                and values[4].startswith('CREATE TABLE ')):
            sql = values[4]
            columns = [column.strip().split()[0] for column in sql[sql.find('(')+1:sql.rfind(')')].split(',')]
            schemas[values[1]] = (values[3], columns)
    tables = {}
    for name in ('Layer', 'Mipmap', 'MipmapInfo'):
        if name not in schemas:
            continue
        root, columns = schemas[name]
        tables[name] = [dict(zip(columns, values)) for page, _, values in rows
                        if page == root and len(values) == len(columns)]
    metadata = {}
    selected = None
    for layer in tables.get('Layer', []):
        if layer.get('LayerType') != 0:
            continue
        metadata = {key:layer.get(key) for key in ('LayerColorTypeIndex', 'LayerColorTypeBlackChecked',
                    'LayerColorTypeWhiteChecked', 'LayerName')}
        mipmap_id = layer.get('ResizableOriginalMipmap') or layer.get('LayerRenderMipmap')
        mipmap = next((m for m in tables.get('Mipmap', []) if m.get('MainId') == mipmap_id), None)
        info = next((m for m in tables.get('MipmapInfo', []) if mipmap and m.get('MainId') == mipmap.get('BaseMipmapInfo')), None)
        if info and info.get('Offscreen') in offscreens:
            selected = info['Offscreen']
            break
    if selected is None:
        # Some schemas fall in the unavailable prefix. Only real raster blocks
        # are candidates; choose the largest populated source, never a preview.
        selected = max(offscreens, key=lambda key:len(offscreens[key][1]))
        metadata['selection'] = 'largest_original_raster'
    image = _offscreen_image(*offscreens[selected],remaining_pixels=remaining_pixels)
    metadata.update({'offscreen_id': selected, 'width':image.width, 'height':image.height})
    return image, metadata


def _material_file(blob: bytes, original_path: str, *, remaining_pixels: int | None = None) -> tuple[Image.Image, dict]:
    if len(blob) > MAX_FILE_BYTES:
        raise SutImportError("Embedded brush material exceeds the import limit.")
    if blob.startswith(_C2F):
        return _material_raster(blob,remaining_pixels=remaining_pixels)
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode='r:') as archive:
            expected = original_path.split(':data:', 1)[-1].replace(':', '/')
            candidates = [member for member in archive.getmembers()
                          if member.isfile() and member.name.startswith('data/')
                          and member.name.lower().endswith(('.layer', '.png'))]
            if not candidates:
                raise SutImportError("Brush material has no original image payload.")
            member = next((m for m in candidates if m.name == 'data/' + expected), candidates[0])
            if member.size > MAX_FILE_BYTES:
                raise SutImportError("Brush material payload exceeds the import limit.")
            stream = archive.extractfile(member)
            if stream is None:
                raise SutImportError("Unreadable brush material payload.")
            # Buffered readers may reserve the requested capacity before
            # noticing EOF. Read this already-bounded member, not the
            # entire 128 MiB import allowance for every tiny material.
            payload = stream.read(member.size+1)
            if payload.startswith(_C2F):
                return _material_raster(payload,remaining_pixels=remaining_pixels)
            if member.name.lower().endswith('.png'):
                with Image.open(io.BytesIO(payload)) as image:
                    _check_image_size(image.width,image.height,remaining_pixels)
                    return image.convert('RGBA'), {'selection':'original_png'}
    except (tarfile.TarError, OSError) as exc:
        raise SutImportError("Unsupported or damaged brush material archive.") from exc
    raise SutImportError("Unsupported original brush image format.")


def _references(data: bytes | None) -> list[dict]:
    if not data:
        return []
    if not isinstance(data, bytes) or len(data) < 8 or _u32(data, 0) != 8:
        raise SutImportError("Unsupported brush material reference list.")
    count = _u32(data, 4)
    if count > 1024:
        raise SutImportError("Too many brush materials.")
    cursor = 8
    result = []
    for _ in range(count):
        end = cursor + _u32(data, cursor)
        if end > len(data) or end <= cursor + 4:
            raise SutImportError("Truncated brush material reference.")
        cursor += 4

        def string() -> str:
            nonlocal cursor
            size = _u32(data, cursor)
            cursor += 4
            if size & 1 or cursor + size > end:
                raise SutImportError("Invalid brush material name.")
            value = data[cursor:cursor + size].decode('utf-16-le')
            cursor += size
            return value

        path = string()
        flags = _u32(data, cursor)
        cursor += 4
        name = string()
        catalog_version = _u32(data, cursor)
        cursor += 4
        catalog = string() if catalog_version else ''
        result.append({'path':path, 'name':name, 'flags':flags, 'catalog':catalog})
        cursor = end
    return result


def _curve(data: bytes) -> tuple[tuple[float, float], ...]:
    if len(data) < 12 or _u32(data, 0) != 12 or _u32(data, 8) != 16:
        raise SutImportError("Unsupported brush response curve.")
    count = _u32(data, 4)
    if count > 256 or len(data) != 12 + count*16:
        raise SutImportError("Invalid brush response curve length.")
    points = tuple(struct.unpack_from('>dd', data, 12 + 16*i) for i in range(count))
    if not points or any(not math.isfinite(v) for point in points for v in point):
        raise SutImportError("Invalid brush response curve coordinates.")
    return tuple(dict.fromkeys((clamp(x), clamp(y)) for x,y in points))


def _dynamics(data: bytes | None, *, signed: bool = False, tilt_enlargement: bool = True,
              diagnostics: list[str] | None = None) -> BrushDynamics:
    if not data:
        return BrushDynamics()
    if not isinstance(data, bytes) or len(data) < 44:
        raise SutImportError("Unsupported brush dynamics encoding.")
    header_size = _u32(data, 0)
    # Newer 48-byte headers retain the original eleven words and append the
    # velocity-curve byte length. The three graphs follow in that order.
    # Do not infer unknown header layouts or curve offsets from signatures.
    if header_size not in (44, 48):
        raise SutImportError("Unsupported brush dynamics header size.")
    if len(data) < header_size:
        raise SutImportError("Truncated brush dynamics header.")
    header = struct.unpack_from('>11I', data)
    _, capabilities, active, pressure_min, tilt_min, velocity_min, random_min, _, pressure_size, tilt_size, tilt_max = header
    if signed:
        # Color changes use signed percentage minima, especially Random=-100.
        # Sizes and other positive-only parameters retain their prior bounds.
        pressure_min, tilt_min, velocity_min, random_min = struct.unpack_from('>4i', data, 12)
    lower = -1. if signed else 0.
    curve_end = header_size + pressure_size + tilt_size
    if curve_end > len(data):
        raise SutImportError("Truncated brush dynamics curve.")
    extension = _u32(data, 44) if header_size == 48 else 0
    velocity_size = extension if 28 <= extension <= 4108 and (extension-12) % 16 == 0 else 0
    if curve_end + velocity_size > len(data):
        raise SutImportError("Truncated brush velocity curve.")
    if curve_end + velocity_size != len(data):
        raise SutImportError("Unsupported trailing brush dynamics data.")
    pressure_curve = _curve(data[header_size:header_size+pressure_size]) if pressure_size else ((0.,0.),(1.,1.))
    tilt_curve = _curve(data[header_size+pressure_size:curve_end]) if tilt_size else ((0.,0.),(1.,1.))
    velocity_curve = _curve(data[curve_end:curve_end+velocity_size]) if velocity_size else ((0.,1.),(1.,0.))
    if diagnostics is not None:
        if extension and not velocity_size:
            diagnostics.append('The nonzero 48-byte dynamics extension is retained but not applied; known response channels were imported.')
        if active & ~0xf0:
            diagnostics.append('Unknown active dynamics inputs are retained but not applied; known response channels were imported.')
        if tilt_enlargement and not signed and tilt_max > 1000:
            diagnostics.append('Tilt maximum above 1000% is limited to 1000%; the original value is retained.')
    return BrushDynamics(pressure=bool(active & 0x10), minimum=clamp(pressure_min/100,lower,1),
                         pressure_curve=pressure_curve, tilt=bool(active & 0x20),
                         tilt_minimum=clamp(tilt_min/100,lower,1),
                         tilt_maximum=clamp(tilt_max/100,0,10) if tilt_enlargement and not signed else 1.,
                         tilt_curve=tilt_curve,
                         velocity=bool(active & 0x40), velocity_minimum=clamp(velocity_min/100,lower,1),
                         velocity_curve=velocity_curve,
                         random=clamp(random_min/100,lower,1) if active & 0x80 else 1.)


def _json_value(value):
    if isinstance(value, bytes):
        return {'encoding':'base64', 'data':base64.b64encode(value).decode('ascii')}
    return value


def _image_png(image: Image.Image) -> str:
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return base64.b64encode(stream.getvalue()).decode('ascii')


def import_sut(path: str | Path, *, dpi: float = DEFAULT_IMPORT_DPI) -> BrushDefinition:
    """Import one raster brush, retaining the source settings and limitations.

    An unresolved active tip is an error rather than a substituted round brush.
    Materials not used by the current variant need not be decoded.
    Explicit millimeter measurements use ``dpi`` (300 by default); each source
    measurement and its converted pixels are recorded in source.length_units.
    """
    dpi = validate_import_dpi(dpi)
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise SutImportError("This brush file exceeds the 128 MiB import limit.")
    data = path.read_bytes()
    if not data.startswith(_SQLITE):
        raise SutImportError("This file is not a supported SQLite CSP subtool (.sut).")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.set_progress_handler(lambda: 1, 500000)
        # Validate actual tables before querying; a view must never be treated as
        # a format table. Fixed SQL queries do not execute any source SQL text.
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'Node','Variant'}.issubset(tables):
            raise SutImportError("The file does not contain CSP brush settings.")
        nodes = [dict(row) for row in connection.execute('SELECT * FROM Node LIMIT 1025')]
        if len(nodes) != 1:
            raise SutImportError("Import individual .sut brushes; this file contains a tool group.")
        node = nodes[0]
        variants = [dict(row) for row in connection.execute('SELECT * FROM Variant LIMIT 1025')]
        variant = next((row for row in variants if row.get('VariantID') == node.get('NodeVariantID')), None)
        if variant is None:
            raise SutImportError("The brush's active settings variant is missing.")
        # Other subtool types (selection, fill, text) are also exported as SUT.
        if variant.get('BrushSize') is None:
            raise SutImportError("This CSP subtool is not a raster drawing brush.")
        manager = dict(connection.execute('SELECT * FROM Manager LIMIT 1').fetchone() or {}) if 'Manager' in tables else {}
        material_rows = [dict(row) for row in connection.execute('SELECT * FROM MaterialFile LIMIT 1025')] if 'MaterialFile' in tables else []
        reconstruction = dict(connection.execute('SELECT Key,Value FROM ReconstructionInfo LIMIT 32')) if 'ReconstructionInfo' in tables else {}
    except sqlite3.Error as exc:
        raise SutImportError("The CSP brush database is damaged or unsupported.") from exc
    finally:
        if 'connection' in locals():
            connection.close()
    if len(material_rows) > 1024:
        raise SutImportError("Too many embedded brush materials.")
    return _build_definition(path.name,hashlib.sha256(data).hexdigest(),node,variant,manager,material_rows,reconstruction,dpi=dpi)


class _MaterialCache:
    """Keep one decoded material at a time, with bounded shared PNG results.

    Large multi-tip brushes can contain hundreds of millions of pixels. Their
    originals need not all remain decoded while their portable PNGs are built.
    The total work budget is separate from the one-image resident pixel bound.
    """

    def __init__(self):
        self.decoded = {}
        self.records = {}
        self.encoded = {}
        self.pixels = 0
        self.serialized_image_bytes = 0

    def get(self, path: str, blob: bytes) -> tuple[Image.Image, dict]:
        if path not in self.decoded:
            for previous,_ in self.decoded.values():
                previous.close()
            self.decoded.clear()
            if path in self.records and (path,False) in self.encoded:
                # Reuse the already validated, lossless PNG for a repeated tip.
                with Image.open(io.BytesIO(base64.b64decode(self.encoded[path,False]))) as stored:
                    image = stored.convert('RGBA')
                metadata = self.records[path]
            else:
                remaining = (MAX_IMAGE_PIXELS if path in self.records else
                             MAX_TOTAL_IMAGE_PIXELS-self.pixels)
                image,metadata = _material_file(blob,path,remaining_pixels=remaining)
            if path not in self.records:
                self.pixels += image.width*image.height
                self.records[path] = metadata
            self.decoded[path] = image,metadata
        return self.decoded[path]

    def png(self, path: str, *, texture: bool = False) -> str:
        key = (path,texture)
        if key not in self.encoded:
            image = self.decoded[path][0]
            if texture:
                # Alpha-only textures are ink coverage; flatten over paper to
                # retain the source density polarity before luminance encoding.
                with Image.new('RGBA',image.size,'white') as paper:
                    paper.alpha_composite(image)
                    with paper.convert('L') as grayscale:
                        self.encoded[key] = _image_png(grayscale)
            else:
                self.encoded[key] = _image_png(image)
        encoded = self.encoded[key]
        # Portable presets serialize each reference by value, even when the
        # in-memory PNG string is shared. Charge every published occurrence.
        if self.serialized_image_bytes + len(encoded) > MAX_SERIALIZED_IMAGE_BYTES:
            raise SutImportError("Brush image references exceed the serialized-image import limit.")
        self.serialized_image_bytes += len(encoded)
        return encoded

    def close(self):
        for image,_ in self.decoded.values():
            image.close()
        self.decoded.clear()
        self.records.clear()
        self.encoded.clear()


def _build_definition(filename: str, checksum: str, node: dict, variant: dict,
                      manager: dict, material_rows: list[dict], reconstruction: dict | None = None,
                      *, dpi: float = DEFAULT_IMPORT_DPI, _depth: int = 0,
                      _material_cache: _MaterialCache | None = None) -> BrushDefinition:
    if _material_cache is None:
        cache = _MaterialCache()
        try:
            return _build_definition(filename,checksum,node,variant,manager,material_rows,reconstruction,
                                     dpi=dpi,_depth=_depth,_material_cache=cache)
        finally:
            cache.close()
    warnings = []
    if reconstruction:
        warnings.append('This is a research fixture reconstructed from installed CSP data, not a native CSP export.')
        if 'pressure calibration is unavailable' in str(reconstruction.get('recovery','')).lower():
            warnings.append('The original device-pressure calibration is unavailable in this reconstructed brush; an identity curve is used.')
    material_map = {row.get('OriginalPath'):row for row in material_rows}
    material_seen = set()
    material_info = []

    def material(reference: dict) -> tuple[Image.Image, dict]:
        key = reference['path']
        row = material_map.get(key)
        if row is None:
            raise SutImportError(f"The brush needs a missing material: {reference['name']}.")
        image,metadata = _material_cache.get(key,row.get('FileData') or b'')
        if key not in material_seen:
            material_seen.add(key)
            material_info.append(dict(metadata, name=reference['name'], path=key))
            if metadata.get('selection') == 'largest_original_raster':
                warnings.append(f"{reference['name']}: the source-layer link could not be recovered; the largest actual raster was selected.")
        return image,metadata

    tips = []
    if variant.get('BrushUsePatternImage'):
        for reference in _references(variant.get('BrushPatternImageArray')):
            image, metadata = material(reference)
            if metadata.get('LayerColorTypeIndex') in (1,2):
                mode = 'dual_color' if metadata.get('LayerColorTypeWhiteChecked') else 'mask'
                if metadata.get('LayerColorTypeIndex') == 2:
                    warnings.append('Packed monochrome coverage and bit order were recovered; the black/white plane assignment still needs a native CSP comparison.')
            elif metadata.get('LayerColorTypeIndex') == 0:
                mode = 'color'
            else:
                mode = 'color'
                warnings.append('Material expression color is unknown; the original RGBA colors were retained.')
            tips.append(BrushTip(reference['name'], image.width, image.height,
                                 _material_cache.png(reference['path']), mode, 'image'))
        if not tips:
            raise SutImportError("This brush selects image tips but has no tip materials.")
    else:
        tips.append(BrushTip())

    def number(key: str, default: float = 0.) -> float:
        return finite(variant.get(key), default)

    def percent(key: str, default: float = 0.) -> float:
        return clamp(number(key, default*100)/100)

    pattern_color = bool(variant.get('BrushChangePatternColor'))
    stroke_color = bool(variant.get('BrushChangeStrokeColor'))

    texture = None
    references = _references(variant.get('TextureImage'))
    if references:
        image, metadata = material(references[0])
        mode_id = int(number('TextureCompositeMode'))
        mode = _TEXTURE_MODES.get(mode_id)
        if mode is None:
            mode = 'multiply'
            warnings.append(f'Texture mode {mode_id} is not mapped; multiply is used and the original code is retained.')
        else:
            warnings.append('Texture mode numbers follow the installed CSP menu order; exact texture formulas still need CSP comparison.')
            if mode == 'normal':
                warnings.append('Normal texture currently uses the Multiply approximation; its density-preserving response still needs calibration.')
        texture = BrushTexture(name=references[0]['name'],png=_material_cache.png(references[0]['path'],texture=True),
                    scale=max(.01,number('TextureScale2',number('TextureScale',100))/100), angle=number('TextureRotate'),
                    density=percent('TextureDensity',1), mode=mode,
                    brightness=clamp(number('TextureBrightness')/100,-1,1),
                    contrast=clamp(number('TextureContrast')/100,-1,1),
                    invert=bool(variant.get('TextureReverseDensity')),per_dab=bool(variant.get('TextureForPlot')),
                    emphasize_density=bool(variant.get('TextureStressDensity')))
    dynamics = {}
    dynamics_map = {'BrushSizeEffector':'size','BrushOpacityEffector':'opacity','BrushFlowEffector':'density',
                    'BrushThicknessEffector':'thickness','BrushIntervalEffector':'spacing',
                    'TextureDensityEffector':'texture_density','BrushMixColorEffector':'paint_amount',
                    'BrushMixAlphaEffector':'paint_density','BrushBlurEffector':'blur',
                    'BrushSpraySizeEffector':'particle_size',
                    'BrushSprayDensityEffector':'particle_density'}
    for source, target in dynamics_map.items():
        if variant.get(source):
            try:
                diagnostics = []
                dynamics[target] = _dynamics(variant[source], diagnostics=diagnostics)
                warnings.extend(f'{source}: {message}' for message in diagnostics)
            except SutImportError as exc:
                warnings.append(f'{source}: {exc} The original bytes are retained.')
    if pattern_color:
        color_dynamics = {'BrushHueChangeEffector':'hue_shift',
                          'BrushSaturationChangeEffector':'saturation_shift',
                          'BrushValueChangeEffector':'luminosity_shift',
                          'BrushSubColorEffector':'sub_color_amount'}
        for source,target in color_dynamics.items():
            if variant.get(source):
                try:
                    diagnostics = []
                    dynamics[target] = _dynamics(variant[source],signed=target.endswith('_shift'),
                                                 tilt_enlargement=False,
                                                 diagnostics=diagnostics)
                    warnings.extend(f'{source}: {message}' for message in diagnostics)
                    effector = variant[source]
                    if _u32(effector,8)&0x20 and _u32(effector,40)!=100:
                        warnings.append('Color-input tilt maximum scaling is retained but not applied; its stored scale has not been calibrated.')
                except SutImportError as exc:
                    warnings.append(f'{source}: {exc} The original bytes are retained.')
    rotation_flags = int(number('BrushRotationEffector'))
    direction = 'stroke' if rotation_flags & 0x40 else 'tilt' if rotation_flags & 0x20 else 'fixed'
    if rotation_flags & 0x80:
        dynamics['angle'] = BrushDynamics(random=1-percent('BrushRotationRandomScale',1))
    if rotation_flags & ~0xe3:
        warnings.append('Some direction dynamics flags are not mapped; their original values are retained.')
    auto_spacing = int(number('BrushAutoIntervalType'))
    spacing = max(.001, number('BrushInterval',10)/100) if auto_spacing == 0 else {1:.5,2:.25,3:.1}.get(auto_spacing,.1)
    if auto_spacing:
        warnings.append('Automatic gap uses an approximation; its exact CSP spacing still needs visual calibration.')
    order = int(number('BrushPatternOrderType2', number('BrushPatternOrderType')))
    # CSP's "Reverse" traverses the sequence back and forth, whereas the
    # portable model's "reverse" is a descending repeated cycle. The six CSP
    # modes follow the documented menu order; controlled exports are still
    # needed to verify every stored enumeration across versions.
    repeat = {0:'forward',1:'pingpong',2:'hold_last',3:'random',4:'once',5:'one_random'}.get(order,'forward')
    if len(tips) > 1:
        warnings.append('Tip repeat ordering is mapped provisionally; compare its sequence with CSP.')
    if variant.get('BrushPatternReverse') and repeat == 'forward':
        repeat = 'reverse'
        warnings.append('The legacy pattern-reversal flag is mapped provisionally to descending order.')
    blend_id = int(number('CompositeMode'))
    blend = _INK_BLEND_MODES.get(blend_id,'normal')
    if blend_id not in _INK_BLEND_MODES:
        warnings.append(f'Ink blending mode {blend_id} is not mapped; normal is used and the original code is retained.')
    elif blend_id not in (0,27):
        warnings.append('Ink blend codes use the native compositing enumeration; exact brush blending formulas still need CSP comparison.')
    mix = 'none'
    if variant.get('BrushUseWaterColor'):
        mix = {0:'blend',1:'running',2:'smear'}.get(int(number('BrushWaterColor')),'blend')
        if variant.get('UseDualBrush') and _depth == 0:
            # CSP exposes only Smear while dual brushing is active. The stored
            # single-tip Blend/Running choice can remain in the source row.
            mix = 'smear'
        warnings.append('Color mixing and watercolor behavior are approximations pending CSP stroke comparison.')
    if variant.get('BrushUseSpray'):
        warnings.append('Spray density and deviation are mapped provisionally; particle distribution needs CSP calibration.')
    particle_flags = int(number('BrushRotationEffectorInSpray'))
    particle_direction = ('stroke' if particle_flags & 0x40 else
                          'whole_spray' if particle_flags & 0x100 else
                          'center' if particle_flags & 0x200 else 'fixed')
    if variant.get('BrushUseSpray'):
        if particle_flags & 0x300:
            warnings.append('Whole-spray/center particle direction flags are mapped provisionally; compare the particle orientation with CSP.')
        if sum(bool(particle_flags & bit) for bit in (0x40,0x100,0x200)) > 1:
            warnings.append('Multiple particle-direction flags are enabled; one direction is used and the original flags are retained.')
        if particle_flags & ~0x3c3:
            warnings.append('Some particle-direction flags are not mapped; their original values are retained.')
    if variant.get('BrushAdjustVelocity'):
        warnings.append('CSP Correct velocity input is retained but its algorithm switch is not applied; device-consistent sampling remains in use.')
    if variant.get('FlickerReductionBySpeed'):
        warnings.append('Speed-dependent stabilization is retained but not yet applied; the imported stabilization strength uses a fixed response.')
    if number('Stickness'):
        warnings.append('The nonzero source Stickness setting is retained but not applied; its possible pen-release taper behavior requires a controlled export.')
    if variant.get('BrushSharpenCorner'):
        warnings.append('Sharp-angle correction is retained but not yet applied.')
    if manager.get('Version') not in (131,138):
        warnings.append('This CSP settings version has not been validated against the reference brush pack.')
    global_pressure_curve = ((0.,0.),(1.,1.))
    if manager.get('PressureGraph'):
        if manager.get('PressureGraphInitialized') == 0 or manager.get('UsePressureFile') == 0:
            warnings.append('The source device-pressure graph is disabled or uninitialized; it is retained without applying it.')
        else:
            try:
                pressure_graph = manager['PressureGraph']
                if not isinstance(pressure_graph,bytes):
                    raise SutImportError('Unsupported device-pressure graph value.')
                global_pressure_curve = _curve(pressure_graph)
                warnings.append('The source device-pressure calibration applies to this imported preset before its brush dynamics; app tablet preferences are unchanged.')
            except SutImportError as exc:
                warnings.append(f'{exc} The original device-pressure graph is retained without applying it.')
    blur_link = variant.get('BrushBlurLinkSize')
    blur_mode = 'fixed' if blur_link == 0 else 'automatic'
    # Automatic is a separate choice, not the saved fixed-width slider scaled
    # by brush size. Keep that slider for switching back to Fixed value.
    blur_strength = 1. if blur_link == 1 else percent('BrushBlur')
    if mix == 'running':
        if blur_link not in (0,1):
            warnings.append('The Running-color blur width mode is missing or unknown; legacy normalized automatic blur is used and the original values are retained.')
        elif blur_mode == 'automatic':
            warnings.append('Automatic Running-color blur follows brush size using a capped normalized approximation; the dormant fixed width is preserved. The native automatic radius and blur kernel still need CSP comparison.')
        elif number('BrushBlur') > 0:
            warnings.append('Fixed Running-color blur preserves its pixel width and physical units; its five-sample blur kernel remains an approximation pending CSP comparison.')
    for key in ('BrushUseReferLayer','BrushUseVectorEraser','BrushUseVectorMagnet','BrushEraseAllLayer'):
        if variant.get(key):
            warnings.append(f'{key} is outside the raster brush tool and is not applied.')
    if pattern_color or stroke_color:
        warnings.append('Color changes, enabled response curves, and signed limits are mapped; their combined-input equation and random distribution still need CSP comparison.')
        warnings.append('The main/sub color-change target is mapped provisionally from the documented menu order; compare it with CSP.')
        if int(number('BrushChangeColorTarget',1)) not in (0,1,2):
            warnings.append('An unknown color-change target is approximated with main color; its original code is retained.')
    if stroke_color and any(number(key)<0 for key in ('BrushStrokeHueChange','BrushStrokeSaturationChange','BrushStrokeValueChange')):
        warnings.append('A signed per-stroke color amount is mapped to its random-amplitude magnitude; its native interpretation has not been calibrated.')
    if ((pattern_color and number('BrushSubColor')) or (stroke_color and number('BrushStrokeSubColor'))) and any(t.mode=='color' for t in tips):
        warnings.append('Sub-color blending is not applied to full-color image tips; the source amounts are retained.')
    ribbon = bool(variant.get('BrushRibbon') and variant.get('BrushUsePatternImage')
                  and not variant.get('BrushUseSpray'))
    if variant.get('BrushRibbon') and not ribbon:
        warnings.append('The stored Ribbon flag is inactive with spraying or a round tip; its original value is retained.')
    if ribbon:
        warnings.append('Ribbon deformation and repeat ordering still need comparison with a CSP chain brush.')
        ribbon_angle = number('BrushRotation') % 90
        if min(ribbon_angle, 90-ribbon_angle) > 1e-6:
            warnings.append('Angled ribbon tips may show gaps or distortion between repeats; matching these settings to CSP is still unfinished.')
        if rotation_flags & 0x30 or (rotation_flags & 0x80 and percent('BrushRotationRandomScale',1)>0):
            warnings.append('Dynamic ribbon angle changes are not yet applied by the ribbon renderer; the angle settings are retained.')
        if mix != 'none':
            warnings.append('Color mixing is not yet applied to ribbon strokes; both source settings are retained.')
        if variant.get('BrushContinuousPlot'):
            warnings.append('Time-based paint buildup is not yet applied to ribbons, whether moving or held still; the source continuous-spraying setting is retained.')
    # Preserve binary and unknown parameters for future version/mapping work.
    source = {'format':'csp-sut','filename':filename,'sha256':checksum,
              'manager':{k:_json_value(v) for k,v in manager.items()},
              'node':{k:_json_value(v) for k,v in node.items()},
              'variant':{k:_json_value(v) for k,v in variant.items()},
              'materials':material_info, 'importer_version':8}
    if reconstruction:
        source['reconstruction'] = reconstruction
    taper_targets = []
    taper_minima = []
    target_data = variant.get('BrushInOutTarget')
    if isinstance(target_data,bytes) and len(target_data)>=12 and _u32(target_data,0)==12 and _u32(target_data,8)==12:
        parameters={1001:'size',1021:'opacity',1041:'density',1051:'spacing',1082:'thickness',1084:'texture_density',
                    1093:'paint_amount',1100:'paint_density',1132:'particle_size',1134:'particle_density'}
        for cursor in range(12,min(len(target_data),12+12*_u32(target_data,4)),12):
            if _u32(target_data,cursor+4) and _u32(target_data,cursor) in parameters:
                taper_targets.append(parameters[_u32(target_data,cursor)])
                taper_minima.append(clamp(_u32(target_data,cursor+8)/100))
    taper_type = int(number('BrushInOutType'))
    taper_mode = {0:'length',1:'percentage',2:'fade'}.get(taper_type,'length')
    taper_supported = taper_type in (0,1,2)
    taper_minimum = taper_minima[0] if taper_minima else 0.
    if taper_targets and (variant.get('BrushUseIn') or variant.get('BrushUseOut')):
        if taper_supported:
            warnings.append('Starting/ending length, percentage or Fade values are imported; their exact envelope and speed adjustment need CSP calibration.')
            if taper_type == 2:
                warnings.append('The Fade mode number follows the documented menu order; its stored enumeration still needs a controlled CSP export.')
        else:
            warnings.append('This starting/ending mode is not mapped; taper is disabled and the source settings are retained.')
    if variant.get('BrushUseRevision'):
        warnings.append('Post-correction strength is imported with an independent Gaussian smoother; CSP spline/Bezier methods are not yet implemented.')
        if variant.get('BrushRevisionBySpeed') or variant.get('BrushRevisionByViewScale'):
            warnings.append('Enabled post-correction speed/view-scale adjustments are retained but not yet applied.')
    if taper_targets and (variant.get('BrushUseIn') or variant.get('BrushUseOut')) and variant.get('BrushInOutBySpeed'):
        warnings.append('Speed-dependent starting/ending lengths are retained but not yet applied.')
    dual = None
    if variant.get('UseDualBrush') and _depth == 0:
        child = {'Opacity':100,'CompositeMode':0,'VariantID':variant.get('VariantID')}
        for key,value in variant.items():
            if key.startswith('Dual') and value is not None:
                suffix = key[4:]
                if suffix.startswith('Texture') or suffix == 'AntiAlias':
                    target = suffix
                elif suffix == 'BrushCompositeMode':
                    continue
                else:
                    target = 'Brush'+suffix
                if target == 'BrushPatternOrderType':
                    target = 'BrushPatternOrderType2'
                child[target] = value
        child_node = dict(node,NodeName=str(node.get('NodeName','Brush'))+' secondary')
        dual = _build_definition(filename,checksum,child_node,child,manager,material_rows,reconstruction,
                                 dpi=dpi,_depth=1,_material_cache=_material_cache)
        warnings.append('Dual brush settings and original secondary materials are imported; combining formulas still require CSP comparison.')
        secondary_effects = []
        if dual.watercolor_edge:
            secondary_effects.append('watercolor edge')
        if dual.blending_mode != 'normal':
            secondary_effects.append('canvas blending mode')
        if dual.post_correction:
            secondary_effects.append('post-correction')
        if secondary_effects:
            warnings.append('Secondary-brush '+', '.join(secondary_effects)+' settings are retained but are not applied independently of the primary brush.')
        warnings.extend(dual.warnings)
    dual_mode_id = int(number('DualBrushCompositeMode',1))
    dual_mode = _DUAL_BLEND_MODES.get(dual_mode_id,'multiply')
    if dual and dual_mode_id not in _DUAL_BLEND_MODES:
        warnings.append(f'Dual blending mode {dual_mode_id} is not mapped; multiply is used and the original code is retained.')
    flip_modes = {0:'none',1:'fixed',2:'random',3:'reverse'}
    relative_particle = bool(variant.get('BrushSpraySizeSyncBrushSize'))
    units = BrushLengthConversion(dpi)

    def length(target, field, default=0., *, active=True, minimum=0., maximum=10000.):
        return units.length(target,field,number(field,default),variant.get(field+'Unit',0),
                            active=active,minimum=minimum,maximum=maximum)

    brush_size = length('size','BrushSize',16,minimum=.1,maximum=4096)
    particle_size = (max(.001,number('BrushSpraySize',4)/100) if relative_particle else
                     length('particle_size','BrushSpraySize',4,active=bool(variant.get('BrushUseSpray')),
                            minimum=.1,maximum=4096))
    edge_enabled = bool(variant.get('BrushUseWaterEdge'))
    edge = length('watercolor_edge','BrushWaterEdgeRadius',maximum=100) if edge_enabled else 0.
    edge_blur = length('watercolor_blur','BrushWaterEdgeBlur',
                       active=edge_enabled and bool(variant.get('BrushWaterEdgeAfterDrag')),maximum=100)
    blur_width = length('blur_width','BrushBlur',active=mix == 'running' and blur_mode == 'fixed')
    taper_start = taper_end = 0.
    if variant.get('BrushUseIn') and taper_targets and taper_supported and taper_type != 2:
        taper_start = (number('BrushInRatio') if taper_type == 1 else
                       length('taper_start','BrushInLength'))
    if variant.get('BrushUseOut') and taper_targets and taper_supported:
        taper_end = (number('BrushOutRatio') if taper_type == 1 else
                     length('taper_end','BrushOutLength'))
    source['length_units'] = units.metadata()
    warnings.extend(units.warnings)
    result = BrushDefinition(
        id='sut-'+checksum[:20]+('-secondary' if _depth else ''), name=str(node.get('NodeName') or Path(filename).stem),
        size=brush_size, size_by_view=bool(variant.get('BrushSizeSyncViewScale')),
        opacity=percent('Opacity',1), density=percent('BrushFlow',1),
        hardness=percent('BrushHardness',1),thickness=max(.01,number('BrushThickness',100)/100),
        thickness_axis='vertical' if variant.get('BrushVerticalThicknes') else 'horizontal',
        angle=number('BrushRotation'),direction=direction,antialiasing=int(clamp(number('AntiAlias',2),0,3)),
        flip_x=flip_modes.get(int(number('BrushPatternReverseHorizontal')),'none'),
        flip_y=flip_modes.get(int(number('BrushPatternReverseVertical')),'none'),
        minimum_pixel=bool(variant.get('BrushAtLeast1Pixel',1)),spacing=spacing,
        density_by_gap=bool(variant.get('BrushAdjustFlowByInterval')),tips=tuple(tips),repeat_mode=repeat,
        ribbon=ribbon,continuous=bool(variant.get('BrushContinuousPlot')),
        correct_velocity=bool(variant.get('BrushAdjustVelocity')),
        spray=bool(variant.get('BrushUseSpray')),particle_size=particle_size,
        particle_size_relative=relative_particle,
        particle_density=max(1,number('BrushSprayDensity',8)),spray_deviation=clamp(number('BrushSprayBias',50)/100,-1,1),
        particle_angle=number('BrushRotationInSpray'),
        particle_angle_random=percent('BrushRotationRandomInSpray',1) if particle_flags & 0x80 else 0.,
        particle_direction=particle_direction,
        texture=texture,dynamics=dynamics,global_pressure_curve=global_pressure_curve,blending_mode=blend,
        blend_tips='darken' if variant.get('BrushBlendPatternByDarken') else 'normal',mixing_mode=mix,
        paint_amount=percent('BrushMixColor',.5),paint_density=percent('BrushMixAlpha',1),
        color_stretch=percent('BrushMixColorExtension',.5),blur=blur_strength,
        blur_mode=blur_mode,blur_width=blur_width,
        watercolor_edge=edge,
        watercolor_opacity=percent('BrushWaterEdgeAlphaPower',.5),watercolor_darkness=percent('BrushWaterEdgeValuePower',.5),
        watercolor_blur=edge_blur,watercolor_after=bool(variant.get('BrushWaterEdgeAfterDrag')),
        dual=dual,dual_mode=dual_mode,dual_link_size=bool(variant.get('SyncDualBrushSize')),
        hue_shift=clamp(number('BrushHueChange')/360,-1,1) if pattern_color else 0.,
        saturation_shift=clamp(number('BrushSaturationChange')/100,-1,1) if pattern_color else 0.,
        luminosity_shift=clamp(number('BrushValueChange')/100,-1,1) if pattern_color else 0.,
        sub_color_amount=percent('BrushSubColor') if pattern_color else 0.,
        color_change_target={0:'both',1:'main',2:'sub'}.get(int(number('BrushChangeColorTarget',1)),'main'),
        stroke_hue_jitter=clamp(abs(number('BrushStrokeHueChange'))/360) if stroke_color else 0.,
        stroke_saturation_jitter=clamp(abs(number('BrushStrokeSaturationChange'))/100) if stroke_color else 0.,
        stroke_luminosity_jitter=clamp(abs(number('BrushStrokeValueChange'))/100) if stroke_color else 0.,
        stroke_sub_color_mix=percent('BrushStrokeSubColor') if stroke_color else 0.,
        stabilization=percent('FlickerReduction'),
        post_correction=percent('BrushRevision') if variant.get('BrushUseRevision') else 0.,
        taper_mode=taper_mode,taper_minimum=taper_minimum,taper_minima=dict(zip(taper_targets,taper_minima)),
        taper_start=taper_start,taper_end=taper_end,
        taper_parameters=tuple(taper_targets),source=source,warnings=tuple(dict.fromkeys(warnings)))
    return result
