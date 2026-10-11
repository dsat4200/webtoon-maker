"""Typed source packets and guarded normal-batch filtered feedback replacement."""
from dataclasses import replace
import math

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage

from comic_editor.render.filtered_feedback_worker import (FilteredFeedbackWorker,
    capture_filtered_contact_packet)
from comic_editor.render.filtered_raster_feedback import FeedbackUnavailable
from comic_editor.render.projection import ProjectionAddress, ProjectionRequest


class FilteredRasterFeedbackState:
    def __init__(self, canvas, prepared, notify):
        self.canvas, self.prepared = canvas, prepared
        self.worker = FilteredFeedbackWorker(prepared.snapshot,prepared.identifier,
            prepared.recipes,notify=notify)
        self.generations = {}
        self.pending_dirty = QRectF()
        self.dirty = QRectF()
        self.sequence = 0
        self.revision = prepared.document.revision
        self.completed_revision = prepared.document.revision
        self.base_revision = prepared.document.revision
        self.updates = {}
        self.crops = {}
        self.crop_bytes = 0
        self.error = ''

    def changed(self, change):
        influence = QRectF()
        for item in change.entities:
            if not item.affects_artwork:
                continue
            if item.old_bounds is None or item.new_bounds is None:
                raise FeedbackUnavailable('Filtered contact has no complete typed influence')
            for value in (item.old_bounds,item.new_bounds):
                if not all(math.isfinite(coordinate) for coordinate in value):
                    raise FeedbackUnavailable('Filtered contact influence is nonfinite')
                influence = influence.united(QRectF(*value))
        for item in change.resources:
            if item.address is None or item.new_generation is None:
                raise FeedbackUnavailable('Filtered contact has no owned source generation')
            self.generations[item.address] = item.new_generation
        if change.resources and influence.isEmpty():
            raise FeedbackUnavailable('Filtered source changes have unknown coverage')
        self.pending_dirty = self.pending_dirty.united(influence)
        self.dirty = self.dirty.united(influence)

    def advance(self, document, *, capture=True):
        if self.error:
            return False
        # A completed job must not launch an older queued source before
        # current typed changes are merged. Timer-only polls may publish
        # the completion, leaving dirty pending work for the next capture.
        if capture and self.generations:
            obj = self.canvas.chapter.objects.get(self.prepared.identifier)
            if obj is None:
                return False
            try:
                packet = capture_filtered_contact_packet(self.canvas.tiles,obj.object_id,
                    self.generations,self.pending_dirty,tuple(obj.interaction_rect),
                    self.sequence+1,document.revision)
                if not self.worker.submit(packet):
                    return False
            except FeedbackUnavailable as error:
                self.error = str(error)
                return False
            self.sequence += 1
            self.revision = document.revision
            self.generations.clear()
            self.pending_dirty = QRectF()
        completion = self.worker.poll(start_pending=capture or not self.generations)
        if self.worker.error:
            self.error = self.worker.error
            return False
        if completion is not None:
            self.completed_revision = completion.revision
            for index,update in completion.updates:
                self.updates[index] = update
                for key in tuple(self.crops):
                    if key[0] == index:
                        self.crop_bytes -= self.crops.pop(key).sizeInBytes()
            self.canvas.update()
        return True

    @staticmethod
    def _native_base(document, requests, base, prepared_revision):
        if (base is None or base[0] != document.configuration or base[2] < prepared_revision
                or tuple(phase for phase,_tiles in base[1]) != (None,)):
            return None
        tiles = {}
        for tile in base[1][0][1]:
            if (not isinstance(tile.key,tuple) or len(tile.key)!=2 or tile.key[0] is not None
                    or not isinstance(tile.key[1],ProjectionAddress)):
                return None
            address = tile.key[1]
            request = ProjectionRequest(address)
            if (address.level!=0 or tile.world_rect != request.world_rect
                    or tile.source_rect != request.source_rect or tile.image.isNull()
                    or tile.image.width()!=request.pixel_size or tile.image.height()!=request.pixel_size):
                return None
            tiles[address] = tile
        return tiles if all(request.address in tiles for request in requests) else None

    def covers(self, document, requests, base):
        prepared = self.prepared
        if (self.error or self.worker.stopped.is_set() or document.configuration != prepared.document.configuration
                or document.identity != prepared.document.identity):
            return False
        tiles = self._native_base(document,requests,base,prepared.document.revision)
        if tiles is None:
            return False
        dirty = self.dirty.intersected(document.bounds)
        if dirty.isEmpty():
            return True
        for request in requests:
            if not request.capture_rect.intersects(dirty):
                continue
            if not any(recipe.request.bounds.contains(request.capture_rect)
                    and recipe.request.requested.contains(request.capture_rect)
                    for recipe in prepared.recipes):
                return False
        return True

    @staticmethod
    def native_batch(batch):
        return (tuple(phase for phase,_tiles in batch) == (None,)
            and all(isinstance(tile.key,tuple) and len(tile.key)==2
                and tile.key[0] is None and isinstance(tile.key[1],ProjectionAddress)
                for _phase,tiles in batch for tile in tiles))

    def replace_batch(self, document, requests, base, batch):
        """Draw each complete scene tile once, including erased transparency."""
        if (self.dirty.isEmpty() or not self.covers(document,requests,base)
                or not self.native_batch(batch)):
            return batch,0
        output = []
        replaced = 0
        for tile in batch[0][1]:
            if not tile.world_rect.adjusted(-2.,-2.,2.,2.).intersects(self.dirty):
                output.append(tile)
                continue
            replacement = None
            for index,update in self.updates.items():
                if update.revision < self.base_revision:
                    continue
                recipe = self.prepared.recipes[index]
                request = ProjectionRequest(tile.key[1])
                if not recipe.request.requested.contains(request.capture_rect):
                    continue
                key = index,request.address
                crop = self.crops.get(key)
                if crop is None:
                    rectangle = request.capture_rect.translated(-recipe.request.bounds.topLeft()).toAlignedRect()
                    crop = update.image.copy(rectangle)
                    if crop.isNull() or crop.sizeInBytes()+self.crop_bytes > 32*1024*1024:
                        self.error = 'Filtered presentation exceeded its crop byte budget'
                        return batch,0
                    self.crops[key] = crop
                    self.crop_bytes += crop.sizeInBytes()
                replacement = replace(tile,image=crop)
                break
            output.append(tile if replacement is None else replacement)
            replaced += replacement is not None
        return [(None,output)],replaced

    def checkpoint(self, revision):
        self.base_revision = max(self.base_revision,revision)
        self.dirty = QRectF()
        self.updates.clear()
        self.crops.clear()
        self.crop_bytes = 0

    def close(self):
        self.worker.close()
        self.generations.clear()
        self.updates.clear()
        self.crops.clear()
        self.crop_bytes = 0
