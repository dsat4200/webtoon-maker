"""Exact frame progress survives tile/worker alias eviction within one pool."""
from collections import Counter

import numpy as np
import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QColorSpace, QImage, QPainter

from comic_editor.render.service import RenderPending
from comic_editor.render.tile_graph import TileGraph, TileNode, tiles
from comic_editor.ui.async_projection import projection_result_or_pending
from test_effect_job_retention import owner


def native_image(width, height, image_format=QImage.Format_ARGB32_Premultiplied):
    result = QImage(width, height, image_format)
    if image_format in (QImage.Format_RGBA16FPx4_Premultiplied,
                        QImage.Format_RGBA32FPx4_Premultiplied):
        dtype = np.float16 if image_format == QImage.Format_RGBA16FPx4_Premultiplied else np.float32
        values = np.frombuffer(result.bits(), dtype=dtype).reshape(height, width, 4)
        yy, xx = np.mgrid[:height, :width]
        values[..., 0] = 1.25 + xx / width
        values[..., 1] = -.125 + yy / height
        values[..., 2] = (xx % 5) / 5
        values[..., 3] = ((xx + yy) % 11) / 10
    else:
        result.fill(QColor('#8048a080'))
        for y in range(height):
            for x in range(width):
                result.setPixelColor(x, y, QColor(x * 17 % 256, y * 23 % 256,
                                                  (x + y) * 13 % 256, (x + y) * 7 % 256))
    return result


def bits(image):
    return bytes(image.constBits())


def finish(jobs):
    for job in jobs.running_jobs:
        job[3].result(timeout=5)
    jobs.poll()


