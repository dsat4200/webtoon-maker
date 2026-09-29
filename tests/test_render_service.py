"""Renderer contracts exercised without a canvas or an editor backend."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace

import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainterPath

from comic_editor.render.projection import DocumentProjection, ProjectionRequest
from comic_editor.render.service import (
    CaptureState, DocumentRenderService, RenderDocument, RenderFailed, RenderPending,
    RenderQuality, RenderRequest, RenderResult, RenderStatus, TileBatchPolicy,
)


class SyntheticScene:
    """Only scene operations; no widget, camera, input, or mutable UI flags."""

    def __init__(self):
        self.identity = ("document",)
        self.configuration = ("ordinary-view",)
        self.captures = []
        self.paint_hook = None
        self.provisional = False
        self.channel = "canvas"
        self.events = []

    def matches(self, document):
        return document.identity == self.identity and document.configuration == self.configuration

    @contextmanager
    def capture(self, document, request, effect_region):
        self.captures.append((document, request, QRectF(effect_region)))
        state = CaptureState(self.provisional)
        yield state

    def paint(self, painter, visible, *, phase=None, page_contents_only=False):
        self.events.append((self.channel, QRectF(visible), phase, page_contents_only))
        if self.paint_hook is not None:
            self.paint_hook()
        if phase != "top":
            painter.fillRect(QRectF(-8, 4, 24, 8), QColor("red"))
        if phase != "base":
            painter.fillRect(QRectF(10, 6, 12, 6), QColor("blue"))

    def page_area(self):
        area = QPainterPath()
        area.addRect(QRectF(0, 0, 8, 16))
        return area

    @contextmanager
    def overflow_channel(self):
        previous, self.channel = self.channel, "overflow"
        try:
            yield
        finally:
            self.channel = previous


def setup_service(**metadata):
    backend = SyntheticScene()
    service = DocumentRenderService(backend)
    service.configure((*backend.configuration, None), document=backend.identity)
    document = RenderDocument(backend.identity, backend.configuration,
                              service.projection.revision, 32, 16, "#FFFFFFFF", **metadata)
    return service, backend, document


def request(document, **values):
    return RenderRequest((0., 0., 32., 16.), 1., (32, 16), ("output",), document.revision, **values)


@pytest.mark.parametrize("phase", [None, "base", "top"])
def test_explicit_output_phase_and_transparent_chapter_clip(phase):
    service, _, document = setup_service()
    region = RenderRequest((-8., 0., 40., 16.), 1., (40, 16), ("crop",), document.revision, phase=phase)
    result = service.render_region(document, region)
    assert result.exact and result.document.revision == document.revision
    assert result.image.pixelColor(0, 8).alpha() == 0
    assert result.image.pixelColor(10, 8) == QColor("red" if phase != "top" else "transparent")
    assert result.image.pixelColor(20, 8) == QColor("red" if phase == "base" else "blue")
    assert result.image.pixelColor(36, 2) == QColor("transparent" if phase == "top" else "white")


def test_requested_subset_keeps_capture_origin_and_effect_source_region():
    service, backend, document = setup_service()
    value = request(document, requested_region=(10., 6., 4., 4.))
    result = service.render_region(document, value)
    assert result.exact
    assert backend.captures[0][2] == document.bounds
    assert backend.events[0][1] == QRectF(10, 6, 4, 4)
    assert result.image.pixelColor(12, 8) == QColor("blue")


def test_overflow_is_separate_and_obeys_chapter_clip():
    service, backend, document = setup_service(overflow=.5)
    result = service.render_region(document, request(document))
    assert result.exact
    assert [event[0] for event in backend.events] == ["overflow", "canvas"]
    assert backend.events[0][3] and not backend.events[1][3]
    assert backend.channel == "canvas"


@pytest.mark.parametrize("failure,status", [(RenderPending(), RenderStatus.PENDING),
                                          (RenderFailed("failed filter"), RenderStatus.FAILED)])
def test_unfinished_dependencies_never_return_publishable_pixels(failure, status):
    service, backend, document = setup_service()
    def fail():
        raise failure
    backend.paint_hook = fail
    result = service.render_region(document, request(document))
    assert result.status is status and result.image.isNull() and not result.exact
    assert result.error == ("failed filter" if status is RenderStatus.FAILED else "")


def test_provisional_pixels_cannot_become_exact_tiles():
    service, backend, document = setup_service()
    backend.provisional = True
    result = service.render_region(document, request(document, quality=RenderQuality.INTERACTIVE))
    assert result.status is RenderStatus.PROVISIONAL and result.image.isNull()


@pytest.mark.parametrize("change", ["revision", "document", "view"])
def test_changes_during_capture_reject_finished_pixels(change):
    service, backend, document = setup_service()
    def mutate():
        if change == "revision":
            service.invalidate(QRectF(0, 0, 1, 1))
        elif change == "document":
            backend.identity = ("replacement",)
        else:
            backend.configuration = ("solo-view",)
    backend.paint_hook = mutate
    result = service.render_region(document, request(document))
    assert result.status is RenderStatus.STALE and result.image.isNull()
    assert result.request.revision == document.revision


def test_old_revision_is_rejected_before_scene_work():
    service, backend, document = setup_service()
    service.invalidate()
    result = service.render_region(document, request(document))
    assert result.status is RenderStatus.STALE and not backend.captures


@pytest.mark.parametrize("change", ["invalidate", "configure", "replace"])
@pytest.mark.parametrize("batched", [False, True])
def test_reentrant_cache_changes_cannot_relabel_old_pixels(change, batched):
    projection = DocumentProjection(tile_size=8)
    projection.configure(("view",), document=("doc",))
    requests = projection.requests(QRectF(0, 0, 8, 8), 1.)
    def change_cache():
        if change == "invalidate":
            projection.invalidate()
        else:
            projection.configure(("new-view",), document=("new-doc" if change == "replace" else "doc",))
        image = QImage(12, 12, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.red)
        return image, True
    if batched:
        tiles = projection.collect(requests, lambda _: pytest.fail("Unexpected retry"),
                                   render_many=lambda _: {requests[0].address: change_cache()})
    else:
        tiles = projection.collect(requests, lambda _: change_cache())
    assert tiles == [] and not projection.tiles


@pytest.mark.parametrize("scale", [1., 2.])
def test_fixed_blocks_have_identical_tile_pixels_to_region_rendering(scale):
    service, _, document = setup_service()
    document = replace(document, width=2048, height=1024)
    requests = service.projection.requests(QRectF(-260, -10, 1540, 300), scale)
    batch = service.render_tiles(document, requests, TileBatchPolicy((1200., 100.)))
    assert batch.tiles and not batch.pending and not batch.error and not batch.yielded
    for tile in requests:
        expected_request = RenderRequest(tuple(tile.capture_rect.getRect()), tile.scale,
            (tile.pixel_size, tile.pixel_size), ("oracle",), document.revision)
        expected = service.render_region(document, expected_request)
        assert batch.tiles[tile.address] == (expected.image, True)


def test_priority_deadline_and_remaining_regions_are_explicit():
    service, backend, document = setup_service()
    requests = service.projection.requests(QRectF(0, 0, 2048, 256), 1.)
    batch = service.render_tiles(document, requests, TileBatchPolicy((1400., 100.), deadline=0.))
    assert batch.blocks_started == 1 and batch.yielded
    assert backend.captures[0][1].key == (0, 1, 0)
    assert any(exact for _, exact in batch.tiles.values())
    assert any(not exact for _, exact in batch.tiles.values())
    backend.captures.clear()
    stopped = service.render_tiles(document, requests,
                                   TileBatchPolicy((1400., 100.), deadline=0., blocks_started=1))
    assert stopped.yielded and not backend.captures


def test_wrong_revision_callback_result_is_not_admitted():
    service, _, document = setup_service()
    requests = service.projection.requests(QRectF(0, 0, 256, 256), 1.)
    def wrong(request):
        result = service.render_region(document, request)
        return replace(result, request=replace(request, revision=request.revision + 1))
    batch = service.render_tiles(document, requests, TileBatchPolicy((0., 0.)), capture=wrong)
    assert all(image.isNull() and not exact for image, exact in batch.tiles.values())


def test_service_cache_reuses_camera_requests_and_invalidates_document_edits():
    service, backend, document = setup_service()
    requests = service.projection.requests(document.bounds, 1.)
    first = service.collect_tiles(document, requests, TileBatchPolicy((16., 8.)))
    assert first and all(tile.valid for tile in first)
    captures = len(backend.captures)
    again = service.collect_tiles(document, requests, TileBatchPolicy((20., 10.)))
    assert again == first and len(backend.captures) == captures
    service.invalidate(QRectF(0, 0, 1, 1))
    document = replace(document, revision=service.projection.revision)
    rebuilt = service.collect_tiles(document, requests, TileBatchPolicy((20., 10.)))
    assert rebuilt and len(backend.captures) > captures
    assert all(tile.revision == document.revision for tile in rebuilt)


def test_cached_tiles_cannot_satisfy_a_request_for_an_obsolete_view_or_revision():
    service, backend, document = setup_service()
    requests = service.projection.requests(document.bounds, 1.)
    policy = TileBatchPolicy((16., 8.))
    assert service.collect_tiles(document, requests, policy)
    backend.configuration = ("solo-view",)
    assert service.collect_tiles(document, requests, policy) == []
    backend.configuration = document.configuration
    service.invalidate()
    updated = replace(document, revision=service.projection.revision)
    assert service.collect_tiles(updated, requests, policy)
    assert service.collect_tiles(document, requests, policy) == []


def test_document_metadata_detaches_nested_mutable_view_configuration():
    configuration = ["view", ["solo-object"]]
    document = RenderDocument(["doc"], configuration, 0, 32, 16, "white")
    configuration[1].append("other-object")
    assert document.configuration == ("view", ("solo-object",))


def test_request_detaches_mutable_coordinates_and_exposes_only_copies():
    _, _, document = setup_service()
    coordinates, dimensions = [0., 0., 32., 16.], [32, 16]
    value = RenderRequest(coordinates, 1., dimensions, ["output"], document.revision)
    coordinates[0], dimensions[0] = 100, 1
    bounds = value.bounds
    bounds.translate(500, 500)
    assert value.region == (0., 0., 32., 16.) and value.pixel_size == (32, 16)
    assert value.bounds == QRectF(0, 0, 32, 16)
    with pytest.raises(FrozenInstanceError):
        value.scale = 2.


@pytest.mark.parametrize("values", [dict(scale=0), dict(scale=float("nan")),
    dict(region=(0., 0., -1., 2.)), dict(pixel_size=(0, 16)),
    dict(requested_region=(float("inf"), 0., 1., 1.)), dict(phase="unknown"), dict(quality="exact")])
def test_invalid_requests_fail_before_allocating_or_painting(values):
    _, _, document = setup_service()
    with pytest.raises(ValueError):
        replace(request(document), **values)
