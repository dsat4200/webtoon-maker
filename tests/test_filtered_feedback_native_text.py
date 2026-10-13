"""Native Qt text commands retain whole-device pixels and scene ordering."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QAbstractTextDocumentLayout, QColor, QColorSpace,
    QFont, QImage, QPainter, QPainterPath, QPalette, QTextDocument, QTransform)

from comic_editor.core.models import TextObject, OutlineModifier, BrightnessContrastModifier
from comic_editor.render.filtered_raster_feedback import (
    FeedbackUnavailable, NativeTextOperation, _Payload, _Recorder,
    capture_native_feedback_recipe, update_native_feedback_recipe)
from comic_editor.render.outputs import capture_document
from test_filtered_native_recipe import native, ordinary, changed_snapshot
from test_outline_only_native_feedback import outline_fixture, influence


def text_state(painter, transformed, composition, text_antialiasing=True):
    painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing, text_antialiasing)
    painter.setOpacity(.37)
    painter.setCompositionMode(composition)
    painter.setFont(QFont('Segoe UI', 17))
    if transformed == 'projective':
        painter.setTransform(QTransform(.83, .017, .0007, .07, 1.17, -.0004, 17.25, 23.75, 1.))
    elif transformed:
        painter.setTransform(QTransform().translate(17.25, 23.75).rotate(-7.3).scale(.83, 1.17))
    clip = QPainterPath()
    clip.addEllipse(QRectF(7.25, 4.5, 198.75, 164.25))
    painter.setClipPath(clip)
    painter.translate(-3.125, 6.25)


@pytest.mark.parametrize('format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
@pytest.mark.parametrize('transformed', [False, True, 'projective'])
@pytest.mark.parametrize('composition', [QPainter.CompositionMode_SourceOver, QPainter.CompositionMode_Multiply])
@pytest.mark.parametrize('text_antialiasing', [False, True])
def test_native_layout_commands_match_direct_draw_with_original_device_state(qapp, format, transformed, composition, text_antialiasing):
    document = QTextDocument()
    document.setDefaultFont(QFont('Segoe UI', 19))
    document.setHtml('<p>POCKET <b>TEST</b> é中😀<br/><i>second line</i></p>')
    document.setTextWidth(205.25)
    context = QAbstractTextDocumentLayout.PaintContext()
    context.palette.setColor(QPalette.Text, QColor.fromRgbF(.19, .73, .42, .67))
    basis = QImage(256, 224, format)
    basis.setColorSpace(QColorSpace(QColorSpace.SRgb))
    basis.fill(QColor.fromRgbF(.43, .27, .81, .77))
    expected = basis.copy()
    painter = QPainter(expected)
    text_state(painter, transformed, composition, text_antialiasing)
    document.documentLayout().draw(painter, context)
    painter.end()
    initial = basis.copy()
    painter = QPainter(initial)
    text_state(painter, transformed, composition, text_antialiasing)
    recorder = _Recorder(painter, _Payload(8 * 1024 * 1024), lambda: False)
    recorder.phase = 'after'
    recorder._draw_native_text_document(document, context)
    painter.end()
    assert native(initial) == native(expected)
    assert len(recorder.operations) == 1
    operation = recorder.operations[0]
    assert isinstance(operation, NativeTextOperation)
    # The document and layout are never retained by the operation.
    assert set(vars(operation)) == {'picture', 'name'}
    actual = basis.copy()
    painter = QPainter(actual)
    text_state(painter, transformed, composition, text_antialiasing)
    before = (painter.transform(), painter.opacity(), painter.clipPath(), painter.compositionMode())
    operation.apply(painter)
    after = (painter.transform(), painter.opacity(), painter.clipPath(), painter.compositionMode())
    painter.end()
    assert before == after
    assert native(actual) == native(expected)
    assert native(expected) != native(basis)


def text_siblings(chapter, selected, modified):
    layer = chapter.layers[selected.parent_layer_id]
    selected_ref = next(ref for ref in layer.children if ref.entity_id == selected.object_id)
    original = [ref for ref in layer.children if ref is not selected_ref]
    text_refs = []
    for index, layout in enumerate(('strict', 'free')):
        text = chapter.add_object(layer.layer_id, TextObject(
            x=31.25 + index * 17.5, y=71.75 + index * 29.5,
            width=277.5, height=167.25, opacity=.59,
            text='POCKET TEST\né中 😀', text_color='#a04433cc', font_family='Cammi Sans Bold',
            font_size=37.25, layout_mode=layout, bold=bool(index), italic=not index,
            margin=9.125, vertical_alignment='middle'))
        if layout == 'free':
            transform = QTransform().translate(180., 160.).rotate(5.7).translate(-180., -160.)
            text.transform_quad = [transform.map(QPointF(x, y)).toTuple() for x, y in (
                (text.x, text.y), (text.x + text.width, text.y),
                (text.x + text.width, text.y + text.height), (text.x, text.y + text.height))]
        if modified:
            for modifier in (BrightnessContrastModifier(brightness=13., contrast=23.),
                    OutlineModifier(thickness=3.5, color='#80ffeebb', antialiasing=True)):
                chapter.add_modifier(modifier, [('object', text.object_id)])
        text_refs.append(next(ref for ref in layer.children if ref.entity_id == text.object_id))
    layer.children[:] = [text_refs[1], selected_ref, text_refs[0], *original]


@pytest.mark.parametrize('modified', [False, True])
@pytest.mark.parametrize('rotate', [False, True])
def test_text_prefix_suffix_and_held_pencil_eraser_match_full_ordinary_scene(qapp, modified, rotate):
    chapter, tiles, images, selected, _outline, _snapshot, request = outline_fixture(rotate=rotate)
    text_siblings(chapter, selected, modified)
    baseline = capture_document(chapter, tiles, images)
    recipe = capture_native_feedback_recipe(baseline, selected.object_id, request)
    assert native(recipe.initial) == native(ordinary(baseline, request))
    if not modified:
        assert any(isinstance(op, NativeTextOperation) for op in recipe.operations)
    previous = None
    initial = native(recipe.initial), native(recipe.source_image)
    for revision, (point, erase) in enumerate(((QPointF(137., 113.), False),
            (QPointF(153., 126.), False), (QPointF(137., 113.), True)), 1):
        tiles.paint_dab(selected.object_id, point, 19.5, QColor('#c0997722'), opacity=.73, erase=erase)
        updated = changed_snapshot(chapter, tiles, images, baseline, revision)
        actual = update_native_feedback_recipe(recipe, updated, influence(updated, selected, point),
                                               previous_source=previous)
        assert native(actual.image) == native(ordinary(updated, replace(request, revision=revision)))
        assert (native(recipe.initial), native(recipe.source_image)) == initial
        previous = actual.source


def test_native_text_commands_respect_budget_cancellation_and_native_dpi(qapp):
    document = QTextDocument('owned text')
    context = QAbstractTextDocumentLayout.PaintContext()
    image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    for budget, cancelled, dpi, message in ((1, False, False, 'byte budget'),
            (1024 * 1024, True, False, 'cancelled'), (1024 * 1024, False, True, 'DPI')):
        basis = image.copy()
        if dpi:
            basis.setDotsPerMeterX(5000)
        painter = QPainter(basis)
        recorder = _Recorder(painter, _Payload(budget), lambda: cancelled)
        recorder.phase = 'after'
        try:
            with pytest.raises(FeedbackUnavailable, match=message):
                recorder._draw_native_text_document(document, context)
        finally:
            painter.end()
        assert bytes(basis.constBits()) == bytes(image.constBits())


def test_owned_native_text_commands_replay_on_a_different_detached_worker(qapp):
    chapter, tiles, images, selected, _outline, _snapshot, request = outline_fixture(rotate=True)
    text_siblings(chapter, selected, False)
    baseline = capture_document(chapter, tiles, images)
    with ThreadPoolExecutor(max_workers=1) as first:
        recipe = first.submit(capture_native_feedback_recipe, baseline, selected.object_id, request).result(10)
    assert any(isinstance(op, NativeTextOperation) for op in recipe.operations)
    point = QPointF(137., 113.)
    tiles.paint_dab(selected.object_id, point, 19.5, QColor('#c0997722'))
    updated = changed_snapshot(chapter, tiles, images, baseline, 1)
    with ThreadPoolExecutor(max_workers=1) as second:
        result = second.submit(update_native_feedback_recipe, recipe, updated,
            influence(updated, selected, point)).result(10)
    assert native(result.image) == native(ordinary(updated, replace(request, revision=1)))
