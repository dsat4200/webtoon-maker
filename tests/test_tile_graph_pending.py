"""A pending spatial dependency is traversed once per capture."""
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.render.service import RenderPending
from comic_editor.render.tile_graph import TileGraph, TileNode


def test_repeated_full_frame_pending_dependency_does_not_multiply_traversal(qapp):
    frame = QRectF(0, 0, 512, 512)
    source = QImage(512, 512, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor('#4080c0'))
    cached, calls, ready = {}, [], [False]
    def evaluate(output, incoming, needed):
        calls.append(tuple(output.getRect()))
        if not ready[0]:
            raise RenderPending('blocked spatial worker')
        return incoming.copy(output.toAlignedRect())
    nodes = [TileNode(frame, ('source',), None, None)]
    for stage in range(3):
        nodes.append(TileNode(frame, ('warp', stage), lambda _: frame, evaluate))
    def graph():
        return TileGraph(nodes, lambda rect: source.copy(rect.toAlignedRect()),
            lambda _owner, key: cached.get(key), lambda _owner, key, image: cached.__setitem__(key, image),
            continue_pending=lambda: True)
    first = graph()
    with pytest.raises(RenderPending):
        first.output(frame)
    assert len(calls) == 4  # Four source-stage tiles, rather than 4**3 retries.
    with pytest.raises(RenderPending):
        first.output(frame)
    assert len(calls) == 4
    ready[0] = True
    actual, bounds = graph().output(frame)
    assert actual == source and bounds == frame


@pytest.mark.parametrize('pending', [False, True])
def test_stage_preflight_runs_before_any_source_dependencies(qapp, pending):
    frame = QRectF(0, 0, 256, 256)
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#4080c0'))
    def completed(_output):
        if pending:
            raise RenderPending('existing worker')
        return QImage(image)
    def forbidden(*_):
        pytest.fail('preflight recaptured a complete predecessor')
    graph = TileGraph([TileNode(frame, ('source',), None, None),
                       TileNode(frame, ('warp',), forbidden, forbidden, cached_output=completed)],
                      forbidden, lambda *_: None, lambda *_: None, cache_only=True)
    if pending:
        with pytest.raises(RenderPending, match='existing worker'):
            graph.output(frame)
    else:
        assert graph.output(frame) == (image, frame)
