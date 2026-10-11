"""Full native buffers and source ownership for finite filtered feedback."""
from dataclasses import replace
from threading import Event, get_ident
import time

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import (BoundGeometry, ChapterDocument, RasterObject,
    HueSaturationLightnessModifier, OutlineModifier, ToneMask, ParameterMaskBinding,
    BlurModifier)
from comic_editor.core.tiles import TileStore
from comic_editor.core.images import ImageStore
from comic_editor.render.outputs import capture_document
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService, RenderRequest, TileBatchPolicy
from comic_editor.render.projection import ProjectionAddress, ProjectionRequest
from comic_editor.render.filtered_raster_feedback import (FeedbackUnavailable,
    capture_native_feedback_recipe, update_native_feedback_recipe, filtered_feedback_eligible)
from comic_editor.render.filtered_feedback_worker import (FilteredFeedbackWorker,
    capture_filtered_contact_packet)


def native(image):
    return (image.size(), image.format(), image.bytesPerLine(), image.devicePixelRatio(),
        bytes(image.colorSpace().iccProfile()), bytes(image.constBits()))


def ordinary(snapshot, request):
    backend = DetachedSceneBackend(snapshot)
    backend.artwork_scale = 1.
    service = DocumentRenderService(backend)
    service.projection.revision = snapshot.document.revision
    try:
        result = service.render_region(snapshot.document, request)
        assert result.exact, result.error
        return result.image
    finally:
        backend.close()


def fixture(format=QImage.Format_ARGB32_Premultiplied, transparent=True, rotate=True):
    chapter = ChapterDocument(width=512, height=512, document_kind='asset',
        background='#00000000' if transparent else '#fff9f2e9')
    page = chapter.add_page('Page', BoundGeometry.circle(255.5,256.,242.25))
    page.fill_color, page.border_width = '#a0775566', 1.
    layer = chapter.add_layer(page.layer_id, 'Branch', bound=BoundGeometry.rectangle(20.25,30.5,420.75,412.25))
    layer.fill_color, layer.border_width = '#40112233', 2.
    layer.transform_frame = (20.25,30.5,420.75,412.25)
    transform = (QTransform().translate(180.,180.).rotate(-3.).translate(-180.,-180.) if rotate
        else QTransform.fromTranslate(3.25,5.5))
    x,y,w,h = layer.transform_frame
    layer.transform_quad = [transform.map(QPointF(px,py)).toTuple()
        for px,py in ((x,y),(x+w,y),(x+w,y+h),(x,y+h))]
    back = chapter.add_object(layer.layer_id, RasterObject())
    selected = chapter.add_object(layer.layer_id, RasterObject(x=-13.25,y=7.75,opacity=.54,
        interaction_rect=(0.,0.,384.,384.)))
    front = chapter.add_object(layer.layer_id, RasterObject())
    layer.children.reverse()
    tiles, images = TileStore(), ImageStore()
    for obj,color in ((back,QColor('#804466aa')),(selected,QColor('#a06e44a1')),(front,QColor('#5033ee66'))):
        image = QImage(256,256,format)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing,True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(13.25,27.75,231.5,211.25))
        painter.end()
        tiles.set_tile(obj.object_id,(0,0),image)
    mask = ToneMask(name='Stable intensity',saved=True)
    chapter.masks[mask.mask_id] = mask
    tiles.paint_dab(mask.mask_id,QPointF(173.,128.),380.,QColor('#737373'))
    for modifier in (HueSaturationLightnessModifier(hue=40.),
            HueSaturationLightnessModifier(hue=51.,saturation=98.,lightness=-66.,
                parameter_masks={'intensity':ParameterMaskBinding(mask_id=mask.mask_id,
                    black_value=0.,white_value=88.7878787878788)}),
            OutlineModifier(thickness=12.,color='#ffffffff',opacity=100.,antialiasing=True)):
        chapter.add_modifier(modifier,[('object',selected.object_id)])
    baseline = capture_document(chapter,tiles,images)
    request = RenderRequest((0.,0.,512.,512.),1.,(512,512),('ordinary-native-recipe',),0)
    recipe = capture_native_feedback_recipe(baseline,selected.object_id,request)
    return chapter,tiles,images,selected,baseline,request,recipe


