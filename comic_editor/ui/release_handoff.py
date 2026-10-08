"""PROPOSED, UNRUN: checked presentation-only ordinary Image release handoff.

No renderer, cache, readback, or document mutation lives here. Every admission
is optional and bounded; rejection keeps the existing pending presentation.
"""
from copy import copy, deepcopy
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QPointF
from PySide6.QtGui import QImage, QPainterPath, QPolygonF, QTransform

from comic_editor.core.commands import CallbackCommand
from comic_editor.core.document_patch import RecordSnapshot
from comic_editor.core.models import ImageObject
from comic_editor.core.tiles import TileStore
from comic_editor.render.device import DeviceImage
from comic_editor.render.scene import SCENE_FIELDS, EvaluatedScene
from comic_editor.render.service import RenderQuality
from comic_editor.ui.attached_translation import (
    ownership, effective_preview_mask, translate_mask, translation,
)
from comic_editor.ui.transform_modifier_preview import (
    effective_preview_modifier, transform_modifier_rig,
)


class Rejected(Exception):
    pass


class Budget:
    """No full-model serialization/hashing, pixel equality, or unbounded proof."""
    def __init__(self, seconds=.002, nodes=20000):
        self.started = perf_counter()
        self.deadline = perf_counter() + seconds
        self.remaining = nodes
        self.nodes = nodes
        self.file_stats = 0

    def step(self):
        self.remaining -= 1
        if self.remaining < 0 or perf_counter() > self.deadline:
            raise Rejected("proof budget")

    def equal(self, a, b):
        self.step()
        if a is b:
            return
        if isinstance(a, TileStore) and isinstance(b, TileStore):
            # Empty inactive vector/selection feedback stores are equivalent;
            # active transient stores are excluded rather than sampled.
            if a._tiles or b._tiles:
                raise Rejected("active transient tile state")
            return
        if type(a) is not type(b):
            raise Rejected("value type")
        if a is None or isinstance(a, (bool, int, float, str, bytes)):
            # Model bytes are immutable values, never decoded here.
            if isinstance(a, (str, bytes)) and max(len(a), len(b)) > (4096 if isinstance(a, str) else 65536):
                raise Rejected("large nonidentical immutable value")
            if a != b:
                raise Rejected("value")
        elif is_dataclass(a) and not isinstance(a, type):
            for field in fields(a):
                self.equal(getattr(a, field.name), getattr(b, field.name))
        elif isinstance(a, dict):
            if max(len(a), len(b)) > 4096 or tuple(a) != tuple(b):
                raise Rejected("map order/keys")
            for key in a:
                self.equal(a[key], b[key])
        elif isinstance(a, (list, tuple)):
            if max(len(a), len(b)) > 4096 or len(a) != len(b):
                raise Rejected("sequence length")
            for left, right in zip(a, b):
                self.equal(left, right)
        elif isinstance(a, (set, frozenset)):
            if len(a) > 256 or a != b:
                raise Rejected("set")
        elif isinstance(a, QImage):
            # Image identity is metadata, never QImage deep pixel equality.
            if image_token(a) != image_token(b):
                raise Rejected("image storage")
        elif isinstance(a, QPainterPath):
            if a.elementCount() > 2048 or b.elementCount() > 2048 or a != b:
                raise Rejected("path")
        elif isinstance(a, (QTransform, QPolygonF)):
            if isinstance(a, QPolygonF) and max(len(a), len(b)) > 256:
                raise Rejected("large Qt polygon")
            if a != b:
                raise Rejected("Qt geometry")
        elif type(a).__module__.startswith("PySide6"):
            if a != b:
                raise Rejected("Qt value")
        else:
            # Unknown records, borrowed numpy buffers, TileStores, and owner
            # objects cannot silently become trusted proof dependencies.
            raise Rejected("unsupported value")


def image_token(image):
    if image.isNull():
        raise Rejected("expired image")
    return (int(image.cacheKey()), image.width(), image.height(),
            image.format().value, image.colorSpace(), image.devicePixelRatio())


def view_token(canvas):
    camera = canvas.camera_transform()
    return (tuple(getattr(camera, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)),
            canvas.width(), canvas.height(), canvas.devicePixelRatioF(),
            getattr(canvas, "_graphics_worker", None),
            canvas.context() if hasattr(canvas, "context") else None,
            canvas.settings.canvas_renderer)


