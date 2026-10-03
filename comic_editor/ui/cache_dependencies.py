"""Stable source identities and regional scene keys shared by both cache tiers."""
from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QRectF, qVersion
from PySide6.QtGui import QFont, QRawFont, QTextLayout

from comic_editor.core.models import TextObject, RasterObject
from comic_editor.render.cache import digest
from comic_editor.render.service import RenderPending, RenderFailed


class RenderDependencies:
    def __init__(self, canvas, backing):
        self.canvas, self.backing = canvas, backing
        self.memo = {}
        self.tile_memo = {}
        self.fonts = {}
        self.hashes = {}
        self.deferred = False

    def file_digest(self, logical, path):
        path = Path(path)
        try:
            stat = path.stat()
        except OSError as error:
            raise RenderFailed(f"Cannot read artwork source: {path.name}") from error
        stamp = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
        record = self.backing.source_digests.get(logical)
        if (isinstance(record, dict) and record.get("stamp") == stamp
                and isinstance(record.get("digest"), str) and len(record["digest"]) == 64
                and record.get("seal") == digest((logical, stamp, record["digest"], record.get("pixels")))):
            return record["digest"]
        marker = str(path), tuple(stamp)
        future = self.hashes.get(marker)
        if future is None:
            for old, previous in list(self.hashes.items()):
                if previous.done() and old != marker:
                    # These may belong to a view abandoned while loading.
                    # Keeping completed work in a small content map avoids
                    # occupying all read slots after a camera/scene change.
                    self.hashes.pop(old)
                    try:
                        if len(self.memo) >= 2048:
                            self.memo.pop(next(iter(self.memo)))
                        self.memo[("file", old)] = previous.result()
                    except OSError:
                        pass
            known = self.memo.get(("file", marker))
            if known is not None:
                self._remember_file(logical, stamp, known)
                return known
            def calculate():
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(block)
                return hasher.hexdigest()
            if self.deferred and len(self.hashes) >= 8:
                raise RenderPending("Checking saved sources")
            future = self.hashes[marker] = self.backing.executor.submit(calculate)
        if self.deferred and not future.done():
            raise RenderPending("Checking saved sources")
        try:
            result = future.result()
        except OSError as error:
            raise RenderFailed(f"Cannot read artwork source: {path.name}") from error
        finally:
            self.hashes.pop(marker, None)
        self._remember_file(logical, stamp, result)
        return result

    def _remember_file(self, logical, stamp, content, pixels=None):
        previous = self.backing.source_digests.get(logical)
        if (pixels is None and isinstance(previous, dict) and previous.get("digest") == content
                and previous.get("seal") == digest((logical, previous.get("stamp"), content,
                                                     previous.get("pixels")))):
            pixels = previous.get("pixels")
        self.backing.source_digests[logical] = dict(stamp=stamp, digest=content, pixels=pixels,
                                                  seal=digest((logical, stamp, content, pixels)))

    def tiles(self, store, object_id, region=None):
        owner = store._tiles.get(object_id)
        if owner is None:
            return ()
        result = []
        for address in sorted(owner):
            if region is not None and not QRectF(address[0] * store.tile_size, address[1] * store.tile_size,
                                                store.tile_size, store.tile_size).intersects(region):
                continue
            version = owner.version(address) if hasattr(owner, "version") else int(owner[address].cacheKey())
            path = owner.backing(address) if hasattr(owner, "backing") else None
            marker = id(store), object_id, address
            previous = self.tile_memo.get(marker)
            known = previous[2] if previous is not None and previous[:2] == (version, str(path)) else None
            if known is None:
                if path is not None:
                    logical = f"tile/{object_id}/{address[0]}_{address[1]}"
                    content = self.file_digest(logical, path)
                    record = self.backing.source_digests[logical]
                    # Saving adopts an encoded backing without changing pixels.
                    # Retain a known dirty-pixel identity across that transition;
                    # the encoded content hash validates it after reopening.
                    pixels = previous[2] if previous is not None and previous[0] == version else None
                    if pixels is not None and pixels[0] == "pixels":
                        self._remember_file(logical, record["stamp"], content, pixels)
                    else:
                        pixels = record.get("pixels")
                    known = tuple(pixels) if pixels is not None else ("encoded", content)
                else:
                    image = owner[address]
                    known = ("pixels", image.width(), image.height(), image.format().value,
                             hashlib.sha256(bytes(image.constBits())).hexdigest())
                self.tile_memo[marker] = version, str(path), known
            result.append((address, known))
        return tuple(result)

    def image(self, store, object_id):
        source = store.source(object_id)
        if source is None:
            return ()
        path = source._encoded.pin.path
        try:
            encoded_size = path.stat().st_size
        except OSError as error:
            raise RenderFailed(f"Cannot read artwork source: {path.name}") from error
        if encoded_size:
            return (self.file_digest(f"image/{object_id}", path),)
        # Programmatically supplied decoded images may have no encoded payload.
        image = store.native_image(object_id)
        return (image.format().value, image.width(), image.height(),
                hashlib.sha256(bytes(image.constBits())).hexdigest())

    def font(self, obj):
        properties = obj.font_family, obj.font_size, obj.bold, obj.italic, obj.text
        if properties not in self.fonts:
            font = QFont(obj.font_family)
            font.setPointSizeF(obj.font_size)
            font.setBold(obj.bold)
            font.setItalic(obj.italic)
            layout = QTextLayout(obj.text, font)
            layout.beginLayout()
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(1e6)
            layout.endLayout()
            fonts = [QRawFont.fromFont(font), *(run.rawFont() for run in layout.glyphRuns())]
            signatures = set()
            for raw in fonts:
                tables = tuple(hashlib.sha256(bytes(raw.fontTable(name))).hexdigest()
                               for name in ("head", "name", "cmap", "glyf", "loca", "CFF ", "CFF2",
                                            "hhea", "hmtx", "vhea", "vmtx", "kern", "GSUB", "GPOS", "GDEF",
                                            "OS/2", "post", "fvar", "gvar", "HVAR", "MVAR", "avar",
                                            "COLR", "CPAL", "CBDT", "CBLC", "sbix", "SVG "))
                signatures.add((raw.familyName(), raw.styleName(), tables))
            if len(self.fonts) >= 128:
                self.fonts.pop(next(iter(self.fonts)))
            self.fonts[properties] = tuple(sorted(signatures))
        return self.fonts[properties]

    def projection_key(self, request, configuration):
        canvas, chapter = self.canvas, self.canvas.chapter
        canvas._render_bounds.prepare()
        region = request.capture_rect
        def branch(layer_id):
            layer = chapter.layers[layer_id]
            if not layer.visible:
                return None
            bounds = canvas._render_bounds.entity_bounds("layer", layer_id)
            # Open containers may have children beyond their own geometry.
            if bounds is not None and not bounds.isEmpty() and not bounds.intersects(region):
                return None
            if (canvas._has_active_modifiers(layer.modifier_ids) or layer.opacity_mask is not None
                    or layer.compound_enabled):
                return canvas._modifier_layer_signature(layer_id)
            children = []
            for ref in layer.children:
                if ref.kind == "layer":
                    value = branch(ref.entity_id)
                else:
                    obj = chapter.objects[ref.entity_id]
                    bounds = canvas._render_bounds.entity_bounds("object", ref.entity_id)
                    value = None
                    if obj.visible and (bounds is None or bounds.isEmpty() or bounds.intersects(region)):
                        pixels = None
                        if (isinstance(obj, RasterObject) and obj.transform_quad is None
                                and not canvas._has_active_modifiers(obj.modifier_ids) and obj.opacity_mask is None):
                            size = canvas.tiles.tile_size
                            transform = canvas.layer_world_transform(obj.parent_layer_id)
                            local = transform.inverted()[0].mapRect(region).translated(-obj.x, -obj.y)
                            pixels = self.tiles(canvas.tiles, obj.object_id, local)
                        signature = canvas._modifier_object_signature(obj, pixel_signature=pixels)
                        value = obj.blend_mode, signature
                if value is not None:
                    children.append((ref.kind, ref.entity_id, value))
            return (canvas._modifier_entity_settings(layer), tuple(children))
        scene = tuple((identifier, branch(identifier)) for identifier in chapter.root_page_ids)
        context = tuple(configuration[3:])
        color = ()
        config = chapter.pixel_contract.ocio_config
        if config:
            path = Path(config)
            color = (self.file_digest("color-config", path),) if path.is_file() else ("missing-color-config", config)
        return (chapter.chapter_id, request.address.level, request.address.x, request.address.y,
                request.tile_size, request.gutter, digest((scene, context, chapter.pixel_contract.signature, color)))

    @staticmethod
    def environment():
        import numpy, PIL, scipy, PyOpenColorIO, platform
        return (qVersion(), numpy.__version__, PIL.__version__, scipy.__version__,
                PyOpenColorIO.__version__, platform.system(), platform.machine())


