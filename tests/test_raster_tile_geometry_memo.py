"""Placement reuse remains bounded and never retains authored pixel buffers."""
from dataclasses import replace
import gc
import weakref

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.ui import document_presentation as presentation


def tile():
    image = QImage(260, 260, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#a05f40'))
    return presentation.PresentedTile('original', image,
        QRectF(-256., 49920., 256., 256.), QRectF(2., 2., 256., 256.))


def geometry(painter, presented):
    result = presentation._raster_tile_ownership(
        painter, presented, presented.world_rect, presented.source_rect)
    return None if result is None else tuple(rect.getRect() for rect in result)


def test_pixel_revisions_share_geometry_without_retaining_native_images(qapp):
    memo = presentation._raster_tile_ownership_geometry
    memo.cache_clear()
    target = QImage(780, 920, QImage.Format_ARGB32_Premultiplied)
    target.setDevicePixelRatio(1.25)
    painter = QPainter(target)
    try:
        painter.setTransform(QTransform(.57, 0., 0., 0., .57, 0., -89.35, -28500.2, 1.))
        first = tile()
        source_ref = weakref.ref(first.image)
        expected = geometry(painter, first)
        revised_image = first.image.copy()
        revised_image.fill(QColor('#e02570'))
        revised = replace(first, key='current', image=revised_image)
        assert geometry(painter, revised) == expected
        assert memo.cache_info().hits == 1
        assert memo.cache_info().misses == 1
        assert revised.image.size().toTuple() == (260, 260)
        assert revised.image.devicePixelRatio() == 1.
        del first
        gc.collect()
        assert source_ref() is None
    finally:
        painter.end()
        memo.cache_clear()


def test_camera_clipping_image_geometry_and_device_density_select_new_ownership(qapp):
    memo = presentation._raster_tile_ownership_geometry
    memo.cache_clear()
    target = QImage(780, 920, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(target)
    presented = tile()
    try:
        painter.setTransform(QTransform.fromScale(.57, .57))
        first = geometry(painter, presented)
        physical = painter.deviceTransform().mapRect(presented.world_rect)
        assert first[0] == (round(physical.left()), round(physical.top()),
                            round(physical.right())-round(physical.left()),
                            round(physical.bottom())-round(physical.top()))
        clipped = replace(presented, world_rect=QRectF(-246., 49930., 236., 236.),
                          source_rect=QRectF(12., 12., 236., 236.))
        assert geometry(painter, clipped) != first
        painter.translate(.75, .25)
        assert geometry(painter, presented) != first
        larger = QImage(261, 260, QImage.Format_ARGB32_Premultiplied)
        assert geometry(painter, replace(presented, image=larger))[1][2] == 261.
        larger.setDevicePixelRatio(2.)
        assert geometry(painter, replace(presented, image=larger)) is None
        painter.end()
        target.setDevicePixelRatio(1.25)
        painter.begin(target)
        painter.setTransform(QTransform.fromScale(.57, .57))
        assert geometry(painter, presented) != first
        assert memo.cache_info().misses == 6
    finally:
        painter.end()
        memo.cache_clear()


def test_continuous_camera_changes_cannot_grow_geometry_memo_past_256(qapp):
    memo = presentation._raster_tile_ownership_geometry
    memo.cache_clear()
    target = QImage(780, 920, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(target)
    presented = tile()
    try:
        for index in range(400):
            painter.setTransform(QTransform(.57, 0., 0., 0., .57, 0.,
                                             index*.375, -28500.2, 1.))
            geometry(painter, presented)
        assert memo.cache_info().maxsize == 256
        assert memo.cache_info().currsize == 256
        before = memo.cache_info().misses
        painter.setTransform(QTransform(.57, 0., 0., 0., .57, 0., 0., -28500.2, 1.))
        geometry(painter, presented)
        assert memo.cache_info().misses == before+1
    finally:
        painter.end()
        memo.cache_clear()