def resources(canvas, snapshot, budget):
    """Existing immutable pins + native version/Qt mutator evidence only.

    Disk stamps are metadata, not read/decode. A changed or missing resident
    decoded representation is rejected rather than assumed source-equivalent.
    Large/file-heavy scenes may exhaust the budget and receive no handoff.
    """
    if max(len(canvas.images._sources), len(snapshot.images._sources)) > 512 or canvas.images._sources.keys() != snapshot.images._sources.keys():
        raise Rejected("image set")
    encoded, decoded, native_tiles = [], [], []
    for identifier, source in canvas.images._sources.items():
        budget.step()
        captured = snapshot.images._sources[identifier]
        if source._encoded is not captured._encoded:
            raise Rejected("source adoption")
        encoded.append((identifier, source._encoded))
    # Snapshot decoded frames must also be positive ordinary-decoder values.
    # Generic _cache_decoded/put_decoded frames are deliberately untrusted.
    if max(len(snapshot.images._decoded), len(canvas.images._decoded)) > 512:
        raise Rejected("source record cap")
    for key, image in snapshot.images._decoded.items():
        budget.step()
        identifier = key if isinstance(key, str) else key[1]
        source = snapshot.images._sources.get(identifier)
        proof = snapshot.images._owned_decodes.get(key)
        if source is None or proof != (source._encoded, int(image.cacheKey()),
                                      image.width(), image.height(), image.format().value):
            raise Rejected("unproved snapshot decoded input")
    for key, image in canvas.images._decoded.items():
        budget.step()
        captured = snapshot.images._decoded.get(key)
        identifier = key if isinstance(key, str) else key[1]
        source = canvas.images._sources.get(identifier)
        proof = canvas.images._owned_decodes.get(key)
        if (source is None or proof != (source._encoded, int(image.cacheKey()),
                                       image.width(), image.height(), image.format().value)
                or captured is None or image_token(image) != image_token(captured)):
            raise Rejected("resident source mutation/unproved decode")
        decoded.append((key, image_token(image)))
    if max(len(canvas.tiles._tiles), len(snapshot.tiles._tiles)) > 256 or tuple(canvas.tiles._tiles) != tuple(snapshot.tiles._tiles):
        raise Rejected("tile owner set")
    for identifier, owner in canvas.tiles._tiles.items():
        captured = snapshot.tiles._tiles[identifier]
        if max(len(owner.entries), len(captured.entries)) > 4096 or tuple(owner.entries) != tuple(captured.entries):
            raise Rejected("tile address set")
        for key, value in owner.entries.items():
            budget.step()
            version = owner.version(key)  # observes ordinary Qt borrower edits
            if version != (captured.versions.get(key, 0), captured.content_keys.get(key, 0)):
                raise Rejected("native tile mutation")
            stamp = None
            if isinstance(value, Path):
                budget.file_stats += 1
                if budget.file_stats > 64:
                    raise Rejected("file metadata record cap")
                stat = value.stat()
                stamp = (value, stat.st_size, stat.st_mtime_ns, stat.st_ino)
            native_tiles.append((identifier, key, version, stamp))
    return tuple(encoded), tuple(decoded), tuple(native_tiles)


def check_resources(canvas, witness, budget):
    encoded, decoded, native_tiles = witness
    if len(canvas.images._sources) > 512 or canvas.images._sources.keys() != {identifier for identifier, _ in encoded}:
        raise Rejected("source set changed")
    for identifier, value in encoded:
        budget.step()
        if canvas.images._sources[identifier]._encoded is not value:
            raise Rejected("source changed")
    if len(canvas.images._decoded) > 512 or canvas.images._decoded.keys() != {key for key, _ in decoded}:
        # Cache eviction/adoption is deliberately conservative in R1.
        raise Rejected("decoded source set changed")
    for key, value in decoded:
        budget.step()
        image = canvas.images._decoded[key]
        identifier = key if isinstance(key, str) else key[1]
        source = canvas.images._sources[identifier]
        if (canvas.images._owned_decodes.get(key) != (source._encoded, int(image.cacheKey()),
                image.width(), image.height(), image.format().value)
                or image_token(image) != value):
            raise Rejected("decoded source changed")
    # Every old address must remain, with no newly introduced pixels.
    if len(canvas.tiles._tiles) > 256 or sum(len(owner.entries) for owner in canvas.tiles._tiles.values()) != len(native_tiles):
        raise Rejected("tile count changed")
    for identifier, key, version, stamp in native_tiles:
        budget.step()
        owner = canvas.tiles._tiles.get(identifier)
        if owner is None or key not in owner.entries or owner.version(key) != version:
            raise Rejected("tile changed")
        if stamp is not None:
            budget.file_stats += 1
            if budget.file_stats > 64:
                raise Rejected("file metadata record cap")
            path = owner.entries[key]
            if not isinstance(path, Path):
                raise Rejected("file became edited")
            stat = path.stat()
            if (path, stat.st_size, stat.st_mtime_ns, stat.st_ino) != stamp:
                raise Rejected("source stamp changed")


