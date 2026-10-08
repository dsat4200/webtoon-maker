"""Exact snapshot output through the ordinary scene service and native grids."""
from dataclasses import replace
import copy
import math
from pathlib import Path
import uuid
from types import SimpleNamespace

import numpy as np
from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath

from comic_editor.core.assets import entity_visual_bounds
from comic_editor.core.images import ImageStore
from comic_editor.core.tiles import TileStore
from comic_editor.render.admission import RENDER_ADMISSION, WorkCancelled, snapshot_working_bytes
from comic_editor.render.device import cpu_image
from comic_editor.render.pixels import (
    capture_color_environment, display_image, export_image, pixel_scope, premultiplied_pixels,
)
from comic_editor.render.scene import DetachedSceneBackend, SceneSnapshot, SCENE_FIELDS
from comic_editor.render.service import DocumentRenderService, RenderDocument, RenderRequest, RenderQuality


class OutputCancelled(WorkCancelled):
    pass


def capture_document(chapter, tiles, images=None, *, pixel_environment=None):
    """Freeze non-editor sources, including an inactive asset, for output work."""
    document = copy.deepcopy(chapter)
    images = images or ImageStore()
    sources = tiles.detached_snapshot(set(tiles._tiles))
    state = dict.fromkeys(SCENE_FIELDS)
    state.update(selected_kind='', selected_id='', selected_object_id='', selected_entities=set(),
        _solo_entities=set(), _solo_suspended=True, _drawing_selection_path=QPainterPath(),
        _multi_transform_preview_quads={}, _multi_transform_start_world_quads={},
        _live_underlay_amount=0., _live_underlay_object_id='', _render_base_alpha=False,
        _render_cage_source=False, _render_exclude_text=False, _render_excluded_object_id='',
        _render_modifier_sources=set(), _rendering_compound_references=set(),
        _rendering_mask_contributor=0, _rendering_outward_gradient=False,
        _rendering_halftone_source=False, _selection_vector_preview={},
        _selection_vector_preview_revision=0, _suppress_outline_for_mask=False,
        _text_caret_visible=False, _text_cursor_position=0, _text_editing=False,
        _text_selection_anchor=0, _vector_eraser_preview={}, _vector_eraser_preview_revision=0,
        _vector_eraser_preview_versions={}, _vector_gesture_mode='', _vector_preview_id='output-preview',
        _vector_preview_tiles=TileStore(), _vector_samples=[], _vector_simplify_overlay=[],
        _vector_sweep=[], _selection_raster_states={}, _selection_shape_nodes=[],
        _mask_wand_sample_entities=set(), _history_generation=0, _gradient_preview_active=False)
    metadata = RenderDocument((id(document), id(sources), id(images)), (), 0,
                              document.width, document.height, document.background,
                              pixel_contract=document.pixel_contract)
    retained_images = images.clone()
    for key, image in images._decoded.items():
        retained_images._cache_decoded(key, QImage(image))
    return SceneSnapshot(metadata, document, sources, retained_images, state,
                         (document.width, document.height),
                         SimpleNamespace(predictive_ink=False, canvas_renderer='raster'),
                         pixel_environment=(pixel_environment or
                                            capture_color_environment(document.pixel_contract)))


def render_snapshot(snapshot, region=None, *, scale=1., target=None, cancelled=None):
    region = tuple(region or (0., 0., snapshot.document.width, snapshot.document.height))
    estimate = snapshot_working_bytes(snapshot) + math.ceil(region[2]*scale)*math.ceil(region[3]*scale)*64
    stop = cancelled.is_set if cancelled is not None else lambda: False
    try:
        with RENDER_ADMISSION.reserve('export', estimate, priority=1, cancelled=stop):
            return _render_snapshot(snapshot, region, scale=scale, target=target)
    except WorkCancelled as error:
        raise OutputCancelled() from error


def _render_snapshot(snapshot, region, *, scale=1., target=None):
    """Render a frozen document at one sample per canvas pixel, without solo UI.

    An explicit output rectangle may extend beyond chapter bounds. Page masks
    still apply, exactly as in the existing cropped export path; view overflow
    and editing decorations are excluded from the output.
    """
    document = replace(snapshot.document, overflow=0., underlay=('', 0.), live_preview=False, contact_only=False)
    state = dict(snapshot.state, _solo_suspended=True, _render_base_alpha=False,
                 _show_on_top_phase=None, _text_editing=False)
    chapter = snapshot.chapter
    guides = [obj for obj in chapter.objects.values()
              if getattr(obj, 'reference_role', '') == 'uv_map' and obj.visible]
    if target is None and guides:
        chapter = copy.copy(chapter)
        chapter.objects = dict(chapter.objects)
        for obj in guides:
            hidden = copy.copy(obj)
            hidden.visible = False
            chapter.objects[obj.object_id] = hidden
    frozen = replace(snapshot, document=document, state=state, chapter=chapter)
    backend = DetachedSceneBackend(frozen)
    backend.artwork_scale = 1.
    backend.scene._interactive_render = False
    backend.scene._exact_reference_render = True
    service = DocumentRenderService(backend)
    service.projection.revision = document.revision
    request = RenderRequest(region, scale, (max(1, math.ceil(region[2]*scale)),
                                           max(1, math.ceil(region[3]*scale))),
                            ('document-output', region), document.revision,
                            quality=RenderQuality.EXACT, clip_document=False, target=target)
    try:
        result = service.render_region(document, request)
        if not result.exact:
            raise RuntimeError(result.error or f'Output was not completed: {result.status.value}')
        image = cpu_image(result.image)
        if image is not result.image:
            result.image.release()
        return image
    finally:
        backend.close()