def changed_snapshot(chapter,tiles,images,baseline,revision):
    result = capture_document(chapter,tiles,images)
    return replace(result,document=replace(result.document,identity=baseline.document.identity,revision=revision))


@pytest.mark.parametrize('format',[QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied,QImage.Format_RGBA32FPx4_Premultiplied])
@pytest.mark.parametrize('transparent',[False,True])
@pytest.mark.parametrize('rotate',[False,True])
def test_visible_cumulative_contacts_keep_complete_native_buffers_and_owned_basis(qapp,format,transparent,rotate):
    chapter,tiles,images,obj,baseline,request,recipe = fixture(format,transparent,rotate)
    original_source,original_image = native(recipe.source_image),native(recipe.initial)
    assert original_image == native(ordinary(baseline,request))
    previous_source,previous_image = None,recipe.initial
    for revision,(point,erase) in enumerate(((QPointF(137.,113.),False),(QPointF(253.,128.),False),
            (QPointF(173.,244.),False),(QPointF(173.,244.),True)),1):
        tiles.paint_dab(obj.object_id,point,37.5,QColor('#c0997722'),opacity=.73,erase=erase)
        updated = changed_snapshot(chapter,tiles,images,baseline,revision)
        backend = DetachedSceneBackend(updated)
        try:
            mapping = QTransform.fromTranslate(obj.x,obj.y)*backend.scene._drawing_object_transform(obj)*backend.scene.layer_world_transform(obj.parent_layer_id)
            dirty = mapping.mapRect(QRectF(point.x()-20.,point.y()-20.,40.,40.)).adjusted(-18.,-18.,18.,18.)
        finally:
            backend.close()
        actual = update_native_feedback_recipe(recipe,updated,dirty,previous_source=previous_source)
        expected = ordinary(updated,replace(request,revision=revision))
        assert native(actual.image) == native(expected)
        assert native(previous_image) != native(expected)
        assert native(recipe.source_image) == original_source
        assert native(recipe.initial) == original_image
        previous_source,previous_image = actual.source,expected


@pytest.mark.parametrize('change',['identity','configuration','empty','nan','source_frame','previous_format'])
def test_lost_document_demand_or_source_grid_is_rejected(qapp,change):
    chapter,tiles,images,obj,baseline,request,recipe = fixture()
    updated = changed_snapshot(chapter,tiles,images,baseline,1)
    dirty = QRectF(130.,220.,80.,80.)
    previous = None
    if change == 'identity':
        updated = replace(updated,document=replace(updated.document,identity=('another-document',)))
    elif change == 'configuration':
        updated = replace(updated,document=replace(updated.document,configuration=('another-profile',)))
    elif change == 'empty':
        dirty = QRectF()
    elif change == 'nan':
        dirty = QRectF(float('nan'),0.,10.,10.)
    elif change == 'source_frame':
        updated.chapter.objects[obj.object_id].interaction_rect = (0.,0.,500.,500.)
    else:
        previous = recipe.source_image.convertToFormat(QImage.Format_RGBA64_Premultiplied)
    with pytest.raises(FeedbackUnavailable):
        update_native_feedback_recipe(recipe,updated,dirty,previous_source=previous)


@pytest.mark.parametrize('budget',[0,1,3*512*512*4-1,4*1024*1024])
def test_bounded_recipe_declines_insufficient_retained_capacity(qapp,budget):
    _chapter,_tiles,_images,obj,baseline,request,_recipe = fixture()
    with pytest.raises(FeedbackUnavailable):
        capture_native_feedback_recipe(baseline,obj.object_id,request,budget=budget)


def test_prepare_and_update_observe_cancellation_without_modifying_the_basis(qapp):
    _chapter,_tiles,_images,obj,baseline,request,recipe = fixture()
    before = native(recipe.source_image),native(recipe.initial)
    with pytest.raises(FeedbackUnavailable):
        capture_native_feedback_recipe(baseline,obj.object_id,request,cancelled=lambda:True)
    with pytest.raises(FeedbackUnavailable):
        update_native_feedback_recipe(recipe,baseline,QRectF(100.,100.,100.,100.),cancelled=lambda:True)
    assert before == (native(recipe.source_image),native(recipe.initial))


