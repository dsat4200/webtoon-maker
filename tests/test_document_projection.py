import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.ui.document_projection import DocumentProjection


def renderer(calls, color="red", exact=True):
    def render(request):
        calls.append(request.address)
        image = QImage(request.pixel_size, request.pixel_size, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(color))
        return image, exact
    return render


def test_pan_reuses_overlap_and_return_trip_without_rendering(qapp):
    projection, calls = DocumentProjection(), []
    render = renderer(calls)
    projection.collect(projection.requests(QRectF(0, 0, 512, 256), 1), render)
    assert len(calls) == 2
    projection.collect(projection.requests(QRectF(128, 0, 512, 256), 1), render)
    assert len(calls) == 3
    projection.collect(projection.requests(QRectF(0, 0, 512, 256), 1), render)
    assert len(calls) == 3


def test_dirty_regions_include_neighbor_gutters_and_offscreen_tiles(qapp):
    projection, calls = DocumentProjection(), []
    requests = projection.requests(QRectF(0, 0, 1024, 256), 1)
    projection.collect(requests, renderer(calls))
    projection.invalidate(QRectF(255, 20, 1, 1))
    assert [tile.valid for tile in projection.tiles.values()] == [False, False, True, True]
    calls.clear()
    projection.collect(requests[2:], renderer(calls))
    assert not calls
    projection.collect(requests[:2], renderer(calls))
    assert len(calls) == 2


def test_unfinished_work_never_replaces_finished_pixels_with_draft(qapp):
    projection, calls = DocumentProjection(), []
    requests = projection.requests(QRectF(0, 0, 256, 256), 1)
    original = projection.collect(requests, renderer(calls))[0]
    projection.invalidate()
    pending = projection.collect(requests, renderer(calls, "blue", exact=False))[0]
    assert pending.image.cacheKey() == original.image.cacheKey()
    assert pending.image.pixelColor(10, 10) == QColor("red")
    assert not pending.valid
    finished = projection.collect(requests, renderer(calls, "blue"))[0]
    assert finished.image.pixelColor(10, 10) == QColor("blue")
    assert finished.valid


def test_cold_draft_is_not_cached_or_presented(qapp):
    projection = DocumentProjection()
    assert not projection.collect(projection.requests(QRectF(0, 0, 256, 256), 1),
                                  renderer([], exact=False))
    assert not projection.tiles


def test_zoom_uses_sufficient_detail_and_reuses_existing_levels(qapp):
    projection, calls = DocumentProjection(), []
    view = QRectF(0, 0, 256, 256)
    for scale, count in ((1., 1), (1.2, 5), (1.24, 5), (1., 5), (.3, 5)):
        requests = projection.requests(view, scale)
        projection.collect(requests, renderer(calls))
        assert len(calls) == count
        assert all(request.scale >= scale for request in requests)


def test_negative_tile_coordinates_and_capture_gutters(qapp):
    requests = DocumentProjection().requests(QRectF(-257, -10, 258, 20), 1)
    assert {(r.address.x, r.address.y) for r in requests} == {
        (-2, -1), (-1, -1), (0, -1), (-2, 0), (-1, 0), (0, 0)}
    assert requests[0].capture_rect == QRectF(-514, -258, 260, 260)
    assert requests[0].source_rect == QRectF(2, 2, 256, 256)


def test_maximum_zoom_on_hidpi_keeps_physical_pixel_detail():
    for ratio in (1., 1.25, 1.5, 2., 3., 4.):
        density = 8. * ratio
        assert DocumentProjection.resolution_scale(density) >= density


def test_cache_budget_is_enforced_without_dropping_frame_results(qapp):
    projection = DocumentProjection(budget=260 * 260 * 4)
    result = projection.collect(projection.requests(QRectF(0, 0, 768, 256), 1), renderer([]))
    assert len(result) == 3
    assert len(projection.tiles) == 1
    assert projection.bytes <= projection.budget


def test_document_switch_cannot_show_previous_document(qapp):
    projection = DocumentProjection()
    projection.configure("one")
    projection.collect(projection.requests(QRectF(0, 0, 256, 256), 1), renderer([]))
    projection.configure("two")
    assert not projection.tiles
    assert projection.bytes == 0


def test_adjacent_missing_tiles_can_share_one_capture(qapp):
    projection, batches = DocumentProjection(), []
    requests = projection.requests(QRectF(0, 0, 512, 512), 1)
    def batch(missing):
        batches.append([request.address for request in missing])
        return {request.address: renderer([])(request) for request in missing}
    def forbidden(request):
        raise AssertionError("A batched tile must not traverse the scene again")
    projection.collect(requests, forbidden, render_many=batch)
    assert len(batches) == 1 and len(batches[0]) == 4
    projection.collect(requests, forbidden, render_many=batch)
    assert len(batches) == 1


def test_temporary_configuration_restores_finished_pixels(qapp):
    projection, calls = DocumentProjection(), []
    requests = projection.requests(QRectF(0, 0, 256, 256), 1)
    projection.configure("normal", document="chapter")
    normal = projection.collect(requests, renderer(calls))[0]
    projection.configure("solo", document="chapter")
    solo = projection.collect(requests, renderer(calls, "blue"))[0]
    assert normal.image != solo.image
    projection.configure("normal", document="chapter")
    restored = projection.collect(requests, renderer(calls))[0]
    assert restored.image.cacheKey() == normal.image.cacheKey()
    assert len(calls) == 2
    assert projection.snapshot()["configurations"] == 2


def test_document_edits_invalidate_inactive_configurations(qapp):
    projection, calls = DocumentProjection(), []
    requests = projection.requests(QRectF(0, 0, 512, 256), 1)
    for config in ("normal", "solo"):
        projection.configure(config, document="chapter")
        projection.collect(requests, renderer(calls))
    projection.invalidate(QRectF(10, 10, 1, 1))
    for config in ("normal", "solo"):
        projection.configure(config, document="chapter")
        assert [tile.valid for tile in projection.tiles.values()] == [False, True]
        projection.collect(requests, renderer(calls, "blue"))
    assert len(calls) == 6
    projection.invalidate()
    projection.configure("normal", document="chapter")
    assert not any(tile.valid for tile in projection.tiles.values())


def test_alternate_configurations_share_one_budget_and_have_bounded_metadata(qapp):
    tile_bytes = 260 * 260 * 4
    projection = DocumentProjection(budget=2 * tile_bytes, configuration_limit=3)
    requests = projection.requests(QRectF(0, 0, 256, 256), 1)
    for index in range(20):
        projection.configure(index, document="chapter")
        projection.collect(requests, renderer([]))
        assert projection.bytes <= projection.budget
        assert projection.snapshot()["configurations"] <= 3
        assert projection.snapshot()["tiles"] <= 2
    projection.configure("normal", document="different chapter")
    assert projection.bytes == 0
    assert projection.snapshot()["tiles"] == 0
    assert projection.snapshot()["configurations"] == 1
