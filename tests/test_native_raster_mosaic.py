"""Portable exact-pixel presentation tests; no scene/source renderer substitutions."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QRect, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QColorSpace, QImage, QPainter, QTransform

from comic_editor.core.pixel_contract import LEGACY_PIXELS, FLOAT_PIXELS
from comic_editor.ui import document_presentation as presentation
from comic_editor.ui.document_presentation import PresentedTile


def pixels(image):
    storage = image.constBits()
    if storage is None:
        assert image.isNull(), 'Only a null QImage may omit native storage'
        return b''
    return bytes(storage)


def pattern(width=700, height=480):
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(1.)
    image.setColorSpace(QColorSpace(QColorSpace.SRgb))
    rows = np.frombuffer(image.bits(), np.uint8).reshape(height, image.bytesPerLine())
    rgba = rows[:, :width*4].reshape(height, width, 4)
    xx, yy = np.arange(width)[None,:], np.arange(height)[:,None]
    rgba[...,3] = 80 + (xx*3+yy*5)%176
    for channel in range(3):
        rgba[...,channel] = ((xx*(channel+2)+yy*(channel+7))%80).astype(np.uint8)
    return image


def tiles_for(native):
    result=[]
    native_rows = np.frombuffer(native.constBits(), np.uint8).reshape(native.height(), native.bytesPerLine())
    for y in range(0,native.height(),256):
        for x in range(0,native.width(),256):
            image=QImage(260,260,QImage.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(1.)
            image.setColorSpace(native.colorSpace())
            image.fill(Qt.transparent)
            known=QRect(x-2,y-2,260,260).intersected(native.rect())
            out=np.frombuffer(image.bits(),np.uint8).reshape(260,image.bytesPerLine())
            dy,dx=known.y()-(y-2),known.x()-(x-2)
            out[dy:dy+known.height(),dx*4:(dx+known.width())*4] = native_rows[known.y():known.y()+known.height(),known.x()*4:(known.x()+known.width())*4]
            result.append(PresentedTile((None,(x//256,y//256)),image,QRectF(x,y,256,256),QRectF(2,2,256,256)))
    return result


def owner():
    return SimpleNamespace(_native_raster_mosaic=None)


def camera(scale=1.,x=0.,y=10.):
    return QTransform(scale,0.,0.,0.,scale,0.,x,y,1.)


def assemble(holder, tiles, native, *, transform=None, viewport=None, context=('document',1),smooth=True,dpr=1.5):
    return presentation._native_raster_mosaic(holder,tiles,transform or camera(),viewport or QSizeF(700,500),
        QRectF(native.rect()),context,smooth=smooth,presentation_dpr=dpr)


def output(native, region, transform, viewport, dpr):
    image=QImage(round(viewport.width()*dpr),round(viewport.height()*dpr),QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.transparent)
    painter=QPainter(image)
    try:
        painter.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
        presentation._draw_native_raster_mosaic(painter,(native,region),transform,LEGACY_PIXELS,None,smooth=True)
    finally:
        painter.end()
    return image


@pytest.mark.parametrize('dpr',[1.,1.5,2.])
@pytest.mark.parametrize('transform',[camera(),camera(1.25,5.,10.)])
def test_complete_native_cores_match_independent_whole_frame_and_presentation(qapp,dpr,transform):
    native=pattern();tiles=tiles_for(native);holder=owner()
    before=[pixels(t.image) for t in tiles]
    actual=assemble(holder,tiles,native,transform=transform,dpr=dpr)
    assert actual is not None
    joined,region=actual
    reference=native.copy(region.toAlignedRect())
    assert joined.format()==native.format() and joined.colorSpace()==native.colorSpace()
    assert joined.devicePixelRatio()==native.devicePixelRatio()==1.
    assert pixels(joined)==pixels(reference)
    assert joined.sizeInBytes()<=4*1024*1024
    live=output(reference,region,transform,QSizeF(700,500),dpr)
    committed=output(joined,region,transform,QSizeF(700,500),dpr)
    assert pixels(committed)==pixels(live)
    assert [pixels(t.image) for t in tiles]==before


def test_warm_unchanged_batch_reuses_one_surface_without_pixel_copy(qapp,monkeypatch):
    native=pattern();tiles=tiles_for(native);holder=owner()
    first=assemble(holder,tiles,native)
    saved=holder._native_raster_mosaic
    def forbidden(*a,**k):raise AssertionError('Warm presentation attempted a source/native pixel copy')
    with monkeypatch.context() as patch:
        patch.setattr(presentation.np,'frombuffer',forbidden)
        second=assemble(holder,tiles,native)
    assert holder._native_raster_mosaic is saved
    assert second[0].cacheKey()==first[0].cacheKey()
    assert pixels(second[0])==pixels(native)


@pytest.mark.parametrize('context',[('document',2),('other-document',1),('document',1,'new-source-generation'),('document',1,'new-mask-config')])
def test_document_revision_source_and_configuration_changes_retire_cached_surface(qapp,context):
    native=pattern();tiles=tiles_for(native);holder=owner()
    initial=assemble(holder,tiles,native)
    current=assemble(holder,tiles,native,context=context)
    assert current is not None and current[0].cacheKey()!=initial[0].cacheKey()
    assert pixels(current[0])==pixels(native)
    assert holder._native_raster_mosaic[0][0]==context


@pytest.mark.parametrize('change',['mutate-input','replace-input','source-rect'])
def test_actual_input_changes_match_fresh_current_pixels_never_old_surface(qapp,change):
    native=pattern();tiles=tiles_for(native);holder=owner()
    initial=assemble(holder,tiles,native)
    first_image=tiles[0].image
    initial_key=first_image.cacheKey()
    if change=='mutate-input':
        first_image.setPixelColor(32,42,QColor('#ff0000'))
        assert first_image.cacheKey()!=initial_key
    elif change=='replace-input':
        replacement=QImage(first_image)
        replacement.fill(QColor('#0000ff'))
        tiles[0]=replace(tiles[0],image=replacement)
        assert replacement.cacheKey()!=initial_key
    else:
        tiles[0]=replace(tiles[0],source_rect=QRectF(3,2,256,256))
    current=assemble(holder,tiles,native)
    fresh=assemble(owner(),tiles,native)
    assert current is not None and fresh is not None
    assert current[0].cacheKey()!=initial[0].cacheKey()
    assert pixels(current[0])==pixels(fresh[0])
    assert pixels(current[0])!=pixels(initial[0])


@pytest.mark.parametrize('change',['camera','smooth','DPR','profile'])
def test_presentation_metadata_changes_rebuild_without_increasing_native_density(qapp,change):
    native=pattern();tiles=tiles_for(native);holder=owner()
    initial=assemble(holder,tiles,native)
    kwargs={}
    if change=='camera':kwargs['transform']=camera(1.,1.,10.)
    elif change=='smooth':kwargs['smooth']=False
    elif change=='DPR':kwargs['dpr']=2.
    else:
        for tile in tiles:tile.image.setColorSpace(QColorSpace(QColorSpace.AdobeRgb))
    current=assemble(holder,tiles,native,**kwargs)
    fresh=assemble(owner(),tiles,native,**kwargs)
    assert current is not None and fresh is not None
    assert current[0].cacheKey()!=initial[0].cacheKey()
    assert pixels(current[0])==pixels(fresh[0])
    assert current[0].devicePixelRatio()==1.
    assert current[0].width()<=native.width() and current[0].height()<=native.height()


@pytest.mark.parametrize('reason',['missing','overlap','preview','float16','float32','RGBA64','DPR','rotation','shear','projective','negative-scale','source-density','fractional-source','outside-source','no-context','unknown-environment','mixed-environment','null-image','single-preview','tile-count','fractional-view'])
def test_unsupported_or_missing_current_inputs_discard_and_preserve_native_pixels(qapp,reason):
    native=pattern();tiles=tiles_for(native);holder=owner()
    assert assemble(holder,tiles,native) is not None
    transform=camera();context=('document',1)
    if reason=='missing':tiles=tiles[1:]
    elif reason=='overlap':tiles[1]=replace(tiles[1],world_rect=tiles[0].world_rect)
    elif reason=='preview':tiles[0]=replace(tiles[0],key=('preview',17))
    elif reason in ('float16','float32'):
        contract=replace(FLOAT_PIXELS,precision=reason)
        tiles[0]=replace(tiles[0],pixel_contract=contract,image=tiles[0].image.convertToFormat(contract.image_format))
    elif reason=='RGBA64':tiles[0]=replace(tiles[0],image=tiles[0].image.convertToFormat(QImage.Format_RGBA64_Premultiplied))
    elif reason=='DPR':tiles[0].image.setDevicePixelRatio(2.)
    elif reason=='rotation':transform.rotate(7.)
    elif reason=='shear':transform.shear(.1,.2)
    elif reason=='projective':transform=QTransform(1.,0.,.0001,0.,1.,0.,0.,10.,1.)
    elif reason=='negative-scale':transform.scale(-1.,1.)
    elif reason=='fractional-view':transform=camera(1.25,3.25,-2.75)
    elif reason=='source-density':tiles[0]=replace(tiles[0],source_rect=QRectF(2,2,128,256))
    elif reason=='fractional-source':tiles[0]=replace(tiles[0],source_rect=QRectF(2.5,2,256,256))
    elif reason=='outside-source':tiles[0]=replace(tiles[0],source_rect=QRectF(8,2,256,256))
    elif reason=='no-context':context=None
    elif reason=='unknown-environment':tiles[0]=replace(tiles[0],pixel_environment=object())
    elif reason=='mixed-environment':tiles[1]=replace(tiles[1],pixel_environment=SimpleNamespace(signature=('other-resource',)))
    elif reason=='null-image':tiles[0]=replace(tiles[0],image=QImage())
    elif reason=='single-preview':tiles=[PresentedTile(('preview',5),native,QRectF(native.rect()))]
    else:tiles=tiles*6
    before=[pixels(t.image) for t in tiles]
    assert assemble(holder,tiles,native,transform=transform,context=context) is None
    assert holder._native_raster_mosaic is None
    assert [pixels(t.image) for t in tiles]==before


def test_maximum_allowed_extent_and_bytes_are_exact_and_zoomout_rejects_before_copy(qapp,monkeypatch):
    native=pattern(2048,512);tiles=tiles_for(native);holder=owner()
    current=assemble(holder,tiles,native,transform=camera(1.,0.,0.),viewport=QSizeF(2048,512))
    assert current is not None
    assert current[0].sizeInBytes()==4*1024*1024
    assert pixels(current[0])==pixels(native)
    oversized=QRectF(0,0,2049,513)
    def forbidden(*a,**k):raise AssertionError('Rejected extent attempted a native pixel copy')
    with monkeypatch.context() as patch:
        patch.setattr(presentation.np,'frombuffer',forbidden)
        result=presentation._native_raster_mosaic(holder,tiles,camera(.5,0.,0.),QSizeF(2048,512),oversized,('document',1))
    assert result is None and holder._native_raster_mosaic is None


@pytest.mark.parametrize('owner_kind',['none','detached-image'])
def test_public_detached_presentation_default_is_pixel_identical_to_unchanged_fallback(qapp,owner_kind):
    native=pattern();tiles=tiles_for(native)
    expected=QImage(1050,750,QImage.Format_ARGB32_Premultiplied)
    expected.setDevicePixelRatio(1.5);expected.fill(Qt.transparent)
    actual=QImage(expected)
    for image,context in [(expected,None),(actual,('impersonated-document',1))]:
        painter=QPainter(image)
        try:
            painter.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
            presentation.draw_document_tiles(painter,tiles,camera(),QSizeF(700,500),
                owner=image if owner_kind=='detached-image' else None,smooth=True,
                native_context=context,native_bounds=QRectF(native.rect()))
        finally:painter.end()
    assert pixels(expected)==pixels(actual)
    assert getattr(actual,'_native_raster_mosaic',None) is None


def test_mosaic_draw_restores_painter_on_display_exception(qapp,monkeypatch):
    native=pattern();target=QImage(1050,750,QImage.Format_ARGB32_Premultiplied)
    target.fill(Qt.transparent)
    painter=QPainter(target)
    painter.translate(3.,4.)
    painter.setOpacity(.7)
    painter.setRenderHint(QPainter.Antialiasing,True)
    before=(QTransform(painter.transform()),painter.opacity(),painter.renderHints(),painter.compositionMode())
    def fail(*a,**k):raise RuntimeError('display edge rejected')
    monkeypatch.setattr(presentation,'display_image',fail)
    try:
        with pytest.raises(RuntimeError,match='display edge rejected'):
            presentation._draw_native_raster_mosaic(painter,(native,QRectF(native.rect())),camera(),LEGACY_PIXELS,None,smooth=True)
        assert (painter.transform(),painter.opacity(),painter.renderHints(),painter.compositionMode())==before
    finally:painter.end()
