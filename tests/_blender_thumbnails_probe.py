"""Exercise real Blender image previews without a GPU render or user scene."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import sys
import uuid

import bpy
import numpy as np


extension_root = Path(os.environ["WEBTOON_EXTENSION_ROOT"])
sys.path.insert(0, str(extension_root.parent))

import webtoon_comic_views as addon  # noqa: E402
from webtoon_comic_views import bridge, renderer  # noqa: E402


def frame(red: int, green: int, blue: int) -> renderer.RenderFrame:
    pixels = np.empty((128, 64, 4), dtype=np.uint8)
    pixels[:] = (red, green, blue, 255)
    # Two rows distinguish Blender's bottom-up image order from render order.
    pixels[0] = (20, 40, 60, 255)
    pixels[-1] = (80, 100, 120, 255)
    return renderer.RenderFrame(64, 128, pixels.tobytes())


def check_preview(view: object, expected: renderer.RenderFrame) -> None:
    image = bpy.data.images[view.thumbnail_image]
    preview = image.preview
    assert preview is not None
    assert preview.is_image_custom and preview.is_icon_custom
    assert tuple(preview.image_size) == (expected.width, expected.height)
    assert tuple(preview.icon_size) == (16, 32)
    pixels = np.frombuffer(expected.rgba, dtype=np.uint8).reshape(128, 64, 4)
    expected_pixels = np.flipud(pixels).reshape(-1).astype(np.float32) / 255.0
    assert np.allclose(image.pixels[:], expected_pixels, atol=1 / 255)
    assert np.allclose(preview.image_pixels_float[:], expected_pixels, atol=1 / 255)
    assert np.allclose(preview.icon_pixels_float[:4], expected_pixels[:4], atol=1 / 255)
    assert base64.b64decode(view.thumbnail_png) == renderer.png_bytes(expected)
    assert image.packed_file is not None
    if not bpy.app.background:
        assert preview.icon_id > 0


addon.register()
runtime = bridge.RUNTIME
original_render = runtime._render_saved_snapshot
original_preview = renderer._set_thumbnail_preview
original_write = bridge._atomic_write
try:
    addon._initialize_scenes()
    scene = bpy.context.scene
    view = scene.webtoon_comic_views.add()
    view.view_uuid = uuid.uuid4().hex
    view.name = "Thumbnail source"
    first_frame, second_frame = frame(255, 0, 0), frame(0, 0, 255)
    runtime._render_saved_snapshot = lambda *_args: (first_frame, [])
    runtime.render_saved_view(scene, view)
    check_preview(view, first_frame)
    first_name = view.thumbnail_image

    # A duplicated view starts with the source thumbnail but publishes its own
    # replacement. Neither source pixels nor its cached icon may be modified.
    duplicate = scene.webtoon_comic_views.add()
    duplicate.view_uuid = uuid.uuid4().hex
    duplicate.name = "Thumbnail copy"
    duplicate.thumbnail_image = view.thumbnail_image
    duplicate.thumbnail_png = view.thumbnail_png
    runtime._render_saved_snapshot = lambda *_args: (second_frame, [])
    runtime.render_saved_view(scene, duplicate)
    assert view.thumbnail_image != duplicate.thumbnail_image
    check_preview(view, first_frame)
    check_preview(duplicate, second_frame)

    # Re-rendering replaces both preview sizes and frees the unused old image.
    runtime.render_saved_view(scene, view)
    check_preview(view, second_frame)
    assert bpy.data.images.get(first_name) is None
    prior = (view.revision, view.thumbnail_image, view.thumbnail_png)
    prior_images = set(bpy.data.images.keys())

    # Preparation failure leaves both visible preview and metadata untouched,
    # and must not leak the uncommitted replacement image.
    def fail_preview(*_args):
        raise RuntimeError("intentional thumbnail preparation failure")

    renderer._set_thumbnail_preview = fail_preview
    runtime._render_saved_snapshot = lambda *_args: (first_frame, [])
    try:
        runtime.render_saved_view(scene, view)
    except RuntimeError as error:
        assert "intentional thumbnail" in str(error)
    else:
        raise AssertionError("thumbnail preparation failure was ignored")
    assert prior == (view.revision, view.thumbnail_image, view.thumbnail_png)
    assert set(bpy.data.images.keys()) == prior_images
    check_preview(view, second_frame)
    renderer._set_thumbnail_preview = original_preview

    def fail_write(*_args):
        raise OSError("intentional publication failure")

    bridge._atomic_write = fail_write
    try:
        runtime.render_saved_view(scene, view)
    except OSError:
        pass
    else:
        raise AssertionError("publication failure was ignored")
    assert prior == (view.revision, view.thumbnail_image, view.thumbnail_png)
    check_preview(view, second_frame)
    bridge._atomic_write = original_write

    # Background probes verify persistence on both supported versions. GUI probes
    # keep their live window context while checking real preview icon IDs.
    source_uuid = view.view_uuid
    if bpy.app.background:
        saved = Path(os.environ["WEBTOON_COMIC_VIEW_FRAME_ROOT"]).parent / "thumbnails.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(saved))
        bpy.ops.wm.open_mainfile(filepath=str(saved))
        scene = bpy.context.scene
        view = next(item for item in scene.webtoon_comic_views if item.view_uuid == source_uuid)
        check_preview(view, second_frame)

    # Older add-ons wrote images without replacing their auto-generated icon.
    # Initialization repairs those previews outside the drawing callback.
    image = bpy.data.images[view.thumbnail_image]
    image.source = "GENERATED"
    image.preview.is_image_custom = False
    image.preview.is_icon_custom = False
    renderer.ensure_thumbnail_preview(view)
    check_preview(view, second_frame)

    # Deletion preserves cross-scene references and frees the last owner.
    shared_scene = bpy.data.scenes.new("Thumbnail sharing probe")
    shared_view = shared_scene.webtoon_comic_views.add()
    shared_view.thumbnail_image = view.thumbnail_image
    shared_name = view.thumbnail_image
    source_index = next(
        index for index, item in enumerate(scene.webtoon_comic_views)
        if item.view_uuid == source_uuid
    )
    scene.webtoon_comic_settings.active_index = source_index
    assert bpy.ops.webtoon.delete_comic_view() == {"FINISHED"}
    assert bpy.data.images.get(shared_name) is not None
    shared_scene.webtoon_comic_views.clear()
    renderer.release_thumbnail_image(shared_name)
    assert bpy.data.images.get(shared_name) is None
    # Leave this empty test scene to process shutdown: Blender 4.5 GUI loses
    # its window context after a scripted reopen and crashes removing a scene.
    print("WEBTOON_THUMBNAILS_PROBE_OK", bpy.app.version_string)
finally:
    runtime._render_saved_snapshot = original_render
    renderer._set_thumbnail_preview = original_preview
    bridge._atomic_write = original_write
    addon.unregister()