def check_state(canvas, state, budget):
    for name in SCENE_FIELDS:
        budget.step()
        if (name in state) != hasattr(canvas, name):
            raise Rejected("render state presence")
        if name in state:
            budget.equal(getattr(canvas, name), state[name])


def committed_values(snapshot):
    """Use established Image storage and pure attachment semantics, no render."""
    scene = EvaluatedScene(snapshot)
    identifier = snapshot.state["selected_object_id"]
    old = snapshot.chapter.objects[identifier]
    source = snapshot.state["_transform_start_quad"]
    destination = snapshot.state["_transform_preview_quad"]
    parent = scene.layer_world_transform(old.parent_layer_id)
    mapping = scene._quad_to_quad_transform(
        [parent.map(QPointF(*p)).toTuple() for p in source],
        [parent.map(QPointF(*p)).toTuple() for p in destination])
    result = copy(snapshot.chapter)
    result.objects, result.modifiers, result.masks = (dict(snapshot.chapter.objects),
        dict(snapshot.chapter.modifiers), dict(snapshot.chapter.masks))
    obj = deepcopy(old)
    # translate-vs-handle is assigned by the real input owner, supplied below.
    return scene, result, obj, mapping


@dataclass
class PaintedLive:
    document: object
    snapshot: object
    tile: object
    request: object
    serial: int
    view: tuple
    sequence: int
    swapped: bool = False


@dataclass
class Ticket:
    source_document: object
    source_serial: int
    source_image_key: int
    source_request: object
    chapter: object
    state: dict
    resources: tuple
    view: tuple
    signature_tail: tuple
    history_revision: int
    prior_command: object
    refs: set
    image: object
    tile: object
    changes: list
    document: object = None
    target_serial: int | None = None
    presentation: object = None


