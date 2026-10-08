"""Native fit-parent pixels survive transferring bounded detached scene caches.

Review artifact only. This uses the actual incoming snapshot/backend, rather
than reconstructing the former Canvas/EffectJobs source capture architecture.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPolygonF

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject, HueSaturationLightnessModifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, working_image
from comic_editor.render.scene import DetachedSceneBackend, SceneSnapshotCompiler
from comic_editor.render.service import DocumentRenderService, RenderRequest, RenderQuality
from comic_editor.ui.canvas import CanvasWidget


POLICIES = [LEGACY_PIXELS, replace(FLOAT_PIXELS, precision='float16'), FLOAT_PIXELS]


def native(image):
    return image.size(), image.format(), image.bytesPerLine(), bytes(image.constBits())


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    owner = CanvasWidget(EditorSettings(canvas_renderer='raster', grid_overlay_visible=False))
    chapter = ChapterDocument(width=64, height=64, document_kind='asset', background='#00000000')
    page = chapter.add_page('Native grid', BoundGeometry.rectangle(0, 0, 64, 64))
    page.fill_color, page.border_width = None, 0
    owner.set_document(chapter, TileStore())
    owner.setUpdatesEnabled(False)
    yield owner
    owner._scene_controller.reset()
    owner._scene_controller.scheduler.close()
    owner._effect_jobs.cancel()
    owner._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    owner.close()
    owner.deleteLater()


def freeze(canvas, compiler, qapp):
    capture = compiler.capture(canvas, canvas._render_document_state())
    deadline = time.monotonic()+10
    while not capture.advance(.001):
        assert time.monotonic() < deadline
        qapp.processEvents()
    assert not capture.stale and capture.result is not None
    return capture.result


def render(snapshot, object_id, cache_state=None):
    backend = DetachedSceneBackend(snapshot)
    try:
        if cache_state is not None:
            backend.scene.adopt_cache_state(cache_state)
        service = DocumentRenderService(backend)
        service.projection.revision = snapshot.document.revision
        request = RenderRequest((0., 0., 64., 64.), 1., (64, 64),
            ('native-fit-parent',), snapshot.document.revision, quality=RenderQuality.EXACT)
        result = service.render_region(snapshot.document, request)
        assert result.exact and result.image.format() == snapshot.document.pixel_contract.image_format
        scene = backend.scene
        obj = scene.chapter.objects[object_id]
        quad = tuple(scene._image_model_local_quad(obj))
        signature = scene._modifier_object_signature(obj)
        frame = QPolygonF([QPointF(*point) for point in quad]).boundingRect().toAlignedRect()
        return native(result.image), scene.cache_state(), quad, signature, tuple(frame.getRect())
    finally:
        backend.close()


@pytest.mark.parametrize('contract', POLICIES, ids=['rgba8', 'float16', 'float32'])
@pytest.mark.parametrize('compound', [False, True], ids=['parent-bound', 'compound-child-bound'])
def test_full_unrounded_fit_quad_rejects_old_native_source_after_same_aligned_frame_edit(canvas, qapp, contract, compound):
    chapter = canvas.chapter
    chapter.pixel_contract = contract
    parent = chapter.add_layer(chapter.root_page_ids[0], 'Fitted source',
        BoundGeometry.rectangle(10.2, 12.3, 35.4, 31.3))
    parent.fill_color, parent.border_width = None, 0
    edited = parent
    if compound:
        parent.compound_enabled = True
        # The compound union is controlled by its larger child. Both old and
        # new effective bounds still align to the same integer rectangle.
        parent.bound = BoundGeometry.rectangle(20., 20., 4., 4.)
        edited = chapter.add_layer(parent.layer_id, 'Compound geometry',
            BoundGeometry.rectangle(10.2, 12.3, 35.4, 31.3))
        edited.fill_color, edited.border_width = None, 0
    obj = chapter.add_object(parent.layer_id, ImageObject(pixel_width=7, pixel_height=5,
        placement_mode='fit_parent', fit_mode='stretch'))
    yy, xx = np.mgrid[:5, :7]
    alpha = (.2+(xx+2*yy)%7/10).astype(np.float32)
    values = np.stack((xx/8, yy/6, ((xx+yy)%5)/6, np.ones_like(xx)), axis=-1).astype(np.float32)
    values *= alpha[..., None]
    source = working_image(values, contract)
    canvas.images.put_decoded(obj.object_id, 'fitted-owned-native', b'', source)
    original_source = native(canvas.images.native_image(obj.object_id))
    chapter.add_modifier(HueSaturationLightnessModifier(), [('object', obj.object_id)])
    # This fixture assembled records directly after set_document. Publish that
    # setup once so the ordinary reverse index owns the complete subtree before
    # the first cached render; the measured edit below is the narrow bound edit.
    canvas._publish_change_set(ChangeSet(conservative=True, label='Fixture subtree'))
    compiler = SceneSnapshotCompiler()
    first = freeze(canvas, compiler, qapp)
    with ThreadPoolExecutor(max_workers=1) as worker:
        before, cached, old_quad, old_signature, old_frame = worker.submit(render, first, obj.object_id).result(timeout=20)
        assert cached['_modifier_source_cache'], 'Fixture must populate the real source cache'
        assert cached['_modifier_render_cache'], 'Fixture must populate the real stage cache'
        edited.bound = BoundGeometry.rectangle(10.35, 12.45, 35.1, 31.0)
        change = ChangeSet((EntityChange(('layer', edited.layer_id), frozenset({'bound'})),))
        canvas._publish_change_set(change)
        compiler.invalidate(change)
        current = freeze(canvas, compiler, qapp)
        assert current.chapter.layers[edited.layer_id].bound == edited.bound
        warm, _cache, new_quad, new_signature, new_frame = worker.submit(render, current,
            obj.object_id, cached).result(timeout=20)
        fresh, *_ = worker.submit(render, current, obj.object_id).result(timeout=20)
        old_again, *_ = worker.submit(render, first, obj.object_id).result(timeout=20)
    assert old_frame == new_frame, 'The regression requires the same aligned native source frame'
    assert old_quad != new_quad and old_signature != new_signature
    assert warm == fresh and warm != before, 'The visible change must equal a cache-free current native render'
    assert old_again == before, 'An older immutable snapshot keeps its own pixels'
    assert native(canvas.images.native_image(obj.object_id)) == original_source
    assert chapter.pixel_contract == contract and obj.placement_mode == 'fit_parent'
