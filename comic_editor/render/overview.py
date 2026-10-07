"""Bounded presentation of an exact native scene larger than the tile LRU."""
from dataclasses import dataclass
import math

import numpy as np

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from comic_editor.render.admission import WorkCancelled
from comic_editor.render.device import DeviceImage, cpu_image
from comic_editor.render.pixels import display_image, pixel_scope
from comic_editor.render.service import TileBatchPolicy


@dataclass(frozen=True)
class OverviewPresentation:
    document: object
    bounds: tuple
    image: QImage


def exact_overview(demand, backend, service, cancelled):
    """Compose ordinary exact native blocks onto a display-only surface.

    This image never enters an artwork or durable cache. Effects, vector/text
    captures and tile gutters retain their ordinary native grid. Only the
    finished native pixels are reduced for the presentation surface.
    """
    x, y, width, height = demand.visible
    output_width, output_height = demand.presentation_size
    image = QImage(output_width, output_height, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    sx,sy = output_width/width,output_height/height
    output = np.frombuffer(image.bits(),np.uint8).reshape(output_height,output_width,4)
    # One presentation grid covers every block. Native samples are centered
    # at n+.5; tile-local source cropping must never reset filtering phase.
    sample_x = x+(np.arange(output_width,dtype=np.float64)+.5)/sx-.5
    sample_y = y+(np.arange(output_height,dtype=np.float64)+.5)/sy-.5
    groups = {}
    for request in demand.requests:
        address = request.address
        groups.setdefault((address.level, address.x // 4, address.y // 4), []).append(request)
    def distance(key):
        side = 4 * groups[key][0].tile_size
        return ((key[1] + .5) * side - demand.center[0]) ** 2 + ((key[2] + .5) * side - demand.center[1]) ** 2
    for group in sorted(groups, key=distance):
        if cancelled():
            raise WorkCancelled()
        requests = groups[group]
        restored, missing = {}, []
        for request in requests:
            cached = backend.lookup_tile(request, None)
            if cached is None or cached.isNull():
                missing.append(request)
            else:
                restored[request.address] = cached, True
        result = service.render_tiles(demand.snapshot.document, missing,
            TileBatchPolicy(demand.center), phase=None, defer_effects=False)
        result.tiles.update(restored)
        if result.error or result.pending or not all(exact for _, exact in result.tiles.values()):
            raise RuntimeError(result.error or "Overview did not produce an exact native block")
        with pixel_scope(demand.snapshot.document.pixel_contract,
                environment=demand.snapshot.pixel_environment):
            for request in requests:
                if cancelled():
                    raise WorkCancelled()
                native, exact = result.tiles[request.address]
                display = display_image(_native_cpu_tile(native,demand,request,service),
                    demand.snapshot.document.pixel_contract)
                rect = request.world_rect
                left,right = max(0,math.ceil((rect.left()-x)*sx-.5)),min(output_width,math.ceil((rect.right()-x)*sx-.5))
                top,bottom = max(0,math.ceil((rect.top()-y)*sy-.5)),min(output_height,math.ceil((rect.bottom()-y)*sy-.5))
                if right <= left or bottom <= top:
                    continue
                _sample_display_tile(output,display,request.capture_rect,
                    sample_x[left:right],sample_y[top:bottom],left,top)
    if cancelled():
        raise WorkCancelled()
    return OverviewPresentation(demand.snapshot.document, demand.visible, image)


def _native_cpu_tile(native,demand,request,service):
    try:
        return cpu_image(native)
    except RuntimeError:
        if not isinstance(native,DeviceImage) or not native.isNull():
            raise
        # Context loss invalidates storage, not the exact semantic request.
        # The same detached service falls back to its CPU evaluator off-GUI.
        fallback = service.render_tiles(demand.snapshot.document,[request],
            TileBatchPolicy(demand.center),phase=None,defer_effects=False)
        image,exact = fallback.tiles[request.address]
        if fallback.error or fallback.pending or not exact or image.isNull():
            raise RuntimeError(fallback.error or 'Overview could not restore a lost graphics tile')
        return cpu_image(image)


def _sample_display_tile(output,image,capture,sample_x,sample_y,left,top):
    """Bilinear display reduction using global positions and native gutters."""
    image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    pixels = np.frombuffer(image.constBits(),np.uint8).reshape(image.height(),image.bytesPerLine()//4,4)
    ix,iy = np.floor(sample_x).astype(np.int64),np.floor(sample_y).astype(np.int64)
    fx,fy = (sample_x-ix)[None,:,None],(sample_y-iy)[:,None,None]
    ix,iy = ix-int(capture.x()),iy-int(capture.y())
    if (ix.min() < 0 or iy.min() < 0 or ix.max()+1 >= image.width() or iy.max()+1 >= image.height()):
        raise ValueError('Native tile gutters do not cover its presentation samples')
    a = pixels[iy[:,None],ix[None,:]].astype(np.float64)
    b = pixels[iy[:,None],ix[None,:]+1].astype(np.float64)
    c = pixels[iy[:,None]+1,ix[None,:]].astype(np.float64)
    d = pixels[iy[:,None]+1,ix[None,:]+1].astype(np.float64)
    values = (a*(1.-fx)+b*fx)*(1.-fy)+(c*(1.-fx)+d*fx)*fy
    output[top:top+len(sample_y),left:left+len(sample_x)] = np.rint(values).astype(np.uint8)
