"""One current, provisional committed frame before ordinary native tiles.

An actual typed reveal or an owned geometry release can arm this view. It uses
a fresh complete ordinary snapshot and the existing isolated scene evaluator.
It owns no exact cache entries and never changes the real revision/live status.
"""
from dataclasses import dataclass
import math

from comic_editor.core.changes import KIND_GROUPS
from comic_editor.core.commands import CallbackCommand
from comic_editor.render.geometry_capture import gesture
from comic_editor.render.scene import native_source_key


@dataclass
class GeometryRelease:
    before: object
    refs: frozenset
    document: object
    context: tuple
    history_revision: int
    prior_command: object
    changes: list


class VisibilityPresentation:
    def __init__(self, controller):
        self.controller = controller
        self.generation = 0
        self.native_views = []
        self.native_seen = None
        self.clear()

    def clear(self, *, native=False):
        if native:
            self.native_views.clear()
            self.native_seen = None
        preview = getattr(self, 'preview', None)
        if preview is not None and self.controller.preview is preview:
            self.controller.preview = None
            self.controller._preview_snapshot = self.controller._preview_request = None
        self.change = self.owner = self.preview = None
        self.records = ()
        self.geometry_records = ()
        self.release = self.release_context = None
        self.purpose = 'visibility'
        self.submitted = False

    def begin_geometry_release(self):
        """Called only by the actual release without a shown geometry frame."""
        from comic_editor.ui.geometry_presentation_continuity import context
        canvas = self.controller.canvas
        current = gesture(canvas)
        self.clear()
        if current is None:
            return
        document = canvas._render_document_state()
        if (not document.live_preview or document.contact_only
                or document.pixel_contract.floating or document.overflow
                or document.underlay != ('', 0.)):
            return
        self.release = GeometryRelease(current[0], current[2], document, context(canvas),
            canvas.command_stack.revision, canvas.command_stack.top_undo_command, [])

    def finish_geometry_release(self):
        from comic_editor.ui.geometry_presentation_continuity import context
        canvas, release = self.controller.canvas, self.release
        self.release = None
        if release is None:
            return
        command = canvas.command_stack.top_undo_command
        if (len(release.changes) != 1 or not isinstance(command, CallbackCommand)
                or command is release.prior_command
                or command.forward_change is not release.changes[0]
                or canvas._last_published_change is not release.changes[0]
                or canvas.command_stack.revision != release.history_revision + 1
                or canvas._model_before is not None or canvas._projection_has_live_preview()
                or context(canvas) != release.context):
            return
        document = canvas._render_document_state()
        if (document.live_preview or document.contact_only
                or document.identity != release.document.identity
                or document.configuration != release.document.configuration
                or document.pixel_contract != release.document.pixel_contract
                or document.revision < release.document.revision):
            return
        records = []
        for item in release.changes[0].entities:
            group = KIND_GROUPS.get(item.entity[0])
            record = getattr(canvas.chapter, group, {}).get(item.entity[1]) if group else None
            if record is None:
                return
            records.append((item.entity, record))
        self.change, self.geometry_records = release.changes[0], tuple(records)
        self.purpose = 'geometry-release'
        self.release_context = (document, release.context, canvas.command_stack.revision,
            command, self.controller._presentation_dependency_generation,
            id(getattr(canvas, '_graphics_worker', None)))
        self.generation += 1

    def changed(self, change, action):
        release = self.release
        visibility_only = (action == 'commit' and not change.transient and not change.conservative
            and not change.resources and not change.orders and not change.document_fields
            and 1 <= len(change.entities) <= 128
            and all(not item.structural and item.fields == frozenset({'visible'})
                    for item in change.entities))
        if not visibility_only:
            self.native_views.clear()
            self.native_seen = None
        self.clear()
        if release is not None:
            if (action == 'push' and not change.transient and not change.conservative
                    and not change.resources and not change.orders and not change.document_fields
                    and 1 <= len(change.entities) <= 128
                    and all(not item.structural and item.entity in release.refs
                            for item in change.entities)):
                release.changes.append(change)
                self.release = release
            return
        if not visibility_only:
            return
        chapter = self.controller.canvas.chapter
        records = []
        for item in change.entities:
            group = KIND_GROUPS.get(item.entity[0])
            record = getattr(chapter, group, {}).get(item.entity[1]) if group else None
            if record is None or type(getattr(record, 'visible', None)) is not bool:
                return
            records.append((item.entity, record, record.visible))
        if not any(value for _ref, _record, value in records) and not self._large_hidden_subtree(chapter, records):
            return  # Small hides keep the ordinary fast native path.
        self.change, self.records = change, tuple(records)
        self.generation += 1

    @staticmethod
    def _large_hidden_subtree(chapter, records):
        # Optional scheduling choice only. Stop as soon as 32 descendants are
        # known; never scan sources/effects or define a new render/cache identity.
        pending = [ref for ref, _record, value in records if not value and ref[0] == 'layer']
        seen = set()
        while pending and len(seen) <= 32:
            ref = pending.pop()
            if ref in seen:
                continue
            group = KIND_GROUPS.get(ref[0])
            record = getattr(chapter, group, {}).get(ref[1]) if group else None
            if record is None:
                return False
            seen.add(ref)
            if ref[0] == 'layer':
                pending.extend((child.kind, child.entity_id) for child in record.children[:33])
        return len(seen) > 32

    def _native_view(self, document, addresses, phases):
        """Small scheduling hint, never a render key or a source of pixels."""
        from comic_editor.ui.geometry_presentation_continuity import context
        canvas = self.controller.canvas
        if (document.live_preview or document.contact_only or document.pixel_contract.floating
                or document.overflow or document.underlay != ('', 0.)
                or document.identity != (id(canvas.chapter), id(canvas.tiles), id(canvas.images))
                or document.revision != canvas._document_projection.revision
                or phases not in ((None,), ('base', 'top'))
                or not 1 <= len(addresses) <= 256
                or any(address.level != 0 for address in addresses)
                or sum(len(getattr(canvas.chapter, group)) for group in KIND_GROUPS.values()) > 512):
            return None
        visibility = tuple((group, identifier, record.visible)
            for group in KIND_GROUPS.values()
            for identifier, record in getattr(canvas.chapter, group).items()
            if type(getattr(record, 'visible', None)) is bool)
        return (document.identity, document.configuration, document.pixel_contract,
            tuple(document.bounds.getRect()), addresses, phases, context(canvas),
            canvas.command_stack.revision, canvas._scene_snapshot_compiler.source_generation, visibility)

    def native_ready(self, document, requests, phases):
        from comic_editor.ui.geometry_presentation_continuity import context
        canvas = self.controller.canvas
        if document.live_preview or document.contact_only:
            return
        addresses, phases = tuple(request.address for request in requests), tuple(phases)
        seen = (document, addresses, phases, context(canvas), canvas.command_stack.revision,
            canvas._scene_snapshot_compiler.source_generation)
        if seen == self.native_seen:
            return  # Hover/repaints do not rebuild unchanged visibility metadata.
        key = self._native_view(document, addresses, phases)
        self.native_seen = seen if key is not None else None
        if key is not None:
            # Only the last two visibility states of this unchanged scene/view.
            # No images, source snapshots, cache keys or worker caches are held.
            if key in self.native_views:
                self.native_views.remove(key)
            self.native_views.append(key)
            del self.native_views[:-2]

    def _release_context_matches(self, document):
        from comic_editor.ui.geometry_presentation_continuity import context
        canvas = self.controller.canvas
        captured = self.release_context
        return (captured is not None and document == captured[0]
            and context(canvas) == captured[1]
            and canvas.command_stack.revision == captured[2]
            and canvas.command_stack.top_undo_command is captured[3]
            and self.controller._presentation_dependency_generation == captured[4]
            and id(getattr(canvas, '_graphics_worker', None)) == captured[5])

    def waiting_for_first_request(self):
        canvas = self.controller.canvas
        return (self.release_context is not None and self.owner is None
            and self.change is not None and canvas._last_published_change is self.change
            and self._release_context_matches(canvas._render_document_state()))

    def key(self, document, addresses, phases, visible):
        canvas = self.controller.canvas
        if (self.change is None or canvas._last_published_change is not self.change
                or document.live_preview or document.contact_only
                or document.pixel_contract.floating or document.overflow
                or document.underlay != ('', 0.)
                or document.identity != (id(canvas.chapter), id(canvas.tiles), id(canvas.images))
                or document.revision != canvas._document_projection.revision
                or phases not in ((None,), ('base', 'top'))
                or not 1 <= len(addresses) <= 256
                or any(address.level != 0 for address in addresses)
                or not all(math.isfinite(value) for value in visible)
                or visible[2] <= 0 or visible[3] <= 0):
            return None
        if self.release_context is not None:
            if not self._release_context_matches(document):
                return None
        for ref, record in self.geometry_records:
            if getattr(canvas.chapter, KIND_GROUPS[ref[0]]).get(ref[1]) is not record:
                return None
        for ref, record, value in self.records:
            if (getattr(canvas.chapter, KIND_GROUPS[ref[0]]).get(ref[1]) is not record
                    or record.visible is not value):
                return None
        view = canvas.visible_document_rect().intersected(document.bounds)
        if tuple(view.getRect()) != visible:
            return None
        camera = canvas.camera_transform()
        return (document, addresses, phases, visible,
            tuple(getattr(camera, f'm{i}{j}')() for i in range(1, 4) for j in range(1, 4)),
            canvas.width(), canvas.height(), canvas.devicePixelRatioF(),
            canvas.selected_kind, canvas.selected_id, tuple(canvas.selected_entities), canvas.tool,
            canvas.command_stack.revision, getattr(canvas, '_history_generation', 0),
            native_source_key(canvas.tiles, document.identity, canvas._scene_snapshot_compiler.source_generation),
            self.controller._presentation_dependency_generation, id(getattr(canvas, '_graphics_worker', None)))

    def request(self, document, addresses, phases, visible):
        key = self.key(document, addresses, phases, visible)
        if key is None or self.owner is not None and self.owner[1] != key:
            self.clear()
            return False
        if (self.owner is None and self.purpose == 'visibility'
                and self._native_view(document, addresses, phases) in self.native_views):
            # A recently ready native state normally has the faster ordinary
            # path. That path still validates every actual semantic dependency;
            # eviction simply falls back to ordinary rendering, never old pixels.
            self.clear()
            return False
        if self.owner is None:
            self.owner = self.generation, key
        return not self.submitted

    def accept(self, completion, document, addresses, phases, visible):
        demand, result = completion.demand, completion.preview
        if (self.owner is None or demand.visibility_preview_owner != self.owner
                or self.key(document, addresses, phases, visible) != self.owner[1]
                or demand.serial != self.controller.serial or demand.snapshot.document != document
                or not self.controller.scheduler._visibility_preview_bounded(demand)
                or not completion.done or completion.error or result is None
                or completion.tiles or completion.materialized is not None
                or completion.overview is not None or completion.feedback is not None
                or completion.recorded or completion.recorded_rows or completion.cache_manifest is not None
                or result.document != document or result.exact or result.error
                or result.status.value != 'provisional' or result.image.isNull()
                or result.request.key != ('detached-preview', demand.serial)
                or result.request.quality.value != 'interactive'
                or result.request.revision != document.revision
                or tuple(result.request.bounds.getRect()) != visible
                or result.request.pixel_size != demand.preview_size
                or result.request.requested_region is not None or not result.request.clip_document
                or result.request.scale != min(1., max(demand.preview_size[0]/visible[2], demand.preview_size[1]/visible[3]))
                or result.request.output_transform != (demand.preview_size[0]/visible[2], 0., 0.,
                    demand.preview_size[1]/visible[3], -visible[0]*(demand.preview_size[0]/visible[2]),
                    -visible[1]*(demand.preview_size[1]/visible[3]))
                or result.request.phase is not None or result.request.target is not None
                or result.request.smooth_sources or result.request.defer_effects
                or result.image.devicePixelRatio() != 1.
                or result.image.format() != document.pixel_contract.image_format
                or (result.image.width(), result.image.height()) != demand.preview_size):
            return False
        from comic_editor.ui.document_presentation import PresentedTile
        self.controller._preview_snapshot, self.controller._preview_request = demand.snapshot, result.request
        self.preview = document, PresentedTile((self.purpose + '-preview', demand.serial), result.image,
            result.request.bounds, pixel_contract=document.pixel_contract,
            pixel_environment=demand.snapshot.pixel_environment)
        self.controller.preview = self.preview
        self.controller.dispatched = None  # Ordinary exact work follows immediately.
        return True

    def for_request(self, signature):
        if self.preview is None or self.owner is None:
            return None
        if signature is None or self.key(*signature) != self.owner[1]:
            self.clear()
            return None
        return self.preview
