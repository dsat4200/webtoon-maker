"""Bounded, session-local image and editable-object clipboard history."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
import hashlib
import json

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCursor, QIcon, QImage, QImageReader, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QDialog, QLabel, QListWidget, QListWidgetItem, QVBoxLayout
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from comic_editor.core.assets import _collect_subtree, _translate_object, extract_asset, instantiate_asset, entity_visual_bounds
from comic_editor.core.commands import CallbackCommand
from comic_editor.core.images import ImageStore
from comic_editor.core.models import ChapterDocument, ImageObject, RasterObject, VectorDrawingObject
from comic_editor.core.tiles import TileStore


MAX_HISTORY_ITEMS = 30
MAX_HISTORY_BYTES = 128 * 1024 * 1024


class ExternalClipboardReader(QObject):
    """Fetch browser image URLs without blocking drawing or re-reading files."""
    resolved = Signal(object)

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.manager = QNetworkAccessManager(self)
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
        from comic_editor.ui.canvas import RasterSelectionClipboard, VectorSelectionClipboard
        if not isinstance(payload, (RasterSelectionClipboard, VectorSelectionClipboard)):
            return
        existing = next((entry for entry in self.entries if entry.payload is payload), None)
        if existing is not None:
            self.add(existing)
            return
        document = ChapterDocument(document_kind="asset", background="#00000000")
        page = document.add_page()
        tiles = TileStore()
        if isinstance(payload, RasterSelectionClipboard):
            obj = document.add_object(page.layer_id, RasterObject(name=payload.source_name))
            tiles.replace_object_tiles(obj.object_id, payload.tiles)
            size = sum(image.sizeInBytes() for image in payload.tiles.values())
        else:
            obj = document.add_object(page.layer_id, VectorDrawingObject(
                name=payload.source_name, strokes=copy.deepcopy(payload.strokes),
            ))
            size = len(json.dumps(obj.to_dict()).encode("utf-8"))
        preview = canvas._render_entity_crop(document, tiles, "object", obj.object_id, maximum=72)
        self.add(HistoryEntry("drawing", payload.source_name, payload, square_thumbnail(preview), size + 72 * 72 * 4, f"drawing:{id(payload)}"))

    def add_object(self, canvas, payload: ObjectClipboard) -> None:
        existing = next((entry for entry in self.entries if entry.payload is payload), None)
        if existing is not None:
            self.add(existing)
            return
        manifest = payload.manifest
        try:
            current = canvas.chapter
            original_available = current is not None and manifest.root_id in (
                current.layers if manifest.root_kind == "layer" else current.objects)
            preview = canvas._render_entity_crop(
                current if original_available else manifest.document,
                canvas.tiles if original_available else payload.tiles,
                manifest.root_kind, manifest.root_id, maximum=72,
                images=canvas.images if original_available else payload.images,
            )
        finally:
            # History owns original compressed files and tiny previews. A solid
            # 8K PNG can decode to 256 MiB despite being only a few KiB on disk.
            for component in [payload, *payload.dependencies]:
                component.images._decoded.clear()
        size = 0
        for component in [payload, *payload.dependencies]:
            size += len(json.dumps(component.manifest.to_dict()).encode("utf-8"))
            size += sum(image.sizeInBytes() for values in component.tiles._tiles.values() for image in values.values())
            size += sum(len(source.data) for source in component.images.snapshot().values())
        self.add(HistoryEntry("object", manifest.name, payload, square_thumbnail(preview), size + 72 * 72 * 4, f"object:{id(payload)}"))


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


def _translate_mask(canvas, mask_id, offset):
    if offset.isNull():
        return
    mask = canvas.chapter.masks[mask_id]
    if mask.gradient is not None:
        _translate_object(mask.gradient, offset.x(), offset.y(), canvas.chapter)
    source = canvas.tiles.object_tiles(mask_id)
    size = canvas.tiles.tile_size
    translated = {}
    for (tile_x, tile_y), image in source.items():
        position = QPointF(tile_x * size, tile_y * size) + offset
        for key in canvas.tiles.keys_for_rect(QRectF(position.x(), position.y(), size, size)):
            tile = translated.get(key)
            if tile is None:
                tile = translated[key] = canvas.tiles._empty(size)
            painter = QPainter(tile)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawImage(position - QPointF(key[0] * size, key[1] * size), image)
            painter.end()
    canvas.tiles.replace_object_tiles(mask_id, translated)
    mask.revision += 1


def paste_object(canvas, payload: ObjectClipboard, parent_id: str, world: QPointF,
                 insertion_index: int | None = None, label="Paste object") -> str:
    """Use independent model/resource identities and one reversible command."""
    before = canvas.chapter.to_dict()
    before_images = canvas.images.snapshot()
    before_selection = canvas._selection_snapshot()
    before_resource_ids = set(canvas.chapter.objects) | set(canvas.chapter.masks)
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
        for mask_id in {identifier for _component, masks in mask_maps for identifier in masks.values()}:
            _translate_mask(canvas, mask_id, offset)
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
        canvas.replace_chapter(before)
        canvas.images.restore(before_images)
        for identifier in created_ids:
            canvas.tiles.remove_object(identifier)
        raise
    after = canvas.chapter.to_dict()
    after_images = canvas.images.snapshot()
    resource_ids = set(created_objects) | (set(canvas.chapter.masks) - before_resource_ids)
    tile_payload = {identifier: canvas.tiles.object_tiles(identifier) for identifier in resource_ids}
    canvas.set_selection(kind, root_id, activate_default_tool=True)
    after_selection = canvas._selection_snapshot()

    def restore(state, resources, selection, with_tiles):
        canvas.replace_chapter(state)
        canvas.images.restore(resources)
        for identifier in resource_ids:
            if with_tiles:
                canvas.tiles.replace_object_tiles(identifier, tile_payload[identifier])
            else:
                canvas.tiles.remove_object(identifier)
        canvas._restore_selection_snapshot(selection)
        canvas.hierarchyChanged.emit()
        canvas.documentChanged.emit(QRectF())
        canvas.update()

    canvas.command_stack.push(CallbackCommand(
        label, lambda: restore(after, after_images, after_selection, True),
        lambda: restore(before, before_images, before_selection, False),
    ), already_done=True)
    canvas.hierarchyChanged.emit()
    canvas.documentChanged.emit(QRectF())
    canvas.interactionFinished.emit()
    canvas.update()
    return root_id
