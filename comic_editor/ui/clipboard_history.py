"""Bounded, session-local image and editable-object clipboard history."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import hashlib
import json

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QIcon, QImage, QImageReader, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QDialog, QLabel, QListWidget, QListWidgetItem, QVBoxLayout
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from comic_editor.core.assets import _collect_subtree, _translate_object, extract_asset, instantiate_asset, entity_visual_bounds
from comic_editor.core.commands import CallbackCommand
from comic_editor.core.changes import ResourceChange
from comic_editor.core.document_patch import DocumentPatch, RecordSnapshot
from comic_editor.core.images import ImageStore
from comic_editor.core.models import ChapterDocument, ImageObject, RasterObject, VectorDrawingObject
from comic_editor.core.tiles import TileStore
from comic_editor.ui.network import create_network_manager


MAX_HISTORY_ITEMS = 30
MAX_HISTORY_BYTES = 128 * 1024 * 1024


class ExternalClipboardReader(QObject):
    """Fetch browser image URLs without blocking drawing or re-reading files."""
    resolved = Signal(object)

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.manager = create_network_manager(self)
        self.entries = []
        self.replies = set()

    @property
    def pending(self):
        return bool(self.replies)

    def read(self, mime):
        obsolete = list(self.replies)
        self.replies.clear()
        for reply in obsolete:
            reply.abort()
            reply.deleteLater()
        self.entries = self.canvas._external_image_entries(mime)[:16] if mime is not None else []
        for entry in self.entries:
            if not entry["pending"] or entry["data"]:
                continue
            request = QNetworkRequest(QUrl(entry["url"]))
            request.setRawHeader(b"User-Agent", b"WebtoonMaker/1.0")
            request.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                                 QNetworkRequest.NoLessSafeRedirectPolicy)
            reply = self.manager.get(request)
            self.replies.add(reply)
            reply.downloadProgress.connect(
                lambda received, total, current=reply: current.abort()
                if max(received, total) > 256 * 1024 * 1024 else None)
            reply.finished.connect(lambda current=reply, item=entry: self._finished(current, item))
            QTimer.singleShot(15000, self, lambda current=reply: current.abort() if current in self.replies else None)
        return self.sources()

    def sources(self):
        return [(entry["filename"], entry["mime_type"], entry["data"])
                for entry in self.entries if entry["data"] and not entry["failed"]]

    def _finished(self, reply, entry):
        if reply not in self.replies:
            return
        self.replies.remove(reply)
        entry["pending"] = False
        if reply.error() == QNetworkReply.NoError:
            validated = self.canvas._validated_image_source(entry["filename"], entry["mime_type"], bytes(reply.readAll()))
            if validated:
                entry.update(filename=validated[0], mime_type=validated[1], data=validated[2])
        reply.deleteLater()
        if not self.replies:
            self.resolved.emit(self.sources())


def square_thumbnail(image: QImage, size: int = 72) -> QImage:
    result = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor("#353535"))
    painter = QPainter(result)
    for y in range(0, size, 9):
        for x in range(0, size, 9):
            if (x // 9 + y // 9) % 2 == 0:
                painter.fillRect(x, y, 9, 9, QColor("#484848"))
    if not image.isNull():
        scaled = image.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return result


def image_thumbnail(data: bytes) -> QImage:
    encoded = QByteArray(data)
    buffer = QBuffer(encoded)
    buffer.open(QIODevice.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid():
        reader.setScaledSize(size.scaled(72, 72, Qt.KeepAspectRatio))
    decoded = reader.read()
    if decoded.isNull():
        decoded, _format = ImageStore._decode(data)
    return square_thumbnail(decoded)


@dataclass
class ObjectClipboard:
    manifest: object
    tiles: TileStore
    images: ImageStore
    original_center: QPointF
    is_page: bool = False
    dependencies: list[ObjectClipboard] = field(default_factory=list)
    external_contributors: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


@dataclass
class HistoryEntry:
    kind: str
    label: str
    payload: object
    thumbnail: QImage
    byte_size: int
    fingerprint: str = ""


class ClipboardImageHistory:
    def __init__(self, max_items=MAX_HISTORY_ITEMS, max_bytes=MAX_HISTORY_BYTES):
        self.entries: list[HistoryEntry] = []
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.byte_size = 0

    def add(self, entry: HistoryEntry) -> bool:
        # Oversize items remain available to ordinary Paste without retaining
        # another long-lived reference in history.
        if entry.byte_size > self.max_bytes:
            return False
        if entry.fingerprint:
            self.entries = [
                previous for previous in self.entries
                if previous.fingerprint != entry.fingerprint
            ]
        self.entries.insert(0, entry)
        self.byte_size = sum(item.byte_size for item in self.entries)
        while len(self.entries) > self.max_items or self.byte_size > self.max_bytes:
            self.byte_size -= self.entries.pop().byte_size
        return True

    def add_images(self, sources) -> None:
        for filename, mime_type, data in sources:
            digest = hashlib.sha256(data).hexdigest()
            existing = next((e for e in self.entries if e.fingerprint == digest), None)
            if existing is not None:
                self.add(existing)
                continue
            if len(data) > self.max_bytes:
                continue
            try:
                thumbnail = image_thumbnail(data)
            except ValueError:
                continue
            self.add(HistoryEntry(
                "image", filename, [(filename, mime_type, bytes(data))],
                thumbnail, len(data) + 72 * 72 * 4, digest,
            ))

    def add_drawing(self, canvas, payload) -> None:
        from comic_editor.core.clipboard import RasterSelectionClipboard, VectorSelectionClipboard
        if not isinstance(payload, (RasterSelectionClipboard, VectorSelectionClipboard)):
            return
        existing = next((entry for entry in self.entries if entry.payload is payload), None)
        if existing is not None:
            self.add(existing)
            return
        self._add_preview(canvas, 'drawing', payload, payload.source_name)

    def add_object(self, canvas, payload: ObjectClipboard) -> None:
        existing = next((entry for entry in self.entries if entry.payload is payload), None)
        if existing is not None:
            self.add(existing)
            return
        # The payload is already independently owned. Thumbnail decoding must
        # not leave full-resolution image residency in clipboard history.
        for component in [payload, *payload.dependencies]:
            component.images._decoded.clear()
        self._add_preview(canvas, 'object', payload, payload.manifest.name)

    def _add_preview(self, canvas, kind, payload, label):
        from comic_editor.render.input_capture import clipboard_preview
        from comic_editor.ui.scene_consumers import scene_consumers
        entry = HistoryEntry(kind, label, payload, square_thumbnail(QImage()),
            72*72*4, f'{kind}:{id(payload)}')
        if not self.add(entry):
            return
        def present():
            return any(current is entry for current in self.entries)
        def ready(result, error):
            if error is not None or not present():
                return
            entry.thumbnail, entry.byte_size = result
            self.byte_size = sum(current.byte_size for current in self.entries)
            while self.entries and (len(self.entries) > self.max_items or self.byte_size > self.max_bytes):
                self.byte_size -= self.entries.pop().byte_size
        scene_consumers(canvas).request_detached(('clipboard-thumbnail', id(entry)), clipboard_preview,
            (kind, payload), ready, valid=present)


class ClipboardHistoryPopup(QDialog):
    def __init__(self, entries: list[HistoryEntry], activate, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setWindowTitle("Clipboard image history")
        self.setObjectName("clipboardImageHistory")
        self.resize(360, 440)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Clipboard image history"))
        self.list = QListWidget(self)
        self.list.setIconSize(QSize(72, 72))
        self.list.setSpacing(4)
        self.list.setUniformItemSizes(True)
        self.entries = list(entries)
        self._chosen = False
        self.finished.connect(lambda _result: self.entries.clear())
        for entry in self.entries:
            kind = {"image": "Image", "drawing": "Drawing selection", "object": "Outliner object"}[entry.kind]
            item = QListWidgetItem(QIcon(QPixmap.fromImage(entry.thumbnail)), f"{entry.label}\n{kind}")
            item.setSizeHint(QSize(300, 80))
            self.list.addItem(item)
        if not entries:
            self.list.addItem("Copy or paste an image or object to begin.")
            self.list.setEnabled(False)
        layout.addWidget(self.list)
        self._thumbnail_keys = [entry.thumbnail.cacheKey() for entry in self.entries]
        self._thumbnail_timer = QTimer(self)
        self._thumbnail_timer.setInterval(50)
        self._thumbnail_timer.timeout.connect(self._refresh_thumbnails)
        self._thumbnail_timer.start()

        def chosen(item):
            if self._chosen:
                return
            self._chosen = True
            row = self.list.row(item)
            entry = self.entries[row]
            self.accept()
            activate(entry)

        self.list.itemClicked.connect(chosen)
        self.list.itemActivated.connect(chosen)
        if entries:
            self.list.setCurrentRow(0)

    def _refresh_thumbnails(self):
        for row, entry in enumerate(self.entries):
            key = entry.thumbnail.cacheKey()
            if self._thumbnail_keys[row] != key:
                self.list.item(row).setIcon(QIcon(QPixmap.fromImage(entry.thumbnail)))
                self._thumbnail_keys[row] = key

    def open_at(self, global_position: QPoint) -> None:
        geometry = self.screen().availableGeometry()
        self.move(
            max(geometry.left(), min(global_position.x(), geometry.right() - self.width())),
            max(geometry.top(), min(global_position.y(), geometry.bottom() - self.height())),
        )
        self.show()
        self.list.setFocus()


def cursor_world(canvas) -> QPointF | None:
    """Capture before a popup moves focus or the mouse leaves the canvas."""
    point = canvas.mapFromGlobal(QCursor.pos())
    if canvas.isVisible() and canvas.rect().contains(point):
        return canvas.widget_to_document(QPointF(point))
    return None


def drawing_at(payload, world: QPointF | None):
    if world is None:
        return payload
    from comic_editor.ui.canvas import RasterSelectionClipboard
    if isinstance(payload, RasterSelectionClipboard):
        bounds = payload.selection_path.boundingRect()
    else:
        points = [point for stroke in payload.strokes for point in stroke.points]
        if not points:
            return payload
        left, right = min(p.x for p in points), max(p.x for p in points)
        top, bottom = min(p.y for p in points), max(p.y for p in points)
        bounds = QRectF(left, top, right - left, bottom - top)
    center = payload.source_to_world.map(bounds.center())
    translation = QTransform.fromTranslate(world.x() - center.x(), world.y() - center.y())
    return replace(payload, source_to_world=payload.source_to_world * translation)


def _mask_identity_map(source, target, identity_map):
    """Match mask bindings by copied entity and modifier order."""
    result = {}
    for (kind, old_id), new_id in identity_map.items():
        old = source.layers[old_id] if kind == "layer" else source.objects[old_id]
        new = target.layers[new_id] if kind == "layer" else target.objects[new_id]
        if old.opacity_mask is not None and new.opacity_mask is not None:
            result[old.opacity_mask.mask_id] = new.opacity_mask.mask_id
        for old_modifier_id, new_modifier_id in zip(old.modifier_ids, new.modifier_ids):
            old_modifier = source.modifiers.get(old_modifier_id)
            new_modifier = target.modifiers.get(new_modifier_id)
            if old_modifier is None or new_modifier is None:
                continue
            for name, binding in old_modifier.parameter_masks.items():
                if name in new_modifier.parameter_masks:
                    result[binding.mask_id] = new_modifier.parameter_masks[name].mask_id
    return result


def _component_ids(document, kind, entity_id):
    layers, objects = _collect_subtree(document, kind, entity_id)
    return {("layer", identifier): identifier for identifier in layers} | {
        ("object", identifier): identifier for identifier in objects}


def _capture_component(canvas, kind: str, entity_id: str) -> ObjectClipboard:
    document = canvas.chapter
    entity = document.layers[entity_id] if kind == "layer" else document.objects[entity_id]
    center = entity_visual_bounds(document, canvas.tiles, kind, entity_id, include_effects=True).center()
    included = _component_ids(document, kind, entity_id)
    # The source document stays untouched. Temporarily omit cross-component
    # links so the asset cloner can remap this component's internal identities.
    snapshot = copy.copy(document)
    snapshot.masks = dict(document.masks)
    outside = {}
    for identifier, mask in document.masks.items():
        external = [reference for reference in mask.contributors if reference not in included]
        if external:
            outside[mask.mask_id] = external
            snapshot.masks[identifier] = copy.deepcopy(mask)
            snapshot.masks[identifier].contributors = [
                reference for reference in mask.contributors if reference in included]
    manifest, tiles, images = extract_asset(
        snapshot, canvas.tiles, kind, entity_id, entity.name,
        source_images=canvas.images, include_images=True,
    )
    # Asset extraction freezes live Blender links for library portability;
    # ordinary object Copy preserves their full source descriptor instead.
    for identifier, cloned in manifest.document.objects.items():
        original = document.objects.get(identifier)
        if isinstance(cloned, ImageObject) and isinstance(original, ImageObject):
            cloned.source = copy.deepcopy(original.source)
            cloned.sync_source_metadata()
            images.relabel(identifier, cloned.source_filename, cloned.source_mime_type)
    mask_map = _mask_identity_map(snapshot, manifest.document, included)
    return ObjectClipboard(
        manifest, tiles, images, center, kind == "layer" and entity.is_page,
        external_contributors={mask_map[identifier]: references
                               for identifier, references in outside.items() if identifier in mask_map},
    )


def capture_object(canvas, kind: str, entity_id: str) -> ObjectClipboard:
    payload = _capture_component(canvas, kind, entity_id)
    covered = set(_component_ids(canvas.chapter, kind, entity_id))
    pending = [reference for references in payload.external_contributors.values() for reference in references]
    while pending:
        reference = pending.pop(0)
        if reference in covered:
            continue
        dependency = _capture_component(canvas, *reference)
        dependency.is_page = False
        payload.dependencies.append(dependency)
        covered.update(_component_ids(canvas.chapter, *reference))
        pending.extend(reference for references in dependency.external_contributors.values() for reference in references)
    return payload


def _instantiated_identity_map(source, target, kind, source_id, target_id):
    result = {(kind, source_id): target_id}
    if kind == "layer":
        for old, new in zip(source.layers[source_id].children, target.layers[target_id].children):
            result.update(_instantiated_identity_map(source, target, old.kind, old.entity_id, new.entity_id))
    return result


def paste_object(canvas, payload: ObjectClipboard, parent_id: str, world: QPointF,
                 insertion_index: int | None = None, label="Paste object") -> str:
    """Use independent model/resource identities and one reversible command."""
    ancestor = canvas.chapter.ancestor_layers(parent_id)[0].layer_id
    before = RecordSnapshot.capture(canvas.chapter, scalars=('size', 'root_page_ids'),
        layers={parent_id, ancestor}, objects=(), modifiers=(), masks=())
    original_ids = {group: set(getattr(canvas.chapter, group)) for group in before.records}
    before_selection = canvas._selection_snapshot()
    before_resource_ids = set(canvas.chapter.objects) | set(canvas.chapter.masks)
    def include_created():
        created = {}
        for group, identifiers in original_ids.items():
            added = set(getattr(canvas.chapter, group)) - identifiers
            created[group] = added
            before.records[group].update({identifier: None for identifier in added})
            before.selections[group] = tuple(set(before.selections[group]) | added)
        return created
    try:
        kind, root_id, created_objects = instantiate_asset(
            payload.manifest, payload.tiles, canvas.chapter, canvas.tiles,
            parent_id, world.x(), world.y(), source_images=payload.images,
            target_images=canvas.images,
        )
        identities = _instantiated_identity_map(
            payload.manifest.document, canvas.chapter, kind, payload.manifest.root_id, root_id)
        mask_maps = [(payload, _mask_identity_map(payload.manifest.document, canvas.chapter, identities))]
        offset = world - payload.original_center
        dependency_parent = canvas.chapter.ancestor_layers(parent_id)[0].layer_id
        for dependency in payload.dependencies:
            dep_world = dependency.original_center + offset
            dep_kind, dep_id, dep_objects = instantiate_asset(
                dependency.manifest, dependency.tiles, canvas.chapter, canvas.tiles,
                dependency_parent, dep_world.x(), dep_world.y(),
                source_images=dependency.images, target_images=canvas.images,
            )
            created_objects.update(dep_objects)
            dep_entity = (canvas.chapter.layers[dep_id] if dep_kind == "layer"
                          else canvas.chapter.objects[dep_id])
            dep_entity.mask_only = True
            dep_identity = _instantiated_identity_map(
                dependency.manifest.document, canvas.chapter, dep_kind,
                dependency.manifest.root_id, dep_id)
            # Prefer the primary copied subtree when a contributor subtree
            # also contains one of its original identities.
            for reference, identifier in dep_identity.items():
                identities.setdefault(reference, identifier)
            mask_maps.append((dependency, _mask_identity_map(
                dependency.manifest.document, canvas.chapter, dep_identity)))
        for component, masks in mask_maps:
            for mask_id, references in component.external_contributors.items():
                target_mask = canvas.chapter.masks[masks[mask_id]]
                target_mask.contributors.extend((ref_kind, identities[(ref_kind, identifier)])
                                                for ref_kind, identifier in references)
        canvas.chapter.validate()
        if payload.is_page:
            if canvas.chapter.document_kind in {"image", "asset"}:
                raise ValueError("Pages can be pasted into chapter scenes only")
            root = canvas.chapter.layers[root_id]
            world_transform = canvas.layer_world_transform(root_id)
            canvas.chapter.layers[parent_id].children = [
                child for child in canvas.chapter.layers[parent_id].children
                if child.entity_id != root_id]
            root.parent_id, root.is_page = None, True
            root.transform_frame = root.transform_quad = None
            root.translate_x, root.translate_y = world_transform.dx(), world_transform.dy()
            page = canvas.chapter.ancestor_layers(parent_id)[0]
            index = canvas.chapter.root_page_ids.index(page.layer_id)
            canvas.chapter.root_page_ids.insert(index, root_id)
            canvas.chapter.ensure_height_for(root_id)
            canvas.chapter.validate()
        elif insertion_index is not None:
            children = canvas.chapter.layers[parent_id].children
            root = next(child for child in children if child.entity_id == root_id)
            children.remove(root)
            children.insert(insertion_index, root)
    except Exception:
        created_ids = (set(canvas.chapter.objects) | set(canvas.chapter.masks)) - before_resource_ids
        include_created()
        old, _new = DocumentPatch.pair(before, before.after(canvas.chapter))
        old.apply(canvas.chapter)
        for identifier in created_ids:
            canvas.tiles.remove_object(identifier)
            canvas.images.remove(identifier)
        raise
    include_created()
    after = before.after(canvas.chapter)
    old, new = DocumentPatch.pair(before, after)
    resource_ids = set(created_objects) | (set(canvas.chapter.masks) - before_resource_ids)
    tile_payload = {identifier: canvas.tiles.object_tiles(identifier) for identifier in resource_ids}
    image_payload = canvas.images.clone(resource_ids)
    change = new.change_set(old, label=label)
    change = replace(change, resources=tuple(ResourceChange(
        ('mask' if identifier in canvas.chapter.masks else 'object', identifier), 'raster', key)
        for identifier, values in tile_payload.items() for key in values))
    canvas.set_selection(kind, root_id, activate_default_tool=True)
    after_selection = canvas._selection_snapshot()

    def restore(patch, selection, with_tiles):
        for identifier in resource_ids:
            if with_tiles:
                canvas.tiles.replace_object_tiles(identifier, tile_payload[identifier])
                image_payload.copy_source_to(identifier, canvas.images, identifier)
            else:
                canvas.tiles.remove_object(identifier)
                canvas.images.remove(identifier)
        canvas._restore_history_state(patch, document_patch=True)
        canvas._restore_selection_snapshot(selection)
        canvas._emit_typed_hierarchy_changed(canvas.command_stack.applying_change or change)
        canvas._emit_typed_document_changed(QRectF(), canvas.command_stack.applying_change or change)
        canvas.update()

    canvas.command_stack.push(CallbackCommand(
        label, lambda: restore(new, after_selection, True),
        lambda: restore(old, before_selection, False),
        forward_change=change, backward_change=change.reversed(),
    ), already_done=True)
    canvas._emit_typed_hierarchy_changed(change)
    canvas._emit_typed_document_changed(QRectF(), change)
    canvas.interactionFinished.emit()
    canvas.update()
    return root_id
