"""Editing command retention preserves native Qt primitives and painter state."""
import weakref

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPolygonF

import comic_editor.ui.painting_gizmos as gizmos


def image():
    result = QImage(256, 192, QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor('#426789'))
    return result


def painter_state(painter):
    return (painter.transform(), painter.pen(), painter.brush(), painter.font(),
            painter.opacity(), painter.renderHints(), painter.clipPath(),
            painter.hasClipping(), painter.compositionMode())


def configure(painter):
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(9.5, 8.25)
    painter.scale(.57, .57)
    painter.setOpacity(.91)
    painter.setFont(QFont('Arial', 11))


def primitives(painter):
    painter.save()
    painter.setPen(QPen(QColor('#36b7ff'), 2 / .57, Qt.PenStyle.DashLine))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolygon(QPolygonF([QPointF(8, 8), QPointF(290.5, 15),
                                  QPointF(310, 201.25), QPointF(10, 210)]))
    painter.drawEllipse(QPointF(83.25, 42.5), 7.5, 7.5)
    painter.drawLine(QPointF(12.5, 17.25), QPointF(204.5, 188.75))
    path = QPainterPath(QPointF(41, 51))
    path.cubicTo(48, 80, 105, 29, 156, 136)
    painter.drawPath(path)
    painter.setBrush(QColor('#ffffff'))
    painter.drawRoundedRect(QRectF(50, 30, 90, 35), 6, 6)
    font = painter.font()
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRectF(50, 30, 90, 35), Qt.AlignmentFlag.AlignCenter, 'Free')
    painter.restore()


def test_recording_draws_nothing_and_replays_exact_primitive_overloads(qapp):
    actual, expected = image(), image()
    painter = QPainter(actual)
    configure(painter)
    original_pixels = bytes(actual.constBits())
    original_state = painter_state(painter)
    commands = gizmos.record_painting_gizmos(painter, primitives)
    assert commands is not None
    assert 0 < commands.command_count < gizmos.MAX_GIZMO_COMMANDS
    assert 0 < commands.byte_count < gizmos.MAX_GIZMO_BYTES
    assert bytes(actual.constBits()) == original_pixels
    assert painter_state(painter) == original_state
    commands.replay(painter)
    assert painter_state(painter) == original_state
    painter.end()
    direct = QPainter(expected)
    configure(direct)
    primitives(direct)
    direct.end()
    assert bytes(actual.constBits()) == bytes(expected.constBits())


def test_copied_pen_and_geometry_cannot_change_after_recording(qapp):
    actual, expected = image(), image()
    pen = QPen(QColor('#36b7ff'), 3.25)
    path = QPainterPath(QPointF(3, 7))
    path.lineTo(120, 90)
    point = QPointF(75.5, 54.25)
    polygon = [QPointF(1, 1), QPointF(30, 1), QPointF(15, 25)]

    def draw(painter):
        painter.save()
        painter.setPen(pen)
        painter.drawPath(path)
        painter.drawEllipse(point, 8, 8)
        painter.drawPolygon(polygon)
        painter.restore()

    direct = QPainter(expected)
    draw(direct)
    direct.end()
    painter = QPainter(actual)
    commands = gizmos.record_painting_gizmos(painter, draw)
    assert commands is not None
    pen.setColor(QColor('red'))
    path.clear()
    point.setX(205)
    polygon[0].setX(190)
    commands.replay(painter)
    painter.end()
    assert bytes(actual.constBits()) == bytes(expected.constBits())


@pytest.mark.parametrize('operation', ['image', 'texture', 'native', 'unknown',
                                      'overflow', 'underflow', 'unbalanced'])
def test_declined_recording_restores_entry_state_without_drawing(qapp, monkeypatch, operation):
    actual = image()
    painter = QPainter(actual)
    configure(painter)
    original_pixels, original_state = bytes(actual.constBits()), painter_state(painter)
    if operation == 'overflow':
        monkeypatch.setattr(gizmos, 'MAX_GIZMO_COMMANDS', 5)

    def draw(recorder):
        recorder.save()
        recorder.translate(40, 40)
        recorder.setPen(QColor('red'))
        recorder.drawRect(1, 1, 20, 20)
        if operation == 'image':
            recorder.drawImage(QPointF(0, 0), actual)
        elif operation == 'texture':
            recorder.setBrush(QBrush(actual))
        elif operation == 'native':
            recorder.beginNativePainting()
        elif operation == 'unknown':
            recorder.paintEngine()
        elif operation == 'overflow':
            recorder.drawRect(2, 2, 30, 30)
            recorder.drawRect(3, 3, 40, 40)
        elif operation == 'underflow':
            recorder.restore()
            recorder.restore()
        # Intentionally leave the outer save outstanding for all other failures.

    assert gizmos.record_painting_gizmos(painter, draw) is None
    assert bytes(actual.constBits()) == original_pixels
    assert painter_state(painter) == original_state
    painter.end()


def test_geometry_and_text_payload_limit_is_checked_before_retention(qapp, monkeypatch):
    actual = image()
    painter = QPainter(actual)
    original_pixels = bytes(actual.constBits())
    monkeypatch.setattr(gizmos, 'MAX_GIZMO_BYTES', 512)
    assert gizmos.record_painting_gizmos(
        painter, lambda recorder: recorder.drawText(QPointF(0, 0), 'x' * 1024)) is None
    assert bytes(actual.constBits()) == original_pixels
    painter.end()


def test_callback_error_unwinds_nested_state_before_propagating(qapp):
    actual = image()
    painter = QPainter(actual)
    configure(painter)
    original_state = painter_state(painter)

    def draw(recorder):
        recorder.save()
        recorder.translate(40, 40)
        recorder.save()
        recorder.setOpacity(.1)
        raise ValueError('callback failure')

    with pytest.raises(ValueError, match='callback failure'):
        gizmos.record_painting_gizmos(painter, draw)
    assert painter_state(painter) == original_state
    painter.end()


def test_retained_commands_do_not_own_painter_or_paint_device(qapp):
    actual = image()
    painter = QPainter(actual)
    device_ref = weakref.ref(actual)
    painter_ref = weakref.ref(painter)
    commands = gizmos.record_painting_gizmos(painter, primitives)
    assert commands is not None
    painter.end()
    del painter, actual
    assert painter_ref() is None
    assert device_ref() is None


def test_inactive_painter_declines_without_invoking_callback(qapp):
    calls = []
    assert gizmos.record_painting_gizmos(QPainter(), lambda painter: calls.append(True)) is None
    assert not calls