def test_unbounded_filters_retain_the_ordinary_fallback(qapp):
    _chapter,_tiles,_images,obj,baseline,_request,_recipe = fixture()
    baseline.chapter.add_modifier(BlurModifier(strength=12.),[('object',obj.object_id)])
    assert not filtered_feedback_eligible(baseline,obj.object_id)


def test_linked_mask_dependents_retain_the_ordinary_fallback(qapp):
    _chapter,_tiles,_images,obj,baseline,_request,_recipe = fixture()
    next(iter(baseline.chapter.masks.values())).contributors = [('object',obj.object_id)]
    assert not filtered_feedback_eligible(baseline,obj.object_id)


def packet(tiles,obj,sequence,revision,dirty=None):
    owner = tiles._tiles[obj.object_id]
    generations = {key:owner.version(key) for key in set(owner.entries)|{(0,0)}}
    return capture_filtered_contact_packet(tiles,obj.object_id,generations,
        QRectF(80.,80.,220.,220.) if dirty is None else dirty,obj.interaction_rect,sequence,revision)


def test_packet_owns_resident_pixels_and_refuses_cold_decode_and_tombstones(qapp,monkeypatch):
    _chapter,tiles,_images,obj,_baseline,_request,_recipe = fixture()
    owner = tiles._tiles[obj.object_id]
    captured = packet(tiles,obj,1,1)
    pixels = native(captured.values[0][2])
    tiles.paint_dab(obj.object_id,QPointF(137.,113.),37.5,QColor('#c0997722'))
    assert native(captured.values[0][2]) == pixels
    entry = owner.residency.entries.pop((owner,(0,0)))
    with pytest.raises(FeedbackUnavailable,match='decode'):
        packet(tiles,obj,2,2)
    owner.residency.entries[(owner,(0,0))] = entry
    del owner[(0,0)]
    tombstone = packet(tiles,obj,3,3)
    assert tombstone.values[0][2] is None


def drain(worker):
    results = []
    deadline = time.monotonic()+10.
    while worker.busy and time.monotonic()<deadline:
        completion = worker.poll()
        if completion is not None:
            results.append(completion)
        time.sleep(.001)
    assert not worker.busy,worker.error
    assert not worker.error,worker.error
    return results


def test_serial_worker_coalesces_owned_updates_and_leaves_original_snapshot_unchanged(qapp):
    chapter,tiles,images,obj,baseline,request,recipe = fixture()
    original = native(baseline.tiles._tiles[obj.object_id][(0,0)])
    notifications = []
    worker = FilteredFeedbackWorker(baseline,obj.object_id,(recipe,),notify=lambda:notifications.append(get_ident()))
    entered,release = Event(),Event()
    evaluate = worker._evaluate
    def held(packet):
        entered.set()
        assert release.wait(10.)
        return evaluate(packet)
    worker._evaluate = held
    try:
        tiles.paint_dab(obj.object_id,QPointF(137.,113.),37.5,QColor('#c0997722'))
        assert worker.submit(packet(tiles,obj,1,1))
        deadline = time.monotonic()+10.
        while not entered.is_set() and time.monotonic()<deadline:
            worker.poll()
            time.sleep(.001)
        assert entered.is_set()
        for sequence,point in ((2,QPointF(173.,244.)),(3,QPointF(173.,244.))):
            tiles.paint_dab(obj.object_id,point,37.5,QColor('#c0997722'),erase=sequence==3)
            assert worker.submit(packet(tiles,obj,sequence,sequence,QRectF(80.,80.,240.,240.)))
        assert worker.pending.sequence == 3
        release.set()
        results = drain(worker)
        assert [result.sequence for result in results] == [1,3]
        expected = ordinary(changed_snapshot(chapter,tiles,images,baseline,3),replace(request,revision=3))
        assert native(results[-1].updates[0][1].image) == native(expected)
        assert native(baseline.tiles._tiles[obj.object_id][(0,0)]) == original
        assert worker.worker_threads and get_ident() not in worker.worker_threads
        assert notifications and get_ident() not in notifications
        assert worker.backend.scene._persistent_render_cache is None
    finally:
        release.set()
        worker.close()
        worker.executor.shutdown(wait=True)
    assert worker.owner is None and worker.backend is None and not worker.progress and not worker.recipes


