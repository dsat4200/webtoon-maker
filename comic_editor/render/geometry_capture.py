"""Bounded immutable geometry previews through the ordinary scene evaluator.

Only an actual open boundary/placement gesture can own these snapshots. A
completed preview retains its captured revision; it is never current exact
artwork, exported output, or a durable cache entry.
"""
import copy
from dataclasses import dataclass, fields, replace

from PySide6.QtGui import QImage

from comic_editor.core.changes import GROUP_KINDS, KIND_GROUPS
from comic_editor.core.document_patch import RecordSnapshot
from comic_editor.core.tiles import TileStore
from comic_editor.core.tools import ToolKind
from comic_editor.render.admission import RENDER_ADMISSION
from comic_editor.render.ordinary_contact_capture import (
    CaptureAdmissionUnavailable, CaptureUnavailable, _LeaseOwner, bounded_value, require)
from comic_editor.render.scene import SCENE_FIELDS, SceneSnapshot, detached_value, native_source_key


def gesture(canvas):
    """Strong transaction owner and the complete records it may change."""
    before = getattr(canvas, '_model_before', None)
    if (not isinstance(before, RecordSnapshot) or canvas.chapter is None
            or before.document_identity != id(canvas.chapter)):
        return None
    control = getattr(canvas, '_active_shape_control', None)
    target = getattr(canvas, '_geometry_transform_target', None)
    if canvas.tool == ToolKind.SHAPE_EDIT and control:
        tag = 'boundary', control
    elif (canvas.tool == ToolKind.TRANSFORM
            and (target and target[0] in {'layer_group', 'layer', 'object', 'multi'}
                 or target is None and canvas.selected_kind == 'object'
                     and canvas.selected_object_id == canvas.selected_id)
            and canvas._transform_start_quad and canvas._transform_preview_quad
            and canvas._transform_drag_mode in {'translate', 'rotate', 'handle'}):
        tag = 'transform', target or ('object', canvas.selected_object_id), canvas._transform_drag_mode
    else:
        return None
    refs = frozenset((GROUP_KINDS[group], identifier)
                     for group, records in before.records.items() for identifier in records)
    if not refs or len(refs) > 128 or any(value is None for records in before.records.values()
                                         for value in records.values()):
        return None
    return before, tag, refs


def owned_change(canvas, change, action):
    current = gesture(canvas)
    if (current is None or action != 'transient' or not change.transient
            or change.conservative or change.resources or change.document_fields
            or change.orders or not change.entities
            or any(item.structural or item.entity not in current[2] for item in change.entities)):
        return False
    if current[1][0] == 'boundary':
        # This label belongs to the actual shape-edit publisher, which also
        # moves wholly owned attached rigs. Unrelated edits retire the owner.
        return change.label == 'Edit geometry'
    return change.label == 'Preview transform' and all(
        item.fields == frozenset({'transform_preview'}) for item in change.entities)


@dataclass(frozen=True)
class GeometrySceneSnapshot(SceneSnapshot):
    interaction_preview_owner: tuple | None = None
    capture_lease: object = None