def graph_backend(owner, image_format=QImage.Format_ARGB32_Premultiplied, *, store=True):
    jobs = owner._effect_jobs
    jobs.retained_limit = 5  # The dependency needs 80 native tiles.
    jobs.retained_budget = 512 * 1024
    source = native_image(73, 57, image_format)
    frame = QRectF(-13, -9, source.width(), source.height())
    context = ['current-source', 'chapter', 'history', 'pixel-contract']
    calls = Counter()

    def get(local_owner, key):
        scope = ('assembly-test', local_owner)
        entry = jobs.retained_get(scope, key)
        return entry[0] if entry is not None else projection_result_or_pending(owner, scope, key)

    def put(local_owner, key, image):
        if jobs.retained_put(('assembly-test', local_owner), key, image, shared=True):
            jobs.retained_consume(image)

    def capture(rect):
        crop = QRectF(rect)
        crop.translate(-frame.topLeft())
        return source.copy(crop.toAlignedRect())

    nodes = [TileNode(frame, ('source', tuple(context)), None, None, format=image_format)]
    for index in range(1, 4):
        def evaluate(output, incoming, needed, stage=index):
            address = (int(output.x() // 8), int(output.y() // 8))
            scope = ('assembly-test', (stage, address))
            key = ('effect-tile', nodes[stage].identity, tuple(output.getRect()))
            cached = projection_result_or_pending(owner, scope, key)
            if cached is not None:
                return cached
            calls[stage, address] += 1
            snapshot = QImage(incoming)
            rect = QRectF(output)
            rect.translate(-needed.topLeft())
            jobs.request(scope, key, lambda _, snapshot=snapshot, rect=rect: snapshot.copy(rect.toAlignedRect()),
                         int(snapshot.sizeInBytes()) * 2, require_exact=True, allow_oversized=True)
            raise RenderPending('detached exact tile')
        nodes.append(TileNode(frame, ('warp', index, tuple(context)), lambda _: frame, evaluate,
                              format=image_format))

    def graph():
        regions = jobs.private_regions(tuple(context), lambda: tuple(context)) if store else None
        return TileGraph(nodes, capture, get, put, tile_size=8, region_store=regions)
    return jobs, source, frame, context, calls, graph


@pytest.mark.parametrize('image_format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
def test_dependency_wider_than_record_limit_converges_without_repeating_workers(owner, image_format):
    jobs, source, frame, context, calls, graph = graph_backend(owner, image_format)
    tile_count = len(list(tiles(frame, frame, 8)))
    for _ in range(tile_count * 3 + 4):
        try:
            actual, region = graph().output(frame)
            break
        except RenderPending:
            finish(jobs)
        assert jobs.retained_bytes <= jobs.retained_budget
        assert len(jobs.retained) + len(jobs._regions) <= jobs.retained_limit
    else:
        pytest.fail('Retained native assembly did not make monotonic progress')
    assert region == frame
    # The unchanged pure evaluator is the native QPainter rounding oracle.
    oracle = TileGraph([TileNode(frame, ('source',), None, None, format=image_format)],
        lambda rect: source.copy(rect.translated(-frame.topLeft()).toAlignedRect()),
        lambda *_: None, lambda *_: None, tile_size=8).output(frame)[0]
    assert actual.format() == image_format and bits(actual) == bits(oracle)
    assert sum(calls.values()) == tile_count * 3
    assert max(calls.values()) == 1
    assert jobs.retained_bytes <= jobs.retained_budget
    assert all(key[0] != 'tile-region-assembly' for key in owner.results)


def test_legacy_fresh_graph_does_repeat_evicted_native_work(owner):
    jobs, source, frame, context, calls, graph = graph_backend(owner, store=False)
    for _ in range(30):
        with pytest.raises(RenderPending):
            graph().output(frame)
        finish(jobs)
    assert max(calls.values()) > 1
    assert all(stage == 1 for stage, _ in calls)  # Never reaches the second warp.


@pytest.mark.parametrize('changed', ['source', 'chapter', 'history', 'contract', 'cancel', 'live-cancel'])
def test_private_partial_is_not_published_after_context_or_cancellation(owner, changed):
    jobs, source, frame, context, calls, graph = graph_backend(owner)
    with pytest.raises(RenderPending):
        graph().output(frame)
    finish(jobs)
    old_graph = graph()
    with pytest.raises(RenderPending):
        old_graph.output(frame)
    assert any(not item.complete for item in jobs._regions.values())
    if changed == 'cancel':
        jobs.cancel(clear_retained=False)
    elif changed == 'live-cancel':
        jobs.cancel_exact()
    else:
        context[['source', 'chapter', 'history', 'contract'].index(changed)] = changed + '-new'
    with pytest.raises(RenderPending):
        # A new graph with the old scoped store cannot adopt stale pixels.
        old_store = old_graph.region_store
        old_store.get(('unused',))
    if changed not in ('cancel', 'live-cancel'):
        jobs.private_regions(tuple(context), lambda: tuple(context))
    assert not jobs._regions
    assert all(key[0] != 'tile-region-assembly' for key in owner.results)


def test_complete_cow_handoff_can_evict_parent_without_mutating_worker_source(owner):
    jobs = owner._effect_jobs
    jobs.retained_budget = 2048
    jobs.retained_limit = 2
    context = ('current',)
    store = jobs.private_regions(context, lambda: context)
    frame = QRectF(0, 0, 16, 16)
    parent = store.begin(('parent',), frame, QImage.Format_ARGB32_Premultiplied, 1)
    parent.image.fill(QColor('red'))
    parent.covered.add((0, 0))
    incoming = store.finish(('parent',), parent)
    original = QImage(incoming)
    child = store.begin(('child',), frame, QImage.Format_ARGB32_Premultiplied, 1)
    assert store.get(('parent',)) is None  # Completed input yields the existing pool.
    child.image.fill(QColor('blue'))
    assert incoming == original and incoming.pixelColor(0, 0) == QColor('red')
    assert jobs.retained_bytes <= jobs.retained_budget
    jobs.cancel()
    assert not jobs._regions and not jobs.retained_bytes and not jobs._retained_images


def legacy_shared(frame, requested, source, output_format, tile_size):
    result = QImage(int(requested.width()), int(requested.height()), output_format)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode_Source)
    for _, bounds in tiles(frame, requested.intersected(frame), tile_size):
        crop = bounds.translated(-frame.topLeft()).toAlignedRect()
        painter.drawImage(bounds.topLeft() - requested.topLeft(), source.copy(crop))
    painter.end()
    return result


@pytest.mark.parametrize('input_format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
@pytest.mark.parametrize('metadata', ['native', 'dpr', 'colorspace', 'fractional'])
def test_shared_frame_keeps_native_conversion_metadata_sampling_and_transparent_halos(qapp, input_format, metadata):
    source = native_image(33, 29, input_format)
    if metadata == 'dpr':
        source.setDevicePixelRatio(2)
    elif metadata == 'colorspace':
        source.setColorSpace(QColorSpace.SRgb)
    frame, requested = QRectF(-7, -11, 33, 29), QRectF(-10, -14, 41, 38)
    if metadata == 'fractional':
        frame = QRectF(-7.375, -11.625, 33, 29)
    node = TileNode(frame, ('shared',), None, None, shared_frame=True)
    graph = TileGraph([node], lambda _: QImage(source), lambda *_: None, lambda *_: None, tile_size=8)
    actual = graph.region(0, requested)
    expected = legacy_shared(frame, requested, source, node.format, 8)
    assert actual.format() == expected.format()
    assert actual.devicePixelRatio() == expected.devicePixelRatio()
    assert actual.colorSpace() == expected.colorSpace()
    assert bits(actual) == bits(expected)