def test_worker_packet_budget_retires_instead_of_waiting_or_dropping_source_pixels(qapp):
    _chapter,tiles,_images,obj,baseline,_request,recipe = fixture()
    worker = FilteredFeedbackWorker(baseline,obj.object_id,(recipe,),packet_budget=1)
    assert not worker.submit(packet(tiles,obj,1,1))
    worker.executor.shutdown(wait=True)
    assert worker.stopped.is_set() and 'budget' in worker.error
    assert not worker.submit(packet(tiles,obj,2,2))


def test_worker_adopts_completed_capture_pins_without_relinking_cold_sources(qapp,tmp_path,monkeypatch):
    from comic_editor.core.tile_backing import SnapshotBacking, prefetch_pins
    _chapter,tiles,_images,obj,baseline,_request,recipe = fixture()
    image = QImage(256,256,QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor('#c0773399'))
    path = tmp_path/'cold.png'
    assert image.save(str(path))
    cold = baseline.tiles._object_tiles('unvisited-source')
    cold.entries[(9,9)] = path
    job = prefetch_pins(SnapshotBacking(),(('unvisited-source',(9,9),(0,0),path),))
    job.result()
    baseline = replace(baseline,pin_jobs=(job,))
    def forbidden(*_args,**_kwargs):
        pytest.fail('A completed scene capture pin was unnecessarily linked again')
    monkeypatch.setattr(SnapshotBacking,'pin',forbidden)
    worker = FilteredFeedbackWorker(baseline,obj.object_id,(recipe,))
    try:
        tiles.paint_dab(obj.object_id,QPointF(137.,113.),37.5,QColor('#c0997722'))
        assert worker.submit(packet(tiles,obj,1,1))
        assert drain(worker)
        original_pin = job.ready_pin('unvisited-source',(9,9))
        fork = worker.owner.tiles._tiles['unvisited-source']
        assert fork.snapshot_pins[(9,9)] is original_pin
        path.unlink()
        expected = QImage(str(original_pin.path))
        assert native(fork[(9,9)]) == native(expected)
        assert fork is not cold
        assert worker.owner.tiles.residency is not baseline.tiles.residency
    finally:
        worker.close()
        worker.executor.shutdown(wait=True)


@pytest.mark.parametrize('format',[QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied,QImage.Format_RGBA32FPx4_Premultiplied])
def test_worker_fork_reuses_ready_native_originals_with_private_residency(qapp,tmp_path,format):
    _chapter,tiles,_images,obj,baseline,_request,recipe = fixture(format)
    image = QImage(256,256,QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor('#c0773399'))
    path = tmp_path/'ready.png'
    assert image.save(str(path))
    original = baseline.tiles._object_tiles('ready-source')
    original.entries[(9,9)] = path
    expected = native(original[(9,9)])
    # A ready Path and a ready frozen/editable source both retain original bits.
    selected_before = native(baseline.tiles.tile(obj.object_id,(0,0)))
    worker = FilteredFeedbackWorker(baseline,obj.object_id,(recipe,))
    try:
        drain(worker)
        fork = worker.owner.tiles._tiles['ready-source']
        residency = worker.owner.tiles.residency
        assert residency is not baseline.tiles.residency
        assert (fork,(9,9)) in residency.entries
        assert residency.decodes == 0
        path.unlink()
        assert native(fork[(9,9)]) == expected
        assert native(worker.owner.tiles.tile(obj.object_id,(0,0))) == selected_before
        assert residency.decodes == 0
        assert fork.snapshot_pins[(9,9)] is original.snapshot_pins[(9,9)]
        # A later packet owns its edited revision, not the ready original.
        tiles.paint_dab(obj.object_id,QPointF(137.,113.),37.5,QColor('#c0997722'))
        current = native(tiles.tile(obj.object_id,(0,0)))
        assert worker.submit(packet(tiles,obj,1,1))
        assert drain(worker)
        assert native(worker.owner.tiles.tile(obj.object_id,(0,0))) == current
        assert native(baseline.tiles.tile(obj.object_id,(0,0))) == selected_before
        assert baseline.tiles.residency is not residency
        assert residency.bytes <= residency.budget
    finally:
        worker.close()
        worker.executor.shutdown(wait=True)


