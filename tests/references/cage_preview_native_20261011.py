"""Frozen native cage behavior preceding the October 11 scoped drafts."""
import copy
import math
import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter, QTransform
from comic_editor.render.effect_pipeline import aligned, empty_image
from references.cage_native_20261011 import warp_image

def _cage_object_preview(self, painter, obj, parent_opacity, visible):
        session = self._cage_session
        if not session or ("object", obj.object_id) not in session["targets"]:
            return False
        key = obj.object_id
        mapping = self.layer_world_transform(obj.parent_layer_id)
        source = session["sources"].get(key)
        if source is None:
            bounds = aligned(mapping.inverted()[0].mapRect(self.object_world_rect(obj.object_id)))
            image = empty_image(bounds)
            p = QPainter(image)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.translate(-bounds.left(), -bounds.top())
            self._render_cage_source = True
            try:
                self._render_object_content(p, obj, bounds)
            finally:
                self._render_cage_source = False
                p.end()
            source = image, bounds
            session["sources"][key] = source
        image, bounds = source
        grid = session["grid"]
        cache_key = ("cage-tool", key, int(image.cacheKey()), repr(grid.grid_dict()))
        warped = self._modifier_cache_get(cache_key)
        from comic_editor.ui.cage_rendering import mesh_for_image
        _, destination, _ = mesh_for_image(grid, bounds, mapping)
        low, high = destination.min(axis=0), destination.max(axis=0)
        output_bounds = aligned(QRectF(*low, *(high-low)))
        if warped is None:
            incoming, cage = QImage(image), copy.deepcopy(grid)
            placement, source_bounds, destination_bounds = QTransform(mapping), QRectF(bounds), QRectF(output_bounds)
            def compute(cancelled=None, pixel_scale=1.):
                result = warp_image(incoming, source_bounds, cage, placement, destination_bounds, cancelled, pixel_scale=pixel_scale)
                if result is None:
                    return None
                return result[0]
            asynchronous = self._interactive_render and not self._render_modifier_sources and output_bounds.width()*output_bounds.height() > 128*128
            from comic_editor.ui.gpu_textures import renderer_for
            gpu = renderer_for(self)
            warped = gpu.cage(incoming, source_bounds, cage, placement, destination_bounds) if gpu is not None else None
            if warped is not None:
                self._modifier_cache_put(cache_key, warped)
            elif asynchronous and self._effect_jobs.request(("cage-tool", key), cache_key, compute,
                    10*int(image.sizeInBytes())+16*math.ceil(output_bounds.width()*output_bounds.height())):
                draft_key = ("cage-draft", cache_key)
                warped = self._modifier_cache_get(draft_key)
                if warped is None:
                    warped = compute(pixel_scale=min(1., 192/max(output_bounds.width(), output_bounds.height())))
                    self._modifier_cache_put(draft_key, warped)
            else:
                warped = compute()
                self._modifier_cache_put(cache_key, warped)
        painter.save()
        painter.drawImage(output_bounds, warped)
        painter.restore()
        return True
