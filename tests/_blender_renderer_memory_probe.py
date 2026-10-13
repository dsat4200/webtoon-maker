"""Independent RGBA/PNG parity and bounded rendering workspace in Blender."""
import binascii
import os
from pathlib import Path
import struct
import sys
import tracemalloc
import zlib

import numpy as np

sys.path.insert(0, str(Path(os.environ["WEBTOON_EXTENSION_ROOT"]).parent))
from webtoon_comic_views import renderer


def reference_alpha(raw, width, height):
    source = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)
    result = source.copy()
    for channel in range(3):
        color = source[:, :, channel].astype(np.uint32)
        alpha = source[:, :, 3].astype(np.uint32)
        result[:, :, channel] = np.where(
            alpha > 0,
            np.minimum(255, (color * 255 + alpha // 2) // np.maximum(1, alpha)),
            0,
        )
    return result[::-1].tobytes()


def decode_png(encoded, width, height):
    assert encoded[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    compressed = bytearray()
    while offset < len(encoded):
        size = struct.unpack_from(">I", encoded, offset)[0]
        kind = encoded[offset + 4:offset + 8]
        data = encoded[offset + 8:offset + 8 + size]
        crc = struct.unpack_from(">I", encoded, offset + 8 + size)[0]
        assert crc == binascii.crc32(kind + data) & 0xFFFFFFFF
        if kind == b"IHDR":
            assert struct.unpack(">IIBBBBB", data) == (width, height, 8, 6, 0, 0, 0)
        elif kind == b"IDAT":
            compressed.extend(data)
        offset += size + 12
    rows = np.frombuffer(zlib.decompress(compressed), np.uint8).reshape(height, width * 4 + 1)
    assert np.all(rows[:, 0] == 0)
    return rows[:, 1:].tobytes()


# Every RGB-byte / alpha-byte combination, plus asymmetric multi-strip images.
samples = np.empty((256, 256, 4), dtype=np.uint8)
samples[:, :, :3] = np.arange(256, dtype=np.uint8)[None, :, None]
samples[:, :, 3] = np.arange(256, dtype=np.uint8)[:, None]
for width, height, raw in [
    (256, 256, samples.tobytes()),
    (133, 509, np.random.default_rng(54).integers(0, 256, (509, 133, 4), dtype=np.uint8).tobytes()),
    (1, 1, bytes((1, 2, 3, 4))),
]:
    result = renderer._to_top_down_straight_alpha(raw, width, height)
    assert result == reference_alpha(raw, width, height)
    assert decode_png(renderer.png_bytes(renderer.RenderFrame(width, height, result)), width, height) == result

width, height = 1024, 768
raw = np.random.default_rng(4).integers(0, 256, (height, width, 4), dtype=np.uint8).tobytes()
tracemalloc.start()
result = renderer._to_top_down_straight_alpha(raw, width, height)
peak = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
assert peak < len(raw) * 3 + 4 * 1024 * 1024, peak
assert result == reference_alpha(raw, width, height)
blank = renderer.RenderFrame(width, height, bytes(len(raw)))
tracemalloc.start()
encoded = renderer.png_bytes(blank)
peak = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
assert peak < 2 * 1024 * 1024, peak
assert decode_png(encoded, width, height) == bytes(len(raw))
try:
    renderer.png_bytes(renderer.RenderFrame(10, 10, b""))
except ValueError:
    pass
else:
    raise AssertionError("malformed RGBA length accepted")
print("WEBTOON_RENDERER_MEMORY_PROBE_OK")
