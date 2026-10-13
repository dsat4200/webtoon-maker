"""Metadata capture reuse preserves actual original pixels and pin ownership."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from PySide6.QtGui import QColor, QImage

from comic_editor.core.changes import ChangeSet, EntityChange, ResourceChange
from comic_editor.core.models import RasterObject
from comic_editor.render.scene import DetachedSceneBackend
from test_detached_scene import canvas, freeze, pixels


def metadata(canvas):
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    page.visible = not page.visible
    canvas._publish_change_set(ChangeSet((EntityChange(('layer', page.layer_id),
        frozenset({'visible'})),), label='Visibility'), action='commit')


@pytest.mark.parametrize('format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied, QImage.Format_RGBA16FPx4_Premultiplied,
    QImage.Format_RGBA32FPx4_Premultiplied])
def test_metadata_reuse_has_independent_headers_and_cow_originals(canvas, format):
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0], RasterObject())
    image = QImage(256, 256, format)
    image.fill(QColor('#b3557799'))
    original = pixels(image)
    canvas.tiles.set_tile(obj.object_id, (0, 0), image)
    compiler = canvas._scene_snapshot_compiler
    first = freeze(canvas, compiler)
    headers = compiler.native_sources
    metadata(canvas)
    second = freeze(canvas, compiler)
    assert compiler.native_sources is headers
    assert first.tiles is not second.tiles and first.tiles.residency is not second.tiles.residency
    assert first.tiles._tiles[obj.object_id] is not second.tiles._tiles[obj.object_id]
    borrower = second.tiles.tile(obj.object_id, (0, 0))
    assert borrower.format() == format and pixels(borrower) == original
    borrower.fill(QColor('red'))
    # A worker may decode/spill/edit its own COW borrower without modifying the
    # retained original headers or any preceding/later snapshot.
    assert pixels(first.tiles.tile(obj.object_id, (0, 0))) == original
    assert pixels(canvas.tiles.tile(obj.object_id, (0, 0))) == original
    metadata(canvas)
    third = freeze(canvas, compiler)
    assert pixels(third.tiles.tile(obj.object_id, (0, 0))) == original


@pytest.mark.parametrize('mutation', ['replace', 'borrow', 'register', 'delete'])
def test_native_mutation_invalidates_even_before_typed_resource_publication(canvas, tmp_path, mutation):
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0], RasterObject())
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    canvas.tiles.set_tile(obj.object_id, (0, 0), image)
    compiler = canvas._scene_snapshot_compiler
    first = freeze(canvas, compiler)
    headers = compiler.native_sources
    blue = QImage(image)
    blue.fill(QColor('blue'))
    owner = canvas.tiles._tiles[obj.object_id]
    if mutation == 'replace':
        canvas.tiles.set_tile(obj.object_id, (0, 0), blue)
    elif mutation == 'borrow':
        canvas.tiles.tile(obj.object_id, (0, 0)).fill(QColor('blue'))
    elif mutation == 'register':
        # Clean source replacement is source ownership, not document dirtiness.
        canvas.tiles.residency.remove(owner, (0, 0))
        path = tmp_path/'replacement.png'
        assert blue.save(str(path))
        canvas.tiles.dirty.clear()
        owner.register((0, 0), path)
        assert not canvas.tiles.dirty
    else:
        del owner[(0, 0)]
    metadata(canvas)
    second = freeze(canvas, compiler)
    assert compiler.native_sources is not headers
    assert first.tiles.tile(obj.object_id, (0, 0)).pixelColor(20, 20) == QColor('red')
    if mutation == 'delete':
        assert second.tiles.tile(obj.object_id, (0, 0)) is None
    else:
        assert second.tiles.tile(obj.object_id, (0, 0)).pixelColor(20, 20) == QColor('blue')


def test_reused_clean_file_pin_survives_replacement_and_multiple_consumers(canvas, tmp_path, monkeypatch):
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0], RasterObject())
    red = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    red.fill(QColor('red'))
    path = tmp_path/'original.png'
    assert red.save(str(path))
    canvas.tiles._object_tiles(obj.object_id).register((0, 0), path)
    compiler = canvas._scene_snapshot_compiler
    first = freeze(canvas, compiler)
    for job in first.pin_jobs:
        job.future.result(timeout=10)
    metadata(canvas)
    second = freeze(canvas, compiler)
    blue = QImage(red)
    blue.fill(QColor('blue'))
    replacement = tmp_path/'next.png'
    assert blue.save(str(replacement))
    replacement.replace(path)
    calls = []
    ordinary = second._finish_sources
    def prepare():
        calls.append(True)
        return ordinary()
    object.__setattr__(second, '_finish_sources', prepare)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: second.finish_sources(), range(8)))
    assert len(calls) == 1
    for snapshot in (first, second):
        backend = DetachedSceneBackend(snapshot)
        try:
            assert backend.scene.tiles.tile(obj.object_id, (0, 0)).pixelColor(20, 20) == QColor('red')
        finally:
            backend.close()
    assert canvas.tiles.residency.decodes == 0


@pytest.mark.parametrize('change', [None, ChangeSet(conservative=True),
    ChangeSet(resources=(ResourceChange(('object','source'), 'raster', (0,0)),)),
    ChangeSet((EntityChange(('object','source'), frozenset({'*'})),)),
    ChangeSet((EntityChange(('layer','page'), structural=True),)),
    ChangeSet(document_fields=frozenset({'pixel_contract'}))])
def test_unknown_resource_structural_and_contract_edits_retire_headers(canvas, change):
    compiler = canvas._scene_snapshot_compiler
    freeze(canvas, compiler)
    assert compiler.native_sources is not None
    compiler.invalidate(change)
    assert compiler.native_sources is None