def backing_for(canvas):
    return getattr(canvas, "_persistent_render_cache", None)


def exact_cache_allowed(canvas, key):
    if (not getattr(canvas, "_projection_exact", False)
            or canvas._projection_has_live_preview()
            or getattr(canvas, "_effect_preview_channel", "canvas") == "navigator"):
        return False
    # Identity-only image keys cannot survive reopening. All reusable semantic
    # scene/stage keys start with a named cache kind; drafts are excluded.
    if not (isinstance(key, tuple) and key and isinstance(key[0], str)):
        return False
    def stable(value):
        if not isinstance(value, (tuple, list)):
            return True
        if value and isinstance(value[0], str):
            if "draft" in value[0] or "preview" in value[0]:
                return False
            if value[0] in {"stage-stack", "stage", "effect-tile", "radial-integration"} and len(value) > 1 and isinstance(value[1], int):
                return False
        return all(stable(item) for item in value)
    return stable(key)


def cache_get(canvas, kind, key):
    backing = backing_for(canvas)
    if backing is None or not exact_cache_allowed(canvas, key):
        return None
    return backing.lookup(kind, key, wait=not getattr(canvas, "_projection_defer_effects", False))


def cache_put(canvas, kind, key, value, *, state=None):
    backing = backing_for(canvas)
    if backing is not None and exact_cache_allowed(canvas, key):
        coordinator = getattr(canvas, "_disk_cache_controller", None)
        if coordinator is not None:
            coordinator.observe(kind, key, state)
        backing.retain(kind, key, value, state=state)
