"""Transparent cropped-viewport rendering for Comic Views."""
from __future__ import annotations

import base64
import binascii
import struct
import zlib
from dataclasses import dataclass

import bpy
import gpu
import numpy as np

from . import viewport


MAX_AXIS = 4096
MAX_PIXELS = 16_777_216
THUMBNAIL_LIMIT = 256


@dataclass(frozen=True)
class RenderFrame:
    width: int
    height: int
    rgba: bytes


def validate_resolution(width: object, height: object) -> tuple[int, int]:
    width, height = int(width), int(height)
    if not 64 <= width <= MAX_AXIS or not 64 <= height <= MAX_AXIS:
        raise ValueError("Comic View dimensions must be between 64 and 4096")
    if width * height > MAX_PIXELS:
        raise ValueError("Comic View resolution cannot exceed 16 megapixels")
    return width, height


def _buffer_bytes(buffer: object, width: int, height: int) -> bytes:
    """Copy Blender's strided GPU buffer as packed, interleaved RGBA8."""
    expected = int(width) * int(height) * 4
    try:
        # GPUFrameBuffer.read_color exposes a multidimensional Buffer whose
        # Python memoryview strides are channel-planar.  Flatten through the
        # Blender Buffer API before copying or those backing bytes become
        # colored vertical stripes instead of interleaved RGBA pixels.
        buffer.dimensions = expected
        pixels = np.asarray(buffer, dtype=np.uint8)
    except (AttributeError, TypeError, ValueError) as error:
        raise RuntimeError("Could not flatten the viewport GPU buffer") from error
    if pixels.size != expected:
        raise RuntimeError(
            f"Viewport readback returned {pixels.size} components; "
            f"expected {expected}"
        )
    return np.ascontiguousarray(pixels.reshape(expected)).tobytes()


def _to_top_down_straight_alpha(
    raw: bytes, width: int, height: int,
) -> bytes:
    """Flip OpenGL rows and convert transparent compositing to straight RGBA."""
    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(
        height, width, 4
    ).copy()
    alpha = pixels[:, :, 3].astype(np.uint16)
    rgb = pixels[:, :, :3].astype(np.uint16)
    nonzero = alpha > 0
    denominator = np.maximum(alpha, 1)[:, :, None]
    straight = np.minimum(
        255, (rgb * 255 + denominator // 2) // denominator
    ).astype(np.uint8)
    pixels[:, :, :3] = np.where(nonzero[:, :, None], straight, 0)
    return np.flipud(pixels).tobytes()


def render_active_camera(
    scene: bpy.types.Scene, view_layer: bpy.types.ViewLayer,
    width: int, height: int, stream_frame: object = None,
    view_matrix: object = None, projection_matrix: object = None,
) -> RenderFrame:
    """Render the bound viewport frame into top-down straight RGBA8."""
    width, height = validate_resolution(width, height)
    _window, _area, space, region = viewport.find_view3d()
    if space is None or region is None:
        raise RuntimeError("Open a 3D View before streaming Comic Views")
    bounds = (
        tuple(float(item) for item in stream_frame)
        if stream_frame is not None else viewport.DEFAULT_FRAME
    )
    view_matrix = (
        view_matrix.copy() if view_matrix is not None
        else space.region_3d.view_matrix.copy()
    )
    projection_matrix = (
        projection_matrix.copy() if projection_matrix is not None
        else viewport.cropped_projection(
            space.region_3d.window_matrix.copy(), bounds,
        )
    )
    offscreen = gpu.types.GPUOffScreen(width, height, format="RGBA8")
    try:
        with offscreen.bind():
            offscreen.draw_view3d(
                scene,
                view_layer,
                space,
                region,
                view_matrix,
                projection_matrix,
                do_color_management=True,
                draw_background=False,
            )
            framebuffer = gpu.state.active_framebuffer_get()
            buffer = framebuffer.read_color(
                0, 0, width, height, 4, 0, "UBYTE"
            )
            raw = _buffer_bytes(buffer, width, height)
    finally:
        offscreen.free()
    expected = width * height * 4
    if len(raw) != expected:
        raise RuntimeError(
            f"Viewport readback returned {len(raw)} bytes; expected {expected}"
        )
    return RenderFrame(
        width, height, _to_top_down_straight_alpha(raw, width, height)
    )


def png_bytes(frame: RenderFrame) -> bytes:
    """Encode one RGBA8 frame with only the Python standard library."""
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
        )

    stride = frame.width * 4
    scanlines = b"".join(
        b"\x00" + frame.rgba[offset:offset + stride]
        for offset in range(0, len(frame.rgba), stride)
    )
    header = struct.pack(">IIBBBBB", frame.width, frame.height, 8, 6, 0, 0, 0)
    return (
        signature + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, 6))
        + chunk(b"IEND", b"")
    )


def render_thumbnail(
    scene: bpy.types.Scene, view_layer: bpy.types.ViewLayer,
    source_width: int, source_height: int, stream_frame: object = None,
) -> RenderFrame:
    ratio = min(
        THUMBNAIL_LIMIT / max(1, int(source_width)),
        THUMBNAIL_LIMIT / max(1, int(source_height)),
    )
    width = max(64, round(source_width * ratio))
    height = max(64, round(source_height * ratio))
    return render_active_camera(
        scene, view_layer, width, height, stream_frame=stream_frame
    )


def update_thumbnail_image(view: object, frame: RenderFrame) -> None:
    """Store a packed preview image and compact PNG copy in the .blend."""
    name = str(view.thumbnail_image or f"Webtoon Comic View {view.view_uuid}")
    image = bpy.data.images.get(name)
    if image is None:
        image = bpy.data.images.new(
            name, width=frame.width, height=frame.height, alpha=True
        )
    elif tuple(image.size) != (frame.width, frame.height):
        image.scale(frame.width, frame.height)
    rows = np.frombuffer(frame.rgba, dtype=np.uint8).reshape(
        frame.height, frame.width, 4
    )
    pixels = np.flipud(rows).astype(np.float32).reshape(-1) / 255.0
    image.pixels.foreach_set(pixels)
    image.update()
    image.use_fake_user = True
    encoded = png_bytes(frame)
    try:
        image.pack(data=encoded, data_len=len(encoded))
    except RuntimeError:
        # Generated images are already stored in the blend; packing is an
        # additional size optimization and is not required for persistence.
        pass
    view.thumbnail_image = image.name
    view.thumbnail_png = base64.b64encode(encoded).decode("ascii")
