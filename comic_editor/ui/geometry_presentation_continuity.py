"""Keep an actually presented, dated geometry frame during its own commit.

This is presentation continuity, not a proof that preview pixels equal the
committed native render. The old document/revision remains attached to the
image until ordinary current artwork replaces it. No pixels enter a cache.
"""
from dataclasses import dataclass

from PySide6.QtGui import QImage

from comic_editor.core.commands import CallbackCommand
from comic_editor.render.geometry_capture import GeometrySceneSnapshot, gesture
from comic_editor.render.scene import native_source_key
from comic_editor.ui.release_handoff import view_token


def context(canvas):
    return (view_token(canvas), canvas.selected_kind, canvas.selected_id,
            tuple(canvas.selected_entities), canvas.tool,
            getattr(canvas, '_history_generation', 0),
            native_source_key(canvas.tiles,
                (id(canvas.chapter), id(canvas.tiles), id(canvas.images)), 0))


@dataclass
class PaintedGeometry:
    preview: tuple
    owner: tuple
    signature_tail: tuple
    context: tuple
    swapped: bool


@dataclass
class PendingCommit:
    painted: PaintedGeometry
    before: object
    refs: frozenset
    history_revision: int
    prior_command: object
    changes: list
    document: object = None


class GeometryPresentationContinuity:
    """One last shown frame, admitted only by an explicit owned release."""
    def __init__(self, controller):
        self.controller = controller
        self.last = self.pending_paint = self.armed = self.ticket = None

    def clear(self):
        self.last = self.pending_paint = self.armed = self.ticket = None

    def painted(self, document, batch, *, owned, swapped_required):
        controller = self.controller
        preview = controller.preview
        snapshot = controller._preview_snapshot
        signature = controller.desired
        if (not owned or signature is None or signature[0] != document
                or preview is None or not isinstance(snapshot, GeometrySceneSnapshot)
                or snapshot.document != preview[0]
                or not controller._current_geometry_snapshot(snapshot, *signature)
                or len(batch) != 1 or batch[0][0] is not None
                or len(batch[0][1]) != 1 or batch[0][1][0] is not preview[1]
                or not isinstance(preview[1].image, QImage)
                or preview[1].image.isNull()
                or preview[1].image.sizeInBytes() > controller.canvas._document_projection.budget // 4):
            self.pending_paint = None
            return
        self.pending_paint = PaintedGeometry(preview, controller._geometry_preview_owner,
            signature[1:], context(controller.canvas), not swapped_required)
        if not swapped_required:
            self.last = self.pending_paint

    def swapped(self):
        value = self.pending_paint
        controller = self.controller
        if (value is not None and controller.preview is value.preview
                and controller._geometry_preview_owner == value.owner
                and context(controller.canvas) == value.context):
            value.swapped = True
            self.last = value
        self.pending_paint = None

    def begin(self):
        controller, canvas, last = self.controller, self.controller.canvas, self.last
        current = gesture(canvas)
        self.armed = self.ticket = None
        if (last is None or not last.swapped or current is None
                or controller.preview is not last.preview
                or controller._geometry_preview_owner != last.owner
                or controller.desired is None or controller.desired[1:] != last.signature_tail
                or controller._geometry_preview_key_for(canvas._render_document_state(),
                    *last.signature_tail) != last.owner[1]
                or context(canvas) != last.context):
            controller.visibility_presentation.begin_geometry_release()
            return None
        self.armed = PendingCommit(last, current[0], current[2], canvas.command_stack.revision,
            canvas.command_stack.top_undo_command, [])
        return self.armed

    def changed(self, change, action):
        armed = self.armed
        if armed is None:
            if self.ticket is not None:
                self.clear()
            return
        if (action != 'push' or change.transient or change.conservative
                or change.resources or change.orders or change.document_fields
                or not change.entities or any(item.structural or item.entity not in armed.refs
                    for item in change.entities)):
            self.clear()
            return
        armed.changes.append(change)

    def finish(self, ticket):
        self.controller.visibility_presentation.finish_geometry_release()
        canvas = self.controller.canvas
        command = canvas.command_stack.top_undo_command
        if (ticket is None or self.armed is not ticket or len(ticket.changes) != 1
                or not isinstance(command, CallbackCommand) or command is ticket.prior_command
                or command.forward_change is not ticket.changes[0]
                or canvas.command_stack.revision != ticket.history_revision + 1
                or canvas._model_before is not None or canvas._projection_has_live_preview()
                or context(canvas) != ticket.painted.context):
            self.clear()
            return
        document = canvas._render_document_state()
        older = ticket.painted.preview[0]
        if (document.live_preview or document.identity != older.identity
                or document.configuration != older.configuration
                or document.pixel_contract != older.pixel_contract
                or document.revision < older.revision):
            self.clear()
            return
        ticket.document = document
        # Retain only the existing presented image and bounded commit witness;
        # never retain a complete source snapshot or relabel its revision.
        ticket.before = None
        self.armed, self.ticket = None, ticket
        self.last = self.pending_paint = None

    def for_request(self, signature):
        ticket = self.ticket
        if ticket is None:
            return None
        canvas = self.controller.canvas
        if (signature[0] != ticket.document or signature[1:] != ticket.painted.signature_tail
                or canvas.command_stack.revision != ticket.history_revision + 1
                or canvas._projection_has_live_preview()
                or context(canvas) != ticket.painted.context
                or ticket.painted.preview[1].image.isNull()):
            self.clear()
            return None
        return ticket.painted.preview
