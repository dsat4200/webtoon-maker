"""Typed source packets and guarded normal-batch filtered feedback replacement."""
from dataclasses import replace
import copy
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
        self.presented_basis = None
        self.presented_basis_bytes = 0
        self.provisional = False
        self.provisional_source_completed_revision = prepared.document.revision
        self.provisional_contact_started = False
        self.provisional_change = None
        self.presented_request_generation = -1
        self.presented_selection_generation = -1
        self.selection_handoff = None

    def _object_contract(self, identifier):
        canvas = self.canvas
        obj = canvas.chapter.objects.get(identifier) if canvas.chapter is not None else None
        if obj is None:
            return None
        return (id(obj), obj.to_dict(),
            tuple((mid, canvas.chapter.modifiers[mid].to_dict()) for mid in obj.modifier_ids
                if mid in canvas.chapter.modifiers))

    def _selected_contract(self):
        canvas = self.canvas
        contract = self._object_contract(self.prepared.identifier)
        entities = tuple(getattr(canvas, 'selected_entities', ()))
        if (contract is None or canvas.selected_kind != 'object' or canvas.selected_id != self.prepared.identifier
                or len(entities) > 1 or entities and entities != (('object', self.prepared.identifier),)):
            return None
        return (canvas.selected_kind, canvas.selected_id, entities, *contract)

    def remember_presented(self, document, requests, base, batch, view):
        """Retain metadata only after a complete native view actually swapped."""
        if (self.provisional or self.error or self.generations
                or document.live_preview or document != self.canvas._render_document_state()
                or self.completed_revision != document.revision
                or base is None or base[2] != document.revision
                or not self.native_batch(batch)
                or self._native_base(document, requests, base, self.prepared.document.revision) is None
                or tuple((tile.key, tile.image.cacheKey()) for _phase, tiles in batch for tile in tiles)
                    != tuple((tile.key, tile.image.cacheKey()) for _phase, tiles in base[1] for tile in tiles)):
            return False
        context_getter = getattr(self.canvas, 'context', None)
        context = context_getter() if callable(context_getter) else None
        if not self._context_current(context):
            return False
        source = self.canvas.tiles._tiles.get(self.prepared.identifier)
        selected = self._selected_contract()
        if source is None or len(source) > 128 or selected is None:
            return False
        retained = (base[0], tuple((phase, tuple(tiles)) for phase, tiles in base[1]), base[2])
        size = self._presentation_storage_bytes(retained[1])
        if size is None:
            return False
        generations = {key: source.version(key) for key in source}
        self.presented_basis = (document, retained, tuple(requests), view, context,
            source, generations, copy.deepcopy(selected),
            getattr(self.canvas, '_history_generation', 0),
            getattr(self.canvas, '_graphics_worker', None))
        self.presented_basis_bytes = size
        controller = self.canvas._scene_controller
        self.presented_request_generation = controller._feedback_request_generation
        self.presented_selection_generation = controller._feedback_selection_generation
        return True

    def begin_selection_handoff(self, view):
        """Arm one synchronous sibling selection, without replaying its scene."""
        from comic_editor.core.models import RasterObject
        canvas, controller = self.canvas, self.canvas._scene_controller
        basis = self.presented_basis
        sibling = canvas.chapter.objects.get(canvas.selected_id) if canvas.chapter is not None else None
        original = canvas.chapter.objects.get(self.prepared.identifier) if canvas.chapter is not None else None
        if (basis is None or self.provisional or self.selection_handoff is not None
                or self.worker.busy or self.generations
                or controller.feedback_target != self.prepared.identifier
                or controller._feedback_request_generation != self.presented_request_generation
                or controller._feedback_selection_generation != self.presented_selection_generation + 1
                or canvas.selected_kind != 'object'
                or tuple(canvas.selected_entities) != (('object', canvas.selected_id),)
                or not isinstance(sibling, RasterObject) or original is None
                or sibling.object_id == self.prepared.identifier
                or sibling.parent_layer_id != original.parent_layer_id
                or getattr(canvas, '_raster_contact_active', False) or getattr(canvas, '_drawing', False)
                or canvas._native_input_predecessor() is not None):
            return False
        self.selection_handoff = {
            'identifier': sibling.object_id,
            'contract': copy.deepcopy(self._object_contract(sibling.object_id)),
            'request_generation': controller._feedback_request_generation,
            'selection_generation': controller._feedback_selection_generation,
            'history_revision': canvas.command_stack.revision,
            'change': None, 'committed': False,
        }
        if not self._basis_current(canvas._render_document_state(), view, allow_handoff=True):
            self.selection_handoff = None
            return False
        return True

    def _handoff_selection_current(self):
        handoff, canvas = self.selection_handoff, self.canvas
        controller = canvas._scene_controller
        return (handoff is not None
            and controller.feedback_target == self.prepared.identifier
            and controller._feedback_request_generation == handoff['request_generation']
            and controller._feedback_selection_generation == handoff['selection_generation']
            and canvas.selected_kind == 'object' and canvas.selected_id == handoff['identifier']
            and tuple(canvas.selected_entities) == (('object', handoff['identifier']),)
            and not getattr(canvas, '_raster_contact_active', False) and not getattr(canvas, '_drawing', False)
            and canvas._native_input_predecessor() is None)

    def finish_selection_handoff(self, view):
        """Resolve only the exact return after one committed scalar command."""
        handoff, canvas = self.selection_handoff, self.canvas
        controller = canvas._scene_controller
        if (handoff is None or not self.provisional or not handoff['committed']
                or controller.feedback_target != self.prepared.identifier
                or controller._feedback_request_generation != handoff['request_generation']
                or controller._feedback_selection_generation != handoff['selection_generation'] + 1
                or canvas.command_stack.revision != handoff['history_revision'] + 1
                or self._object_contract(handoff['identifier']) != handoff['contract']
                or getattr(canvas, '_raster_contact_active', False) or getattr(canvas, '_drawing', False)
                or canvas._native_input_predecessor() is not None
                or not self._basis_current(canvas._render_document_state(), view)):
            return False
        self.selection_handoff = None
        return True

    def _presentation_storage_bytes(self, batch):
        """Charge native COW/borrowed storage to the existing presentation cap."""
        from comic_editor.render.device import DeviceImage
        continuity = self.canvas._scene_controller.raster_continuity
        retained = [tile.image for _phase, tiles in batch for tile in tiles]
        other = [tile.image for _phase, tiles in continuity.batch for tile in tiles]
        other.extend(tile.image for tile, _replace in continuity.overlays.values())
        images = retained + other
        if any(not isinstance(image, (QImage, DeviceImage)) or image.isNull() for image in images):
            return None
        def native_storage(image):
            if isinstance(image, QImage):
                return (QImage, image.cacheKey()), image.sizeInBytes()
            # Cropped DeviceImage views have distinct cache keys but retain
            # the same complete producer allocation through their shared lease.
            return ((DeviceImage, id(image.owner), image.token),
                image.texture_width * image.texture_height * 16)
        storage = dict(native_storage(image) for image in images)
        if sum(storage.values()) > continuity.budget:
            return None
        return sum(dict(native_storage(image) for image in retained).values())

    def park_parameter_change(self, change, action, view):
        """Keep one previously displayed scene as an explicitly older draft.

        Only a sibling Blur scalar transaction can park this owner. Its old
        suffix is never declared current: ordinary work still evaluates the new
        effect and selected native packets replay against the old scene only.
        """
        from comic_editor.core.models import BlurModifier, RasterObject
        basis = self.presented_basis
        if (basis is None or action not in {'transient', 'push'}
                or change.conservative or change.document_fields or change.orders
                or change.resources or len(change.entities) != 1):
            return False
        item = change.entities[0]
        kind, identifier = item.entity
        if (kind != 'modifier' or item.structural or not item.fields
                or not item.fields <= {'strength', 'intensity'}):
            return False
        canvas = self.canvas
        if (canvas.chapter is None or self.prepared.identifier not in canvas.chapter.objects
                or not self.provisional and (getattr(canvas, '_raster_contact_active', False)
                    or getattr(canvas, '_drawing', False))):
            return False
        modifier = canvas.chapter.modifiers.get(identifier)
        owners = [obj for obj in canvas.chapter.objects.values() if identifier in obj.modifier_ids]
        if (not isinstance(modifier, BlurModifier) or modifier.parameter_masks
                or len(owners) != 1 or not isinstance(owners[0], RasterObject)
                or owners[0].object_id == self.prepared.identifier
                or owners[0].parent_layer_id != canvas.chapter.objects[self.prepared.identifier].parent_layer_id
                or any(identifier in layer.modifier_ids for layer in canvas.chapter.layers.values())):
            return False
        index = getattr(canvas, '_change_index', None)
        if index is None or index.chapter is not canvas.chapter:
            return False
        selected = ('object', self.prepared.identifier)
        if selected not in index.dependencies:
            return False
        pending, dependencies = [selected], set()
        while pending:
            ref = pending.pop()
            if ref in dependencies:
                continue
            dependencies.add(ref)
            if len(dependencies) > 4096:
                return False
            pending.extend(index.dependencies.get(ref, ()))
        if (('modifier', identifier) in dependencies
                or ('object', owners[0].object_id) in dependencies):
            return False
        current_change = (identifier, item.fields, tuple((field, getattr(modifier, field))
            for field in sorted(item.fields)))
        if self.provisional and current_change != self.provisional_change:
            return False
        handoff = self.selection_handoff
        if handoff is not None:
            if (not self._handoff_selection_current() or owners[0].object_id != handoff['identifier']
                    or handoff['committed']
                    or canvas.command_stack.revision != handoff['history_revision'] + (action == 'push')
                    or handoff['change'] is not None and handoff['change'] != current_change):
                return False
            expected = copy.deepcopy(handoff['contract'])
            records = dict(expected[2])
            if identifier not in records:
                return False
            records[identifier].update(current_change[2])
            if self._object_contract(handoff['identifier']) != expected:
                return False
            handoff['contract'] = expected
            handoff['change'] = current_change
        if not self._basis_current(canvas._render_document_state(), view, allow_handoff=handoff is not None):
            return False
        if handoff is not None:
            handoff['committed'] = action == 'push'
        self.provisional = True
        self.provisional_change = current_change
        self.provisional_source_completed_revision = self.completed_revision
        # Keep completed_revision tied to the old whole-scene basis. Source
        # completions have their own field and cannot relabel the old suffix.
        self.completed_revision = basis[0].revision
        return True

    def _basis_current(self, document, view, *, allow_handoff=False):
        basis = self.presented_basis
        if basis is None or self.error or self.worker.stopped.is_set():
            return False
        old, base, _requests, old_view, context, source, generations, selected, history, graphics = basis
        canvas = self.canvas
        if (document.identity != old.identity or document.configuration != old.configuration
                or document.pixel_contract != old.pixel_contract or document.bounds != old.bounds
                or document.revision < old.revision or view != old_view
                or not self._context_current(context)
                or getattr(canvas, '_graphics_worker', None) is not graphics
                or getattr(canvas, '_history_generation', 0) != history
                or self._object_contract(self.prepared.identifier) != selected[3:]
                or self._selected_contract() != selected and not (
                    allow_handoff and self._handoff_selection_current())
                or canvas.tiles._tiles.get(self.prepared.identifier) is not source
                or canvas._projection_has_live_preview(include_ink=False)
                or self._presentation_storage_bytes(base[1]) is None
                or len(source) > 128 or set(source.entries) != set(generations)
                or any(source.version(key) != generation for key, generation in generations.items())):
            return False
        return True

    def _context_current(self, context):
        try:
            getter = getattr(self.canvas, 'context', None)
            return (context is not None and callable(getter) and getter() is context
                and context.isValid() and self.canvas.isValid())
        except RuntimeError:
            return False  # A replaced native Qt context cannot retain a basis.

    def provisional_valid(self, document, view):
        return self.provisional and self._basis_current(document, view)

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
            if self.provisional and self.presented_basis is not None:
                # The same complete typed journal guards both worker packets
                # and the parked source owner, including transparent pruning.
                expected = self.presented_basis[6]
                if item.address in self.canvas.tiles._tiles[self.prepared.identifier].entries:
                    expected[item.address] = item.new_generation
                else:
                    expected.pop(item.address, None)
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
            if self.provisional:
                self.provisional_source_completed_revision = completion.revision
            else:
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
        if (self.provisional or self.error or self.worker.stopped.is_set() or document.configuration != prepared.document.configuration
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
        return self._replace_native_batch(batch)

    def provisional_batch(self, document, requests, view):
        """Replay complete native tiles from the positively presented old view."""
        if not self.provisional_valid(document, view) or self.dirty.isEmpty():
            return None, 0
        old, base = self.presented_basis[:2]
        tiles = self._native_base(old, requests, base, self.prepared.document.revision)
        if tiles is None:
            return None, 0
        for request in requests:
            if request.capture_rect.intersects(self.dirty) and not any(
                    recipe.request.bounds.contains(request.capture_rect)
                    and recipe.request.requested.contains(request.capture_rect)
                    for recipe in self.prepared.recipes):
                return None, 0
        return self._replace_native_batch(base[1])

    def _replace_native_batch(self, batch):
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
        self.presented_basis = None
        self.presented_basis_bytes = 0
        self.selection_handoff = None