class ReleaseHandoff:
    """One ordinary presented preview alias, no result/pixel/cache pool."""
    def __init__(self, controller):
        self.controller = controller
        self.last = None
        self.pending_paint = None
        self.armed = None
        self.ticket = None
        self.sequence = 0
        self.reason = "unarmed"
        self.validation = []
        self.validation_summary = {}

    def measured(self, phase, budget):
        elapsed = (perf_counter() - budget.started) * 1000
        row = self.validation_summary.setdefault(phase, {"calls": 0, "total_ms": 0.,
            "maximum_ms": 0., "maximum_nodes": 0, "maximum_file_stats": 0, "over_cooperative_deadline": 0})
        row["calls"] += 1
        row["total_ms"] += elapsed
        row["maximum_ms"] = max(row["maximum_ms"], elapsed)
        row["maximum_nodes"] = max(row["maximum_nodes"], budget.nodes - budget.remaining)
        row["maximum_file_stats"] = max(row["maximum_file_stats"], budget.file_stats)
        row["over_cooperative_deadline"] += elapsed > 2.
        if len(self.validation) < 64:
            self.validation.append({"phase": phase, "elapsed_ms": (perf_counter() - budget.started) * 1000,
                "nodes": budget.nodes - budget.remaining, "file_stats": budget.file_stats,
                "reason": self.reason})

    def clear(self, reason):
        ticket = self.ticket or self.armed
        if ticket is not None:
            if self.controller.preview is ticket.presentation:
                self.controller.preview = None
            if isinstance(ticket.image, DeviceImage):
                ticket.image.release()
        self.last = self.pending_paint = self.armed = self.ticket = None
        self.reason = reason

    def new_request(self, signature):
        if self.ticket is not None and signature[0] == self.ticket.document and signature[1:] == self.ticket.signature_tail:
            return
        if self.last is not None and self.last.document == signature[0]:
            return
        self.clear("new/incompatible demand")
        self.controller._preview_snapshot = self.controller._preview_request = None

    def painted(self, document, tile, snapshot, request, *, owned, swapped_required):
        controller = self.controller
        if (not owned or not document.live_preview or controller.desired is None
                or controller.desired[0] != document or snapshot is None
                or snapshot.document != document or controller.preview is None
                or controller.preview[1] is not tile
                or tile.key != ("preview", controller.serial)
                or request is None or request.quality is not RenderQuality.INTERACTIVE
                or request.phase is not None or request.target is not None):
            self.pending_paint = None
            return
        self.sequence += 1
        self.pending_paint = PaintedLive(document, snapshot, tile, request,
            controller.serial, view_token(controller.canvas), self.sequence, not swapped_required)
        if not swapped_required:
            self.last = self.pending_paint

    def swapped(self):
        value, controller = self.pending_paint, self.controller
        if (value is not None and controller.desired is not None
                and controller.desired[0] == value.document and controller.serial == value.serial
                and controller.preview is not None and controller.preview[1] is value.tile):
            value.swapped = True
            self.last = value
        self.pending_paint = None

    def begin(self):
        canvas, controller, live = self.controller.canvas, self.controller, self.last
        self.clear("new commit proof")
        self.last = live
        image = None
        history_revision = canvas.command_stack.revision
        try:
            budget = Budget()
            if (live is None or not live.swapped or controller.desired is None
                    or controller.desired[0] != live.document or controller.serial != live.serial
                    or not live.document.live_preview or live.document.pixel_contract.floating
                    or live.view != view_token(canvas) or controller.preview is None
                    or controller.preview[1] is not live.tile
                    or getattr(canvas, "_geometry_transform_target", None) is not None
                    or canvas._transform_drag_mode not in {"translate", "handle", "rotate"}
                    or not isinstance(canvas._model_before, RecordSnapshot)):
                raise Rejected("no final presented own transform")
            identifier = canvas.selected_object_id
            obj = canvas.chapter.objects.get(identifier)
            if not isinstance(obj, ImageObject) or obj.placement_mode != "free":
                raise Rejected("unsupported target")
            if (tuple(canvas.selected_entities) != (("object", identifier),)
                    or getattr(canvas, "_drawing", False)
                    or live.snapshot.state.get("_mesh_warp_preview_id") is not None
                    or live.snapshot.state.get("_smudge_preview_id") is not None):
                raise Rejected("other interaction")
            for name in ("_cage_session", "_text_editing", "_text_placement", "_gradient_preview_active",
                         "_selection_before_tiles", "_multi_transform_preview_quads", "_overlay_color_preview",
                         "_render_base_alpha", "_render_cage_source", "_rendering_mask_contributor",
                         "_rendering_halftone_source", "_modifier_handle_drag", "_mesh_warp_parameter_drag_id",
                         "_smudge_parameter_drag_id", "_page_gap_draft", "_fill_gesture_active"):
                if getattr(canvas, name, None):
                    raise Rejected("special interaction/capture")
            # Crucial rapid-release guard: the immutable painted pose must be
            # exactly the final queued input pose, not merely the same target.
            check_state(canvas, live.snapshot.state, budget)
            budget.equal(canvas.chapter, live.snapshot.chapter)
            witness = resources(canvas, live.snapshot, budget)
            scene, expected, changed, mapping = committed_values(live.snapshot)
            destination = deepcopy(live.snapshot.state["_transform_preview_quad"])
            source = live.snapshot.state["_transform_start_quad"]
            if canvas._transform_drag_mode == "translate" and obj.transform_quad is None:
                changed.x += destination[0][0] - source[0][0]
                changed.y += destination[0][1] - source[0][1]
            else:
                changed.transform_frame = scene._object_transform_frame(changed)
                changed.transform_quad = destination
            expected.objects[identifier] = changed
            refs = {("object", identifier)}
            modifiers, masks = ownership(live.snapshot.chapter)
            delta = translation(mapping)
            for mid, owners in modifiers.items():
                if owners == {("object", identifier)}:
                    value = deepcopy(live.snapshot.chapter.modifiers[mid])
                    transform_modifier_rig(scene, value, mapping)
                    budget.equal(value, effective_preview_modifier(scene, live.snapshot.chapter.modifiers[mid]))
                    expected.modifiers[mid] = value
                    refs.add(("modifier", mid))
            if delta is not None and delta != (0., 0.):
                for mask_id, owners in masks.items():
                    if owners == {("object", identifier)}:
                        value = deepcopy(live.snapshot.chapter.masks[mask_id])
                        translate_mask(value, *delta)
                        budget.equal(value, effective_preview_mask(scene, live.snapshot.chapter.masks[mask_id]))
                        expected.masks[mask_id] = value
                        refs.add(("mask", mask_id))
            expected_state = dict(live.snapshot.state)
            expected_state["_transform_start_quad"] = expected_state["_transform_preview_quad"] = None
            image = (live.tile.image.copy(live.tile.image.rect()) if isinstance(live.tile.image, DeviceImage)
                     else QImage(live.tile.image))
            if image.isNull() or image.sizeInBytes() > canvas._document_projection.budget // 4:
                raise Rejected("expired/large presentation")
            budget.step()
            if (canvas.command_stack.revision != history_revision or controller.serial != live.serial
                    or canvas._render_document_state() != live.document):
                raise Rejected("reentrant acquisition/model publication")
            ticket = Ticket(live.document, live.serial, int(live.tile.image.cacheKey()), live.request,
                expected, expected_state, witness, live.view, controller.desired[1:],
                history_revision, canvas.command_stack.top_undo_command, refs,
                image, replace(live.tile, image=image), [])
            self.armed, self.reason = ticket, "own commit armed"
            return ticket
        except Exception as error:
            if self.armed is None and isinstance(image, DeviceImage):
                image.release()
            self.clear(type(error).__name__ + ": " + str(error))
            return None
        finally:
            self.measured("begin", budget)

    def changed(self, change, action):
        ticket = self.armed
        if ticket is None:
            if self.ticket is not None:
                self.clear("subsequent typed edit")
            return
        if (action != "push" or change.conservative or change.transient
                or change.resources or change.document_fields or change.orders
                or any(item.structural or item.entity not in ticket.refs for item in change.entities)):
            self.clear("unexpected commit mutation")
            return
        ticket.changes.append(change)

    def finish(self, ticket):
        canvas = self.controller.canvas
        try:
            budget = Budget()
            if ticket is None or self.armed is not ticket or len(ticket.changes) != 1:
                raise Rejected("not exactly one own command")
            command = canvas.command_stack.top_undo_command
            if (not isinstance(command, CallbackCommand) or command is ticket.prior_command
                    or canvas.command_stack.revision != ticket.history_revision + 1
                    or command.forward_change is not ticket.changes[0]
                    or canvas._projection_has_live_preview()):
                raise Rejected("history/remaining preview")
            budget.equal(canvas.chapter, ticket.chapter)
            check_state(canvas, ticket.state, budget)
            check_resources(canvas, ticket.resources, budget)
            if view_token(canvas) != ticket.view:
                raise Rejected("view/backend changed")
            ticket.document = canvas._render_document_state()
            if (ticket.document.live_preview or ticket.document.configuration != ticket.source_document.configuration
                    or ticket.document.identity != ticket.source_document.identity
                    or ticket.document.pixel_contract != ticket.source_document.pixel_contract):
                raise Rejected("committed context changed")
            self.armed, self.ticket = None, ticket
            # Do not keep a second complete source snapshot/pixel pool alive.
            self.last = self.pending_paint = None
            self.controller._preview_snapshot = self.controller._preview_request = None
            self.reason = "equivalent committed geometry; nonexact presentation only"
        except Exception as error:
            self.clear(type(error).__name__ + ": " + str(error))
        finally:
            self.measured("finish", budget)

    def for_request(self, signature, serial):
        ticket = self.ticket
        if ticket is None:
            return None
        canvas = self.controller.canvas
        try:
            budget = Budget()
            if (signature[0] != ticket.document or signature[1:] != ticket.signature_tail
                    or ticket.target_serial is not None and serial != ticket.target_serial
                    or canvas.command_stack.revision != ticket.history_revision + 1
                    or canvas._projection_has_live_preview() or view_token(canvas) != ticket.view):
                raise Rejected("new serial/view/selection/cancel/history")
            budget.equal(canvas.chapter, ticket.chapter)
            check_state(canvas, ticket.state, budget)
            check_resources(canvas, ticket.resources, budget)
            if ticket.image.isNull():
                raise Rejected("expired graphics storage")
            ticket.target_serial = serial
            if ticket.presentation is None:
                ticket.presentation = (ticket.document, replace(ticket.tile, key=("preview", serial)))
            return ticket.presentation
        except Exception as error:
            self.clear(type(error).__name__ + ": " + str(error))
            return None
        finally:
            self.measured("request", budget)