class GeometryCapture:
    """Freeze bounded changed metadata with already pinned complete originals."""
    def __init__(self, canvas, document, token):
        self.canvas, self.document, self.token = canvas, document, token
        self.result = None
        self.stale = False
        controller = canvas._scene_controller
        require(token is not None and token == controller._geometry_preview_owner,
                'actual geometry gesture owner required')
        require(controller._geometry_preview_key_for(document, *controller.desired[1:]) == token[1],
                'current geometry context required')
        current = gesture(canvas)
        require(current is not None, 'open geometry transaction required')
        before, _tag, allowed = current
        compiler = canvas._scene_snapshot_compiler
        require(compiler.identity == document.identity and not compiler.conservative,
                'complete ordinary model capture required')
        require(compiler.dirty <= allowed, 'unrelated model edit pending')
        require(compiler.native_sources is not None
                and compiler.native_source_key == native_source_key(canvas.tiles, document.identity, compiler.source_generation),
                'current complete native sources required')
        require(sum(len(getattr(canvas.chapter, group)) for group in GROUP_KINDS) <= 8192,
                'global model metadata cap')
        # This lane retains only bounded COW borrowers. It cannot enter while
        # an ordinary worker owns an incompatible oversized workspace.
        native = compiler.native_sources.tiles
        retained = sum(int(image.sizeInBytes()) for image, _key in native.residency.entries.values())
        decoded = tuple(canvas.images._decoded.items())
        retained += sum(int(image.sizeInBytes()) for _key, image in decoded)
        require(retained <= 128 * 1024 * 1024 and len(decoded) <= 1536,
                'geometry capture native alias cap')
        for ref in allowed:
            record = getattr(canvas.chapter, KIND_GROUPS[ref[0]]).get(ref[1])
            require(record is not None, 'geometry record retired')
            partial = before.attributes.get(KIND_GROUPS[ref[0]])
            if partial is None:
                bounded_value(record, limit=16384)
            else:
                for name in partial:
                    bounded_value(getattr(record, name))
        state = {}
        for name in SCENE_FIELDS:
            if not hasattr(canvas, name):
                continue
            value = getattr(canvas, name)
            if name == '_cage_session' and value is not None:
                raise CaptureUnavailable('other draft active')
            if isinstance(value, TileStore):
                require(not any(len(owner) for owner in value._tiles.values()), 'other native draft active')
                state[name] = TileStore(value.tile_size)
            else:
                bounded_value(value)
                state[name] = detached_value(value)
        lease = RENDER_ADMISSION.reserve_copies(retained, scope=token)
        if lease is None:
            raise CaptureAdmissionUnavailable('geometry capture admission declined')
        lease_owner = _LeaseOwner(lease)
        chapter = copy.copy(canvas.chapter)
        for field in fields(chapter):
            if field.name not in GROUP_KINDS:
                bounded_value(getattr(canvas.chapter, field.name))
                setattr(chapter, field.name, detached_value(getattr(canvas.chapter, field.name)))
                continue
            group, kind = field.name, GROUP_KINDS[field.name]
            saved = {}
            for identifier, record in getattr(canvas.chapter, group).items():
                ref = kind, identifier
                if ref in allowed:
                    partial = before.attributes.get(group)
                    if partial is None:
                        saved[identifier] = detached_value(record)
                    else:
                        require(ref in compiler.records, 'unchanged original model capture required')
                        saved[identifier] = copy.copy(compiler.records[ref])
                        for name in partial:
                            setattr(saved[identifier], name, detached_value(getattr(record, name)))
                else:
                    require(ref in compiler.records, 'missing unchanged model capture')
                    saved[identifier] = compiler.records[ref]
            setattr(chapter, group, saved)
        images = canvas.images.clone()
        for key, image in decoded:
            alias = QImage(image)
            images._cache_decoded(key, alias)
            images._adopt_owned_decode(key, alias, canvas.images._owned_decodes.get(key))
        sources = compiler.native_sources
        tiles = sources.fork()
        mesh = canvas._mesh_warp_preview_modifier()
        require(mesh is None, 'other modifier draft active')
        state['_mesh_warp_preview_id'] = state['_smudge_preview_id'] = None
        # Capture uses the accepted color environment of the complete ordinary
        # scene. Configuration changes are excluded by the owner context.
        previous = controller.snapshot
        require(previous is not None and previous.document.identity == document.identity
                and previous.document.configuration == document.configuration,
                'ordinary captured color environment required')
        self.result = GeometrySceneSnapshot(document, chapter, tiles, images, state,
            (canvas.width(), canvas.height()), copy.copy(previous.settings), sources.jobs,
            getattr(canvas, '_graphics_worker', None), previous.pixel_environment, None,
            token, lease_owner)
        require(canvas._render_document_state() == document, 'geometry changed during bounded freeze')

    def advance(self, seconds=.004):
        return True

    def retire(self):
        self.stale = True
        self.result = None