def entity_crop(snapshot, kind, identifier, maximum=1024, *, cancelled=None):
    """Capture isolated entity artwork without borrowing an editor's state."""
    bounds = entity_visual_bounds(snapshot.chapter, snapshot.tiles, kind, identifier)
    if bounds.isEmpty():
        return QImage()
    scale = min(1., maximum/max(1., bounds.width()), maximum/max(1., bounds.height()))
    return render_snapshot(snapshot, tuple(bounds.getRect()), scale=scale,
                           target=(kind, identifier), cancelled=cancelled)


def asset_thumbnail(snapshot, manifest, size=256, padding=12, *, cancelled=None):
    """Render native entity material, crop alpha, and apply display once."""
    stop = cancelled.is_set if cancelled is not None else lambda: False
    try:
        with RENDER_ADMISSION.reserve('thumbnail', snapshot_working_bytes(snapshot), priority=2, cancelled=stop):
            return _asset_thumbnail(snapshot, manifest, size, padding, cancelled)
    except WorkCancelled as error:
        raise OutputCancelled() from error


def _asset_thumbnail(snapshot, manifest, size, padding, cancelled):
    bounds = entity_visual_bounds(snapshot.chapter, snapshot.tiles, manifest.root_kind, manifest.root_id)
    contract = snapshot.document.pixel_contract
    result = QImage(size, size, contract.image_format)
    result.fill(Qt.transparent)
    if bounds.isEmpty():
        with pixel_scope(contract, environment=snapshot.pixel_environment):
            return display_image(result, contract)
    crop = entity_crop(snapshot, manifest.root_kind, manifest.root_id, cancelled=cancelled)
    alpha = premultiplied_pixels(crop)[..., 3]
    rows, columns = np.flatnonzero(np.any(alpha > 0., axis=1)), np.flatnonzero(np.any(alpha > 0., axis=0))
    if rows.size:
        source = QRect(int(columns[0]), int(rows[0]), int(columns[-1]-columns[0]+1),
                        int(rows[-1]-rows[0]+1))
    else:
        source = crop.rect()
    available = max(1, size-padding*2)
    ratio = min(available/max(1, source.width()), available/max(1, source.height()))
    width, height = max(1, round(source.width()*ratio)), max(1, round(source.height()*ratio))
    target = QRect((size-width)//2, (size-height)//2, width, height)
    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(target, crop, source)
    finally:
        painter.end()
    with pixel_scope(contract, environment=snapshot.pixel_environment):
        return display_image(result, contract)


def write_export(snapshot, destination, image_format='PNG', *, region=None, cancelled=None):
    """Evaluate/encode off the owner thread, then atomically publish one file."""
    region = tuple(region or (0., 0., snapshot.document.width, snapshot.document.height))
    estimate = snapshot_working_bytes(snapshot) + int(region[2])*int(region[3])*64
    stop = cancelled.is_set if cancelled is not None else lambda: False
    try:
        with RENDER_ADMISSION.reserve('export', estimate, priority=1, cancelled=stop):
            return _write_export(snapshot, destination, image_format, region=region, cancelled=cancelled)
    except WorkCancelled as error:
        raise OutputCancelled() from error


def _write_export(snapshot, destination, image_format, *, region, cancelled):
    destination = Path(destination)
    def check():
        if cancelled is not None and cancelled.is_set():
            raise OutputCancelled()
    check()
    image = render_snapshot(snapshot, region, cancelled=cancelled)
    check()
    contract = snapshot.document.pixel_contract
    with pixel_scope(contract, environment=snapshot.pixel_environment):
        image = export_image(image, contract, high_precision=image_format in ('PNG', 'TIFF'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'.{destination.name}.{uuid.uuid4().hex}.tmp')
    try:
        if image_format == 'PNG' or contract.floating and image_format == 'TIFF':
            if not image.save(str(temporary), image_format):
                raise OSError(f'Qt could not encode the {image_format}')
        else:
            from PIL import Image
            rgba = image.convertToFormat(QImage.Format_RGBA8888)
            output = Image.frombytes('RGBA', (rgba.width(), rgba.height()), bytes(rgba.constBits()),
                                     'raw', 'RGBA', rgba.bytesPerLine())
            if image_format == 'JPEG':
                background = Image.new('RGB', output.size, 'white')
                background.paste(output, mask=output.getchannel('A'))
                output = background
            profile = bytes(image.colorSpace().iccProfile())
            settings = {'icc_profile': profile} if profile and image_format in ('JPEG', 'TIFF', 'WEBP') else {}
            output.save(temporary, format=image_format, **settings)
        check()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
