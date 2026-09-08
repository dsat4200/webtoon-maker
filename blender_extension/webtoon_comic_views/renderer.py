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
THUMBNAIL_ICON_LIMIT = 32


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
    *, hide_overlays: bool = False,
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
    if view_matrix is None or projection_matrix is None:
        camera_view, camera_projection = viewport.render_matrices(
            scene, view_layer, bounds
        )
        view_matrix = camera_view if view_matrix is None else view_matrix.copy()
        projection_matrix = (
            camera_projection
            if projection_matrix is None else projection_matrix.copy()
        )
    else:
        view_matrix = view_matrix.copy()
        projection_matrix = projection_matrix.copy()
    offscreen = gpu.types.GPUOffScreen(width, height, format="RGBA8")
    overlays = bool(space.overlay.show_overlays)
    try:
        if hide_overlays:
            space.overlay.show_overlays = False
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
        if hide_overlays:
            space.overlay.show_overlays = overlays
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
    *, hide_overlays: bool = False,
) -> RenderFrame:
    ratio = min(
        THUMBNAIL_LIMIT / max(1, int(source_width)),
        THUMBNAIL_LIMIT / max(1, int(source_height)),
    )
    width = max(64, round(source_width * ratio))
    height = max(64, round(source_height * ratio))
    return render_active_camera(
        scene, view_layer, width, height, stream_frame=stream_frame,
        hide_overlays=hide_overlays,
    )


def _resize_pixels(source: np.ndarray, limit: int) -> np.ndarray:
    source_height, source_width = source.shape[:2]
    ratio = min(1.0, limit / max(1, source_width, source_height))
    width = max(1, round(source_width * ratio))
    height = max(1, round(source_height * ratio))
    if (width, height) == (source_width, source_height):
        return np.ascontiguousarray(source)
    rows = np.linspace(0, source_height - 1, height).round().astype(int)
    columns = np.linspace(0, source_width - 1, width).round().astype(int)
    # Index both axes together: the intermediate must stay thumbnail-sized,
    # even when reducing a large, wide render.
    return np.ascontiguousarray(source[rows[:, None], columns])


def thumbnail_from_frame(frame: RenderFrame) -> RenderFrame:
    """Create a compact nearest-neighbor preview from an existing render."""
    source = np.frombuffer(frame.rgba, dtype=np.uint8).reshape(
        frame.height, frame.width, 4
    )
    resized = _resize_pixels(source, THUMBNAIL_LIMIT)
    height, width = resized.shape[:2]
    if (width, height) == (frame.width, frame.height):
        return frame
    return RenderFrame(width, height, resized.tobytes())


def _set_thumbnail_preview(image: object, pixels: np.ndarray) -> None:
    """Replace both cached UI sizes, using Blender's bottom-up pixel order."""
    preview = image.preview_ensure()
    for kind, limit in (("image", THUMBNAIL_LIMIT), ("icon", THUMBNAIL_ICON_LIMIT)):
        resized = _resize_pixels(pixels, limit)
        height, width = resized.shape[:2]
        setattr(preview, f"{kind}_size", (width, height))
        setattr(preview, f"is_{kind}_custom", True)
        getattr(preview, f"{kind}_pixels_float").foreach_set(resized.reshape(-1))


def ensure_thumbnail_preview(view: object) -> None:
    """Seed old .blend thumbnails once at load; panel draws only read previews."""
    image = bpy.data.images.get(view.thumbnail_image)
    if image is None:
        return
    # Older versions packed generated images without changing their source;
    # Blender regenerates a blank buffer for those on reopening the .blend.
    if image.source == "GENERATED" and image.packed_file is not None:
        image.source = "FILE"
    preview = image.preview
    if preview is not None and preview.is_image_custom and preview.is_icon_custom:
        return
    width, height = tuple(image.size)
    if width <= 0 or height <= 0:
        return
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    _set_thumbnail_preview(image, pixels.reshape(height, width, 4))


def release_thumbnail_image(name: str) -> None:
    """Free an unused thumbnail and its preview, preserving shared images."""
    if not name or any(
        view.thumbnail_image == name
        for scene in bpy.data.scenes
        for view in getattr(scene, "webtoon_comic_views", ())
    ):
        return
    image = bpy.data.images.get(name)
    if image is not None and image.users <= int(image.use_fake_user):
        bpy.data.images.remove(image)


def update_thumbnail_image(view: object, frame: RenderFrame) -> None:
    """Commit a fresh packed image and UI preview after successful preparation."""
    frame = thumbnail_from_frame(frame)
    encoded = png_bytes(frame)
    encoded_text = base64.b64encode(encoded).decode("ascii")
    previous_name = str(view.thumbnail_image)
    rows = np.frombuffer(frame.rgba, dtype=np.uint8).reshape(
        frame.height, frame.width, 4
    )
    pixels = np.ascontiguousarray(np.flipud(rows), dtype=np.float32) / 255.0
    # Duplicated Comic Views can initially share a packed thumbnail. Build a
    # replacement before changing either the image or preview seen by a view.
    image = bpy.data.images.new(
        f"Webtoon Comic View {view.view_uuid}",
        width=frame.width, height=frame.height, alpha=True,
    )
    try:
        image.pixels.foreach_set(pixels.reshape(-1))
        image.update()
        image.use_fake_user = True
        image.pack(data=encoded, data_len=len(encoded))
        image.source = "FILE"
        _set_thumbnail_preview(image, pixels)
    except Exception:
        bpy.data.images.remove(image)
        raise
    view.thumbnail_image = image.name
    view.thumbnail_png = encoded_text
    release_thumbnail_image(previous_name)