@pytest.mark.parametrize('retire',[False,True])
def test_source_preparation_never_blocks_submission_poll_or_retirement(qapp,monkeypatch,retire):
    _chapter,tiles,_images,obj,baseline,_request,recipe = fixture()
    entered,release = Event(),Event()
    original = FilteredFeedbackWorker._fork_owner
    def held(self):
        entered.set()
        assert release.wait(5.)
        return original(self)
    monkeypatch.setattr(FilteredFeedbackWorker,'_fork_owner',held)
    worker = FilteredFeedbackWorker(baseline,obj.object_id,(recipe,))
    try:
        assert entered.wait(1.)
        assert worker.busy and worker.owner is None
        tiles.paint_dab(obj.object_id,QPointF(137.,113.),37.5,QColor('#c0997722'))
        assert worker.submit(packet(tiles,obj,1,1))
        assert worker.poll() is None
        assert worker.pending.sequence == 1 and worker.future is None
        if retire:
            worker.close()
            assert worker.stopped.is_set() and worker.pending is None
        release.set()
        if not retire:
            assert [value.sequence for value in drain(worker)] == [1]
            assert worker.owner is not None
    finally:
        release.set()
        worker.close()
        worker.executor.shutdown(wait=True)
    assert worker.owner is None and worker.backend is None and worker.preparation is None


@pytest.mark.parametrize('format',[QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied,QImage.Format_RGBA32FPx4_Premultiplied])
@pytest.mark.parametrize('rotate',[False,True])
def test_recipe_matches_each_normal_projection_tile_with_partial_block_demand(qapp,format,rotate):
    chapter,tiles,images,obj,_baseline,_request,_recipe = fixture(format,True,rotate)
    chapter.width = chapter.height = 1536
    page = chapter.layers[chapter.root_page_ids[0]]
    page.bound = BoundGeometry.circle(768.,768.,754.)
    branch = chapter.layers[obj.parent_layer_id]
    branch.bound = BoundGeometry.rectangle(20.,30.,1400.,1400.)
    obj.interaction_rect = (0.,0.,1216.,1216.)
    baseline = capture_document(chapter,tiles,images)
    requests = [ProjectionRequest(ProjectionAddress(0,x,y)) for y in range(3) for x in range(3)]
    requested = QRectF()
    for projection in requests:
        requested = requested.united(projection.capture_rect)
    full = RenderRequest((-2.,-2.,1028.,1028.),1.,(1028,1028),(0,0,0),0,tuple(requested.getRect()))
    recipe = capture_native_feedback_recipe(baseline,obj.object_id,full)
    previous = None
    for revision,(point,erase) in enumerate(((QPointF(573.,544.),False),(QPointF(573.,544.),True)),1):
        tiles.paint_dab(obj.object_id,point,37.5,QColor('#c0997722'),erase=erase)
        updated = changed_snapshot(chapter,tiles,images,baseline,revision)
        mapping_backend = DetachedSceneBackend(updated)
        try:
            mapping = QTransform.fromTranslate(obj.x,obj.y)*mapping_backend.scene._drawing_object_transform(obj)*mapping_backend.scene.layer_world_transform(obj.parent_layer_id)
            dirty = mapping.mapRect(QRectF(point.x()-20.,point.y()-20.,40.,40.)).adjusted(-18.,-18.,18.,18.)
        finally:
            mapping_backend.close()
        actual = update_native_feedback_recipe(recipe,updated,dirty,previous_source=previous)
        ordinary_backend = DetachedSceneBackend(updated)
        service = DocumentRenderService(ordinary_backend)
        service.projection.revision = revision
        try:
            expected = service.render_tiles(updated.document,requests,TileBatchPolicy((384.,384.)))
            assert not expected.error and all(exact for image,exact in expected.tiles.values())
            for projection in requests:
                rectangle = projection.capture_rect.translated(-full.bounds.topLeft()).toAlignedRect()
                assert native(actual.image.copy(rectangle)) == native(expected.tiles[projection.address][0])
        finally:
            ordinary_backend.close()
        previous = actual.source
