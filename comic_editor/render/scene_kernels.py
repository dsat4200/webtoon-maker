"""Shared scene kernels for widget adapters and detached evaluation.

These functions preserve the established native sampling pipeline.
Their state owner supplies source revisions and local caches; it need
not be a QWidget or refer to an editor.
"""
from __future__ import annotations
from collections import OrderedDict
from comic_editor.core.tools import ToolKind
from comic_editor.core.models import ArrayModifier
from comic_editor.core.models import BlurModifier
from comic_editor.core.models import BoundGeometry
from comic_editor.core.models import CageTransformModifier
from comic_editor.core.models import ChapterDocument
from comic_editor.core.models import ChildRef
from comic_editor.core.models import ColorFillGradientObject
from comic_editor.core.models import ColorGradientRamp
from comic_editor.core.models import CurvesModifier
from comic_editor.core.models import DistortModifier
from comic_editor.core.models import DitheringModifier
from comic_editor.core.models import DocumentObject
from comic_editor.core.models import GradientObject
from comic_editor.core.models import HalftoneModifier
from comic_editor.core.models import ImageObject
from typing import Iterable
from comic_editor.core.models import KuwaharaModifier
from comic_editor.core.models import LayerNode
from comic_editor.core.models import LimitedMaskGradient
from comic_editor.core.models import MirrorModifier
from comic_editor.core.models import OutlineModifier
from comic_editor.render.shape_outline_compound import OutlineSource
from comic_editor.core.models import PixelateModifier
from PySide6.QtGui import QAbstractTextDocumentLayout
from PySide6.QtGui import QBrush
from PySide6.QtGui import QColor
from PySide6.QtGui import QFont
from PySide6.QtGui import QFontMetricsF
from PySide6.QtGui import QImage
from PySide6.QtGui import QLinearGradient
from PySide6.QtGui import QPainter
from PySide6.QtGui import QPainterPath
from PySide6.QtGui import QPalette
from PySide6.QtGui import QPen
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPolygonF
from PySide6.QtGui import QRadialGradient
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTextBlockFormat
from PySide6.QtGui import QTextCharFormat
from PySide6.QtGui import QTextCursor
from PySide6.QtGui import QTextDocument
from PySide6.QtGui import QTransform
from PySide6.QtCore import Qt
from comic_editor.core.models import RadialBlurModifier
from comic_editor.core.models import RadialGradientField
from comic_editor.core.models import RasterObject
from comic_editor.ui.tiling_rendering import RepeatMapCache
from comic_editor.core.models import SharpnessModifier
from comic_editor.core.models import SolidColorOverlayModifier
from comic_editor.core.models import SpeedLinesGradientObject
from comic_editor.core.models import StrokeModifier
from comic_editor.core.models import TextObject
from comic_editor.core.tiling import TilingGeometry
from comic_editor.core.models import TilingModifier
from comic_editor.core.models import VectorDrawingObject
from comic_editor.core.models import VectorStroke
from comic_editor.core.models import VectorStrokePoint
from comic_editor.render.modifier_rendering import _parameter_field
from comic_editor.render.modifier_rendering import _premultiplied_qimage
from comic_editor.render.modifier_rendering import _qimage_premultiplied
from comic_editor.render.effect_pipeline import aligned
from comic_editor.render.modifier_rendering import apply_opacity_mask
from comic_editor.render.shape_contours import bound_path as compiled_bound_path
from comic_editor.render.shape_outline_compound import compound_outline
from contextlib import contextmanager
import copy
from comic_editor.render.shape_outline import core_mesh
from dataclasses import dataclass
from comic_editor.core.effect_geometry import effect_bounds
from comic_editor.render.effect_pipeline import empty_image
from comic_editor.core.assets import entity_visual_bounds
from dataclasses import field
from comic_editor.core.vector_geometry import flatten_stroke
from comic_editor.render.shape_contours import geometry_key
import json
import math
import numpy as np
from comic_editor.core.effect_geometry import outline_blur_padding
from comic_editor.render.shape_outline import outline_mesh
from comic_editor.core.tiling import polygon_path
from comic_editor.core.effect_geometry import reflection_transform
from comic_editor.render.effect_pipeline import render_stages
from comic_editor.ui.tiling_rendering import repeat_image
from comic_editor.render.shape_outline_compound import ribbon_source
import sys
from comic_editor.core.text_styles import text_color_at
from comic_editor.core.text_styles import text_index_to_qt_position
from comic_editor.core.text_styles import text_indexes_to_qt_positions
from comic_editor.render.shape_contours import transform_stretch
from comic_editor.ui.cage_rendering import warp_image
from comic_editor.render.pixels import (
    current_contract, color_environment, import_image, premultiplied_pixels,
    working_image, working_color, working_rgba,
)

VECTOR_RENDER_CACHE_BUDGET = 64 * 1024 * 1024


@dataclass(frozen=True)
class TopPlan:
    entries: frozenset
    content: frozenset
    branches: frozenset


VECTOR_RENDER_INDEX_CELL = 256.0


class SceneKernels:
    def _vector_cache_budget(self):
        return getattr(self, "_vector_render_cache_budget", VECTOR_RENDER_CACHE_BUDGET)

    def _object_has_effect_modifiers(self, object_id: str) -> bool:
        if self.chapter is None:
            return False
        obj = self.chapter.objects.get(object_id)
        return bool(
            obj is not None and (
                self._object_is_mask_contributor(object_id)
                or self._has_active_modifiers(obj.modifier_ids)
                or obj.opacity_mask is not None
                or any(
                    self._has_active_modifiers(layer.modifier_ids)
                    or layer.opacity_mask is not None
                    for layer in self.chapter.ancestor_layers(
                        obj.parent_layer_id
                    )
                )
            )
        )

    def _active_modifier_instances(
        self, modifier_ids: Iterable[str], *, suppress_outline: bool = False,
    ) -> list:
        from comic_editor.ui.transform_modifier_preview import effective_preview_modifier
        if self.chapter is None:
            return []
        result = []
        for modifier_id in modifier_ids:
            modifier = self.chapter.modifiers.get(modifier_id)
            if modifier is None or modifier.muted:
                continue
            if isinstance(modifier, RadialBlurModifier):
                def maximum(attribute):
                    binding = modifier.parameter_masks.get(attribute)
                    return max(binding.black_value, binding.white_value) if binding else getattr(modifier, attribute)
                if maximum("angle") <= 0 or maximum("intensity") <= 0:
                    continue
            if suppress_outline and isinstance(modifier, OutlineModifier):
                continue
            result.append(effective_preview_modifier(self, modifier))
        return result

    def _has_active_modifiers(self, modifier_ids: Iterable[str]) -> bool:
        return bool(self._active_modifier_instances(modifier_ids))

    def _object_is_mask_contributor(self, object_id: str) -> bool:
        obj = self.chapter.objects.get(object_id) if self.chapter else None
        ancestors = {
            ("layer", layer.layer_id)
            for layer in self.chapter.ancestor_layers(obj.parent_layer_id)
        } if obj is not None else set()
        return bool(
            self.chapter is not None
            and any(
                ("object", object_id) in mask.contributors
                or bool(ancestors.intersection(mask.contributors))
                for mask in self.chapter.masks.values()
            )
        )

    def _active_vector_drawing(self) -> VectorDrawingObject | None:
        if self.chapter is None or self.selected_kind != "object":
            return None
        selected = self.chapter.objects.get(self.selected_id)
        if isinstance(selected, VectorDrawingObject):
            return selected
        return None

    def _drawing_object_transform(
        self,
        obj: RasterObject | VectorDrawingObject,
        destination: list[tuple[float, float]] | None = None,
    ) -> QTransform:
        target = obj.transform_quad if destination is None else destination
        if target is None:
            return QTransform()
        return self._quad_transform(
            QRectF(*self._object_transform_frame(obj)), list(target)
        )

    def _drawing_local_visible_rect(
        self,
        obj: RasterObject | VectorDrawingObject,
        parent_visible: QRectF,
        destination: list[tuple[float, float]] | None = None,
    ) -> QRectF | None:
        target = obj.transform_quad if destination is None else destination
        visible = QRectF(parent_visible)
        if target is not None:
            inverse, valid = self._drawing_object_transform(
                obj, target
            ).inverted()
            if not valid:
                return None
            visible = inverse.mapRect(visible)
        return visible.translated(-obj.x, -obj.y)

    def _layer_parent_transform(self, layer: LayerNode) -> QTransform:
        if (
            self._geometry_transform_target == ("layer_group", layer.layer_id)
            and self._transform_preview_quad is not None
            and layer.bound is not None
        ):
            left, top, width, height = layer.bound.bbox()
            return self._quad_transform(
                QRectF(left, top, max(1.0, width), max(1.0, height)),
                list(self._transform_preview_quad),
            )
        if layer.transform_frame is not None and layer.transform_quad is not None:
            return self._quad_transform(
                QRectF(*layer.transform_frame), list(layer.transform_quad)
            )
        transform = QTransform()
        transform.translate(layer.translate_x, layer.translate_y)
        return transform

    def layer_world_transform(self, layer_id: str) -> QTransform:
        transform = QTransform()
        for layer in self.chapter.ancestor_layers(layer_id):
            transform = self._layer_parent_transform(layer) * transform
        return transform

    def _document_layer_world_transform(
        self, document: ChapterDocument, layer_id: str,
    ) -> QTransform:
        """Resolve a layer's complete local-to-document mapping."""
        transform = QTransform()
        for layer in document.ancestor_layers(layer_id):
            if document is self.chapter:
                local = self._layer_parent_transform(layer)
            elif (
                layer.transform_frame is not None
                and layer.transform_quad is not None
            ):
                local = self._quad_transform(
                    QRectF(*layer.transform_frame),
                    list(layer.transform_quad),
                )
            else:
                local = QTransform()
                local.translate(layer.translate_x, layer.translate_y)
            transform = local * transform
        return transform

    def _raster_world_point(
        self, obj: RasterObject, local: QPointF,
    ) -> QPointF:
        parent_local = QPointF(local.x() + obj.x, local.y() + obj.y)
        if obj.transform_quad is not None:
            parent_local = self._drawing_object_transform(obj).map(
                parent_local
            )
        return self.layer_world_transform(obj.parent_layer_id).map(parent_local)

    def _vector_world_point(
        self, obj: VectorDrawingObject, local: QPointF,
    ) -> QPointF:
        parent_local = QPointF(local.x() + obj.x, local.y() + obj.y)
        if obj.transform_quad is not None:
            parent_local = self._drawing_object_transform(obj).map(parent_local)
        return self.layer_world_transform(obj.parent_layer_id).map(parent_local)

    def _drawing_local_to_world_transform(
        self, obj: RasterObject | VectorDrawingObject,
    ) -> QTransform:
        """Return the projective drawing-local to document transform."""
        frame = QRectF(*self._object_transform_frame(obj)).translated(
            -obj.x, -obj.y
        )
        if frame.width() <= 0 or frame.height() <= 0:
            return QTransform()
        destination: list[tuple[float, float]] = []
        for point in self._rect_quad(frame):
            mapped = (
                self._raster_world_point(obj, QPointF(*point))
                if isinstance(obj, RasterObject)
                else self._vector_world_point(obj, QPointF(*point))
            )
            destination.append(mapped.toTuple())
        return self._quad_transform(frame, destination)

    def camera_transform(self) -> QTransform:
        transform = QTransform()
        transform.translate(self.width() / 2, self.height() / 2)
        transform.rotate(self.rotation)
        transform.scale(self.scale, self.scale)
        transform.translate(-self.center_x, -self.center_y)
        return transform

    def visible_document_rect(self) -> QRectF:
        inverse, valid = self.camera_transform().inverted()
        if not valid:
            return QRectF()
        polygon = inverse.map(QPolygonF(QRectF(self.rect())))
        return polygon.boundingRect()

    def entity_world_rect(
        self, kind: str, entity_id: str,
    ) -> QRectF | None:
        if self.chapter is None:
            return None
        if kind == "object":
            return self.object_world_rect(entity_id)
        layer = self.chapter.layers.get(entity_id)
        if layer is not None and layer.layer_kind == "text_container":
            return self.layer_world_transform(entity_id).mapRect(self._text_container_bounds(layer))
        if layer is None or layer.bound is None:
            return None
        if layer.layer_kind == "open_shape" and not layer.compound_enabled:
            # The interaction/masking frame deliberately ignores per-edge
            # outline edits; effect isolation must include their visual overflow.
            return entity_visual_bounds(self.chapter, self.tiles, kind, entity_id,
                                        geometry_cache=self._outline_cache)
        return self.layer_world_transform(entity_id).map(
            self.layer_effective_path(entity_id)
        ).boundingRect()

    @staticmethod
    def _outline_tolerance(painter, bounds=None):
        transform = painter.combinedTransform()
        stretch = transform_stretch(transform, bounds)
        # Stable power-of-two precision buckets avoid cache churn under zoom.
        return math.ldexp(.125, -math.ceil(math.log2(max(1., stretch))))

    @staticmethod
    def bound_path(bound: BoundGeometry, vertex_radius: float = 0.0) -> QPainterPath:
        return compiled_bound_path(bound, vertex_radius)

    @classmethod
    def open_shape_mesh(
        cls, bound: BoundGeometry, base_width: float,
        extra_width: float = 0.0,
        start_cap: str = "round", end_cap: str = "round",
        *, cache=None, tolerance=.125,
    ) -> QPainterPath:
        return core_mesh(bound, base_width, extra_width, start_cap, end_cap,
                         cache=cache, tolerance=tolerance)

    @classmethod
    def layer_shape_path(cls, layer: LayerNode) -> QPainterPath:
        if layer.layer_kind == "open_shape":
            return cls.open_shape_mesh(
                layer.bound, layer.shape_style.base_thickness,
                layer.shape_style.outline_thickness * 2,
                layer.shape_style.start_cap, layer.shape_style.end_cap,
            )
        return cls.bound_path(layer.bound, layer.vertex_radius)

    def _validate_compound_path_cache(self) -> None:
        if not self._compound_geometry_dirty:
            return
        # Styling is deliberately absent: width/color/visibility edits do not
        # change the compound fill, its intersections, or its attribution.
        signature = None if self.chapter is None else tuple(
            (layer.layer_id, layer.parent_id, layer.visible,
             layer.compound_enabled, layer.compound_operation,
             tuple((r.kind, r.entity_id) for r in layer.children),
             geometry_key(layer.bound) if layer.bound is not None else None,
             layer.translate_x, layer.translate_y, layer.transform_frame,
             tuple(layer.transform_quad or ()),
             (layer.shape_style.base_thickness, layer.shape_style.start_cap,
              layer.shape_style.end_cap,
              tuple(n.width_multiplier for c in layer.bound.iter_contours() for n in c.nodes))
             if layer.layer_kind == "open_shape" and layer.bound is not None else None,
             tuple((m.axis_start, m.axis_end, m.muted, m.intensity > 0,
                    m.compound_operation) for mid in layer.modifier_ids
                   if isinstance(m := self.chapter.modifiers.get(mid), MirrorModifier)),
             tuple((repr(m.grid_dict()), m.muted, m.intensity)
                   for mid in layer.modifier_ids
                   if isinstance(m := self.chapter.modifiers.get(mid), CageTransformModifier)),
             self._modifier_parameter_signature([mid for mid in layer.modifier_ids
                 if isinstance(self.chapter.modifiers.get(mid), StrokeModifier)]))
            for layer in self.chapter.layers.values()
        )
        if signature != self._compound_geometry_signature:
            self._compound_path_cache.clear()
            self._compound_geometry_signature = signature
        self._compound_geometry_dirty = False

    def _layer_operand_path(self, layer: LayerNode, document=None) -> QPainterPath:
        if layer.bound is None:
            return QPainterPath()
        from comic_editor.ui.compound_strokes import appearance
        styled = appearance(self, layer, document)
        if styled is not None:
            return QPainterPath(styled.path)
        if layer.layer_kind == "open_shape":
            return self.open_shape_mesh(
                layer.bound, layer.shape_style.base_thickness, 0,
                layer.shape_style.start_cap, layer.shape_style.end_cap,
                cache=self._outline_cache,
            )
        return self._outline_cache.get(
            ("fill", geometry_key(layer.bound)),
            lambda: self.bound_path(layer.bound, layer.vertex_radius),
        )

    def _document_layer_effective_path(
        self, document: ChapterDocument, layer_id: str,
        cache: dict[str, QPainterPath], *,
        virtual_parent_id: str = "",
        virtual_path_world: QPainterPath | None = None,
        virtual_operation: str = "add",
    ) -> QPainterPath:
        """Build one effective shape, optionally including a virtual child."""
        layer = document.layers[layer_id]
        if not layer.compound_enabled:
            return self._layer_operand_path(layer, document) if layer.layer_kind != "open_shape" else self.layer_shape_path(layer)
        if (document is getattr(self, "chapter", None)
                and cache is getattr(self, "_compound_path_cache", None)):
            self._validate_compound_path_cache()
        cached = cache.get(layer_id)
        if cached is not None:
            return QPainterPath(cached)
        root_inverse, invertible = self._document_layer_world_transform(
            document, layer_id
        ).inverted()
        if not invertible:
            return QPainterPath()
        additions = QPainterPath(self._layer_operand_path(layer, document))
        additions.setFillRule(Qt.OddEvenFill)
        subtractions = QPainterPath()
        subtractions.setFillRule(Qt.OddEvenFill)

        def combine(target: QPainterPath, operand: QPainterPath) -> QPainterPath:
            return QPainterPath(operand) if target.isEmpty() else target.united(operand)

        def collect(parent: LayerNode, ancestor_cages=()) -> None:
            nonlocal additions, subtractions
            for reference in parent.children:
                if reference.kind != "layer":
                    continue
                child = document.layers[reference.entity_id]
                if not child.visible:
                    continue
                operand = (
                    self._document_layer_effective_path(
                        document, child.layer_id, cache,
                        virtual_parent_id=virtual_parent_id,
                        virtual_path_world=virtual_path_world,
                        virtual_operation=virtual_operation,
                    )
                    if child.compound_enabled
                    else self._layer_operand_path(child, document)
                )
                world_operand = self._document_layer_world_transform(document, child.layer_id).map(operand)
                cages = tuple(m for mid in child.modifier_ids
                    if isinstance(m := document.modifiers.get(mid), CageTransformModifier) and not m.muted and m.intensity > 0)
                if cages or ancestor_cages:
                    from comic_editor.ui.cage_rendering import warp_path
                    for cage in (*cages, *ancestor_cages):
                        warped = warp_path(world_operand, cage)
                        world_operand = warped if cage.intensity >= 100 else world_operand.united(warped)
                operand = root_inverse.map(world_operand)
                if child.compound_operation == "subtract":
                    subtractions = combine(subtractions, operand)
                elif child.compound_operation == "add":
                    additions = combine(additions, operand)
                incoming = world_operand
                for modifier_id in child.modifier_ids:
                    modifier = document.modifiers.get(modifier_id)
                    if isinstance(modifier, MirrorModifier) and not modifier.muted and modifier.intensity > 0:
                        reflected = reflection_transform(modifier).map(incoming)
                        if modifier.compound_operation == "add":
                            additions = combine(additions, root_inverse.map(reflected))
                        elif modifier.compound_operation == "subtract":
                            subtractions = combine(subtractions, root_inverse.map(reflected))
                        incoming = incoming.united(reflected)
                if not child.compound_enabled:
                    collect(child, (*cages, *ancestor_cages))

            if (
                parent.layer_id == virtual_parent_id
                and virtual_path_world is not None
                and not virtual_path_world.isEmpty()
            ):
                operand = root_inverse.map(virtual_path_world)
                if virtual_operation == "subtract":
                    subtractions = combine(subtractions, operand)
                elif virtual_operation != "ignore":
                    additions = combine(additions, operand)

        collect(layer)
        result = (
            additions.subtracted(subtractions)
            if not subtractions.isEmpty() else additions
        )
        result.setFillRule(Qt.OddEvenFill)
        cache[layer_id] = QPainterPath(result)
        return result

    def layer_effective_path(self, layer_id: str) -> QPainterPath:
        return self._document_layer_effective_path(
            self.chapter, layer_id, self._compound_path_cache
        )

    def _render_selected_drawing_underlay(
        self, painter: QPainter, visible: QRectF,
    ) -> None:
        if (
            self.chapter is None
            or not self._live_underlay_object_id
            or self._live_underlay_amount <= 0
        ):
            return
        obj = self.chapter.objects.get(self._live_underlay_object_id)
        if (
            not isinstance(obj, (RasterObject, VectorDrawingObject, ImageObject))
            or not obj.visible
            or not self._solo_content_visible("object", obj.object_id)
        ):
            return
        ancestors = self.chapter.ancestor_layers(obj.parent_layer_id)
        if any(not layer.visible or layer.opacity <= 0 for layer in ancestors):
            return
        from comic_editor.ui.native_artwork import paint_overlay
        if paint_overlay(self, painter, visible, self._render_selected_drawing_underlay):
            return
        painter.save()
        painter.setOpacity(
            self.chapter.effective_object_opacity(obj.object_id)
            * self._live_underlay_amount
        )
        parent_transform = self.layer_world_transform(obj.parent_layer_id)
        painter.setTransform(parent_transform, True)
        inverse, valid = parent_transform.inverted()
        local_visible = inverse.mapRect(visible) if valid else visible
        if isinstance(obj, VectorDrawingObject):
            self._render_vector_drawing(
                painter, obj, local_visible
            )
        elif isinstance(obj, RasterObject):
            self._render_raster_content(
                painter, obj, local_visible,
                use_transform_preview=True,
            )
        else:
            self._render_image_object(painter, obj)
        painter.restore()

    def _render_layer(
        self, painter: QPainter, layer: LayerNode, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        if not self._solo_branch_visible(layer.layer_id):
            return
        if layer.mask_only and not self._mask_only_render_visible("layer", layer.layer_id):
            return
        if not layer.visible or (
            layer.opacity <= 0 and layer.opacity_mask is None
            and not self._render_base_alpha
        ):
            return
        if not self._render_bounds.layer_visible(layer, visible_world):
            return
        if not self._render_bounds.layer_clip_visible(painter, layer, visible_world):
            return
        if (
            (
                self._has_active_modifiers(layer.modifier_ids)
                or layer.opacity_mask is not None
            )
            and not self._render_base_alpha
            and ("layer", layer.layer_id) not in self._render_modifier_sources
        ):
            self._render_modified_layer(
                painter, layer, parent_opacity, visible_world
            )
            return
        if self._render_base_alpha and ("layer", layer.layer_id) not in self._render_modifier_sources:
            if self._render_tiled_target(painter, layer, parent_opacity, visible_world):
                return
        painter.save()
        painter.setTransform(self._layer_parent_transform(layer), True)
        inverse, valid = self.layer_world_transform(layer.layer_id).inverted()
        local_visible = (
            inverse.mapRect(visible_world) if valid else QRectF(visible_world)
        )
        layer_opacity = (
            1.0
            if self._render_base_alpha
            or ("layer", layer.layer_id) in self._render_modifier_sources
            else layer.opacity
        )
        self._render_outward_gradient_children(
            painter, layer, parent_opacity * layer_opacity, local_visible
        )
        if layer.layer_kind == "text_container":
            for child in reversed(layer.children):
                self._render_object(painter, self.chapter.objects[child.entity_id],
                                    parent_opacity*layer_opacity, local_visible)
            painter.restore()
            return
        if layer.compound_enabled:
            self._render_compound_layer_contents(
                painter, layer, parent_opacity, visible_world
            )
            painter.restore()
            return
        if layer.layer_kind == "open_shape":
            style = layer.shape_style
            opacity = parent_opacity * layer_opacity
            painter.setOpacity(opacity)
            core = self.open_shape_mesh(
                layer.bound, style.base_thickness, 0,
                style.start_cap, style.end_cap,
                cache=self._outline_cache,
                tolerance=self._outline_tolerance(painter, QRectF(*layer.bound.bbox())),
            )
            if self._solo_content_visible("layer", layer.layer_id):
                painter.fillPath(
                    core, working_color(style.primary_color or "#111111"),
                )
            clip_path = self.open_shape_mesh(
                layer.bound, style.base_thickness,
                style.outline_thickness * 2,
                style.start_cap, style.end_cap,
                cache=self._outline_cache,
                tolerance=self._outline_tolerance(painter, QRectF(*layer.bound.bbox())),
            )
            painter.save()
            painter.setClipPath(clip_path, Qt.IntersectClip)
            for child in reversed(layer.children):
                if self._child_ignores_parent_mask(child):
                    continue
                if child.kind == "layer":
                    self._render_layer(
                        painter, self.chapter.layers[child.entity_id],
                        opacity, visible_world,
                    )
                else:
                    self._render_object(
                        painter, self.chapter.objects[child.entity_id],
                        opacity, local_visible,
                    )
            painter.restore()
            if (style.outline_thickness > 0 and self._solo_content_visible("layer", layer.layer_id)
                    and getattr(self, "_stroke_hide_border_id", None) != layer.layer_id):
                ring = outline_mesh(
                    layer.bound, style.outline_thickness, clip_path,
                    core=core, base_width=style.base_thickness,
                    cache=self._outline_cache,
                    tolerance=self._outline_tolerance(painter, QRectF(*layer.bound.bbox())),
                    start_cap=style.start_cap, end_cap=style.end_cap,
                )
                painter.fillPath(ring, working_color(style.outline_color))
            for child in reversed(layer.children):
                if not self._child_ignores_parent_mask(child):
                    continue
                if child.kind == "layer":
                    self._render_layer(
                        painter, self.chapter.layers[child.entity_id],
                        opacity, visible_world,
                    )
                else:
                    self._render_object(
                        painter, self.chapter.objects[child.entity_id],
                        opacity, local_visible,
                    )
            painter.restore()
            return
        layer_path = self._layer_operand_path(layer)
        opacity = parent_opacity * layer_opacity
        if layer.fill_color and self._solo_content_visible("layer", layer.layer_id):
            painter.save()
            painter.setOpacity(opacity)
            painter.setClipPath(layer_path, Qt.IntersectClip)
            painter.fillPath(layer_path, working_color(layer.fill_color))
            painter.restore()
        painter.save()
        painter.setClipPath(layer_path, Qt.IntersectClip)
        for child in reversed(layer.children):
            if self._child_ignores_parent_mask(child):
                if self._child_explicitly_ignores_parent_mask(child):
                    continue
                # A smudge can extend beyond this mask, but it still belongs
                # at its ordinary position among the layer's children.
                painter.restore()
                if child.kind == "layer":
                    self._render_layer(
                        painter, self.chapter.layers[child.entity_id], opacity,
                        visible_world,
                    )
                else:
                    self._render_object(
                        painter, self.chapter.objects[child.entity_id], opacity,
                        local_visible,
                    )
                painter.save()
                painter.setClipPath(layer_path, Qt.IntersectClip)
                continue
            if child.kind == "layer":
                self._render_layer(
                    painter, self.chapter.layers[child.entity_id], opacity, visible_world
                )
            else:
                self._render_object(
                    painter, self.chapter.objects[child.entity_id], opacity,
                    local_visible,
                )
        if (layer.border_width > 0 and self._solo_content_visible("layer", layer.layer_id)
                and getattr(self, "_stroke_hide_border_id", None) != layer.layer_id):
            painter.save()
            painter.setOpacity(opacity)
            painter.setClipPath(layer_path, Qt.IntersectClip)
            pen = QPen(
                working_color(layer.border_color), layer.border_width * 2,
                Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin,
            )
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            from comic_editor.ui.compound_strokes import appearance, paint_outline
            styled = appearance(self, layer)
            if styled is not None:
                paint_outline(self, painter, layer, layer_path,
                    [OutlineSource(styled.bound, layer.border_width, QTransform(), owner_id=layer.layer_id)],
                    self._outline_tolerance(painter, layer_path.controlPointRect()))
            else:
                from comic_editor.ui.compound_outline_painting import paint_closed_shape_outline
                paint_closed_shape_outline(
                    painter,
                    layer.bound, layer.border_width, layer_path,
                    working_color(layer.border_color),
                    cache=self._outline_cache,
                    tolerance=self._outline_tolerance(painter, layer_path.controlPointRect()),
                )
            painter.restore()
        painter.restore()
        for child in reversed(layer.children):
            if not self._child_explicitly_ignores_parent_mask(child):
                continue
            if child.kind == "layer":
                self._render_layer(
                    painter, self.chapter.layers[child.entity_id],
                    opacity, visible_world,
                )
            else:
                self._render_object(
                    painter, self.chapter.objects[child.entity_id],
                    opacity, local_visible,
                )
        painter.restore()
        return

    def _render_modified_layer(
        self, painter: QPainter, layer: LayerNode, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        from comic_editor.ui.compound_strokes import scoped
        if scoped(self, layer) and layer.opacity_mask is None and all(
                isinstance(m, StrokeModifier) for m in self._active_modifier_instances(layer.modifier_ids)):
            self._render_modifier_sources.add(("layer", layer.layer_id))
            try:
                # Local stroke effects are already in this operand. Preserve
                # normal subtree composition and the owner's opacity here.
                self._render_layer(painter, layer, parent_opacity*layer.opacity, visible_world)
            finally:
                self._render_modifier_sources.discard(("layer", layer.layer_id))
            return
        if self._render_tiled_target(painter, layer, parent_opacity, visible_world):
            return
        if (self._interactive_render
                and getattr(self, "_effect_preview_channel", "canvas") == "navigator"):
            self._render_mirror_target(painter, layer, parent_opacity, visible_world)
            return
        if any(isinstance(m, SolidColorOverlayModifier) for m in self._active_modifier_instances(layer.modifier_ids)):
            self._render_mirror_target(painter, layer, parent_opacity, visible_world)
            return
        # Curves affects the complete subtree, including children outside a
        # page/shape mask. The staged capture includes their visual bounds.
        if layer.layer_kind == "text_container" or any(isinstance(m, (MirrorModifier, ArrayModifier, RadialBlurModifier, CageTransformModifier, StrokeModifier, HalftoneModifier, PixelateModifier, DistortModifier, CurvesModifier, KuwaharaModifier, DitheringModifier, SharpnessModifier)) for m in self._active_modifier_instances(layer.modifier_ids)):
            self._render_mirror_target(painter, layer, parent_opacity, visible_world)
            return
        world_bounds = self.entity_world_rect("layer", layer.layer_id)
        selection_bounds = self._raster_selection_capture_bounds("layer", layer.layer_id)
        if not selection_bounds.isEmpty():
            world_bounds = (selection_bounds if world_bounds is None else
                            world_bounds.united(selection_bounds))
        modifiers = self._active_modifier_instances(
            layer.modifier_ids,
            suppress_outline=getattr(
                self, "_suppress_outline_for_mask", False
            ),
        )
        if (
            world_bounds is None or world_bounds.isEmpty()
            or (not modifiers and layer.opacity_mask is None)
        ):
            self._render_modifier_sources.add(("layer", layer.layer_id))
            try:
                self._render_layer(painter, layer, parent_opacity, visible_world)
            finally:
                self._render_modifier_sources.discard(("layer", layer.layer_id))
            return
        parent_transform = (
            self.layer_world_transform(layer.parent_id)
            if layer.parent_id else QTransform()
        )
        parent_inverse, valid = parent_transform.inverted()
        if not valid:
            return
        local = parent_inverse.mapRect(world_bounds)
        if layer.layer_kind == "open_shape":
            padding = (
                layer.shape_style.base_thickness / 2
                + layer.shape_style.outline_thickness + 2
            )
            local.adjust(-padding, -padding, padding, padding)
        expansion = sum(
            self._modifier_maximum(
                modifier, "strength", modifier.strength
            ) * 3.0
            if isinstance(modifier, BlurModifier)
            else 25.0 + outline_blur_padding(modifier)
            if isinstance(modifier, OutlineModifier)
            else 0.0
            for modifier in modifiers
        )
        local.adjust(-expansion, -expansion, expansion, expansion)
        bounds = QRectF(
            math.floor(local.left()), math.floor(local.top()),
            max(1, math.ceil(local.right()) - math.floor(local.left())),
            max(1, math.ceil(local.bottom()) - math.floor(local.top())),
        )
        if not bounds.intersects(parent_inverse.mapRect(visible_world)):
            return
        from comic_editor.render.tile_effects import generic_target_output
        tiled = generic_target_output(self, layer, bounds, modifiers, parent_transform, visible_world)
        if tiled:
            processed, output_bounds = tiled
            painter.save()
            painter.setOpacity(parent_opacity * layer.opacity)
            if not any(isinstance(modifier, OutlineModifier) for modifier in modifiers):
                painter.setClipPath(self._layer_parent_transform(layer).map(self.layer_effective_path(layer.layer_id)),
                                    Qt.ClipOperation.IntersectClip)
            painter.drawImage(output_bounds.topLeft(), processed)
            painter.restore()
            return
        from comic_editor.ui.interactive_effects import outline_capture_bounds
        bounds = outline_capture_bounds(
            self, painter, bounds,
            parent_inverse.mapRect(self._modifier_viewport_region(visible_world)), modifiers)
        capture_world = parent_transform.mapRect(bounds)
        world_origin = parent_transform.map(bounds.topLeft())
        from comic_editor.render.effect_regions import region_scope
        request_scope = region_scope(self, self._effect_request_scope("layer", layer.layer_id), bounds)
        layer_signature = self._modifier_layer_signature(layer.layer_id)
        from comic_editor.ui import translation_cache
        move_key = translation_cache.output_key(self, layer, bounds, parent_transform, modifiers)
        move_revision = getattr(self, "_effect_provisional_revision", 0)
        cache_key = (
            "layer", layer.layer_id,
            layer_signature,
            self._render_exclude_text,
            self._rect_signature(bounds), world_origin.toTuple(),
            self._modifier_mapping_signature(parent_transform),
        )
        from comic_editor.ui.interactive_effects import render_interactive_stack
        processed = translation_cache.get(self, move_key)
        if processed is None:
            processed = self._modifier_cache_get(cache_key)
        provisional = False
        if processed is None:
            processed = self._cached_modifier_output(
                cache_key, request_scope,
                layer.opacity_mask, bounds, parent_transform, capture_world)
        if processed is None:
            source_key = (
                "layer-source", layer.layer_id,
                layer_signature[0], layer_signature[3], layer_signature[4],
                self._render_exclude_text,
                self._rect_signature(bounds), world_origin.toTuple(),
                self._modifier_mapping_signature(parent_transform),
            )
            source_provisional = False
            image = self._modifier_source_cache_get(source_key)
            if image is None:
                revision = getattr(self, "_effect_provisional_revision", 0)
                image = QImage(
                    max(1, math.ceil(bounds.width())),
                    max(1, math.ceil(bounds.height())),
                    current_contract().image_format,
                )
                image.fill(Qt.GlobalColor.transparent)
                source = QPainter(image)
                source.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                source.translate(-bounds.left(), -bounds.top())
                self._render_modifier_sources.add(("layer", layer.layer_id))
                try:
                    self._render_layer(source, layer, 1.0, capture_world)
                    drawing = self._active_vector_drawing()
                    modified_ancestors = (
                        [
                            candidate.layer_id
                            for candidate in self.chapter.ancestor_layers(
                                drawing.parent_layer_id
                            )
                            if self._has_active_modifiers(
                                candidate.modifier_ids
                            )
                        ]
                        if drawing is not None else []
                    )
                    if (
                        drawing is not None
                        and not self._has_active_modifiers(
                            drawing.modifier_ids
                        )
                        and modified_ancestors
                        and modified_ancestors[-1] == layer.layer_id
                    ):
                        self._render_modified_vector_pencil_preview(
                            source, layer.parent_id or ""
                        )
                finally:
                    self._render_modifier_sources.discard(
                        ("layer", layer.layer_id)
                    )
                    source.end()
                source_provisional = revision != getattr(self, "_effect_provisional_revision", 0)
                if not source_provisional:
                    self._modifier_source_cache_put(source_key, image)
            width, height = image.width(), image.height()
            world_to_image = self._world_to_image_transform(
                parent_transform, bounds, width, height
            )
            processed, provisional = render_interactive_stack(
                self, image, modifiers, world_origin.toTuple(),
                self._modifier_mask_fields(
                    modifiers, width, height,
                    world_to_image, capture_world,
                ),
                cache_key=("interactive-stack", cache_key),
                scope=request_scope,
                upstream_provisional=source_provisional,
            )
            if layer.opacity_mask is not None:
                binding = layer.opacity_mask
                processed = apply_opacity_mask(
                    processed,
                    self.render_tone_mask_field(
                        binding.mask_id, width, height,
                        world_to_image, capture_world,
                    ),
                    binding.black_value, binding.white_value,
                )
            if not provisional:
                self._modifier_cache_put(cache_key, processed)
        if not provisional:
            self._retain_modifier_output(cache_key, request_scope, processed)
            translation_cache.put(self, move_key, processed, move_revision)
        painter.save()
        painter.setOpacity(parent_opacity * layer.opacity)
        outline_overflows = any(
            isinstance(modifier, OutlineModifier)
            for modifier in modifiers
        )
        if not outline_overflows:
            transform = self._layer_parent_transform(layer)
            painter.setClipPath(
                transform.map(self.layer_effective_path(layer.layer_id)),
                Qt.ClipOperation.IntersectClip,
            )
        painter.drawImage(bounds.topLeft(), processed)
        painter.restore()

    def _compound_outline_mesh(self, layer, path, tolerance=.125, *, sources_only=False):
        """Attribute surviving compound boundaries, including reflected edges."""
        root_inverse, valid = self.layer_world_transform(layer.layer_id).inverted()
        if not valid:
            return [] if sources_only else QPainterPath()

        local_mappings = {}

        def operand_mapping(item, post):
            if not post.isIdentity():
                # Reflections are defined in document space, so their
                # conjugation must retain the complete world transform.
                return self.layer_world_transform(item.layer_id) * post * root_inverse
            cached = local_mappings.get(item.layer_id)
            if cached is not None:
                return cached
            # Compose only the descendant chain. Multiplying a moving root
            # by its inverse introduces tiny rounding differences every frame
            # and needlessly rebuilds the entire attributed outline.
            mapping = QTransform()
            cursor = item
            while cursor.layer_id != layer.layer_id:
                mapping = mapping * self._layer_parent_transform(cursor)
                cursor = self.chapter.layers[cursor.parent_id]
            local_mappings[item.layer_id] = mapping
            return mapping

        def operand_sources(item, post):
            result = []
            if item.bound is not None:
                mapping = operand_mapping(item, post)
                if item.layer_kind == "open_shape":
                    source = ribbon_source(item.bound, item.border_width, mapping,
                        item.shape_style.base_thickness, item.shape_style.start_cap,
                        item.shape_style.end_cap, self._outline_cache)
                    if source is not None:
                        result.append(source)
                else:
                    from comic_editor.ui.compound_strokes import appearance
                    styled = appearance(self, item)
                    result.append(OutlineSource(styled.bound if styled is not None else item.bound,
                        item.border_width, mapping, owner_id=item.layer_id))
            if item.compound_enabled:
                result.extend(contributions(item, post))
            return result

        def contributions(parent, post):
            result = []
            for ref in parent.children:
                if ref.kind != "layer":
                    continue
                child = self.chapter.layers[ref.entity_id]
                if not child.visible:
                    continue
                if child.compound_operation != "ignore":
                    result.extend(operand_sources(child, post))
                # Mirror operates on incoming coverage, including earlier copies.
                incoming = [QTransform()]
                for mid in child.modifier_ids:
                    modifier = self.chapter.modifiers.get(mid)
                    if not isinstance(modifier, MirrorModifier) or modifier.muted or modifier.intensity <= 0:
                        continue
                    reflected = [t * reflection_transform(modifier) for t in incoming]
                    if modifier.compound_operation != "ignore":
                        for transform in reflected:
                            result.extend(operand_sources(child, transform * post))
                    incoming += reflected
                if not child.compound_enabled:
                    result.extend(contributions(child, post))
            return result

        sources = operand_sources(layer, QTransform())
        if sources_only:
            return sources
        return compound_outline(path, layer.border_width, sources,
                                self._outline_cache, tolerance)

    def _render_compound_layer_contents(
        self, painter: QPainter, layer: LayerNode, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        layer_path = self.layer_effective_path(layer.layer_id)
        opacity = parent_opacity * (
            1.0 if self._render_base_alpha
            or ("layer", layer.layer_id) in self._render_modifier_sources
            else layer.opacity
        )
        inverse, valid = self.layer_world_transform(layer.layer_id).inverted()
        local_visible = inverse.mapRect(visible_world) if valid else visible_world
        painter.save()
        painter.setClipPath(layer_path, Qt.IntersectClip)
        if layer.fill_color and self._solo_content_visible("layer", layer.layer_id):
            painter.save()
            painter.setOpacity(opacity)
            painter.fillPath(layer_path, working_color(layer.fill_color))
            painter.restore()
        for child in reversed(layer.children):
            if self._child_ignores_parent_mask(child):
                continue
            if child.kind == "object":
                self._render_object(
                    painter, self.chapter.objects[child.entity_id], opacity,
                    local_visible,
                )
                continue
            candidate = self.chapter.layers[child.entity_id]
            if candidate.compound_operation == "ignore":
                self._render_layer(
                    painter, candidate, opacity, visible_world
                )
            elif candidate.visible:
                self._render_compound_contributor(
                    painter, candidate, opacity, visible_world
                )
        self._render_compound_reference_objects(
            painter, layer.layer_id, opacity, visible_world
        )
        painter.restore()
        if (layer.border_width > 0 and self._solo_content_visible("layer", layer.layer_id)
                and getattr(self, "_stroke_hide_border_id", None) != layer.layer_id):
            painter.save()
            painter.setOpacity(opacity)
            pen = QPen(
                working_color(layer.border_color), layer.border_width * 2,
                Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin,
            )
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            from comic_editor.ui.compound_strokes import paint_outline
            tolerance = self._outline_tolerance(painter, layer_path.controlPointRect())
            sources = self._compound_outline_mesh(layer, layer_path, tolerance, sources_only=True)
            paint_outline(self, painter, layer, layer_path, sources, tolerance)
            painter.restore()
        for child in reversed(layer.children):
            if not self._child_ignores_parent_mask(child):
                continue
            if child.kind == "object":
                self._render_object(
                    painter, self.chapter.objects[child.entity_id], opacity,
                    local_visible,
                )
            else:
                self._render_layer(
                    painter, self.chapter.layers[child.entity_id],
                    opacity, visible_world,
                )

    def _render_compound_contributor(
        self, painter: QPainter, layer: LayerNode, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        if not layer.visible or not self._solo_branch_visible(layer.layer_id):
            return
        if self._show_on_top_standalone_contributor(layer):
            self._render_layer(painter, layer, parent_opacity, visible_world)
            return
        # Compound operands bypass _render_layer, so their color adjustment
        # needs its own isolated-content route; geometry-only captures skip it.
        if ("layer", layer.layer_id) not in self._render_modifier_sources and any(
                isinstance(modifier, (MirrorModifier, ArrayModifier, RadialBlurModifier,
                    CageTransformModifier, HalftoneModifier, PixelateModifier, DistortModifier))
                or isinstance(modifier, (CurvesModifier, KuwaharaModifier, DitheringModifier,
                                         SharpnessModifier, SolidColorOverlayModifier)) and not self._render_base_alpha
                for modifier in self._active_modifier_instances(layer.modifier_ids)):
            self._render_mirror_target(painter, layer, parent_opacity, visible_world)
            return
        painter.save()
        painter.setTransform(self._layer_parent_transform(layer), True)
        path = (
            self.layer_effective_path(layer.layer_id)
            if layer.compound_enabled else self._layer_operand_path(layer)
        )
        painter.save()
        painter.setClipPath(path, Qt.IntersectClip)
        opacity = parent_opacity * (
            1.0 if self._render_base_alpha
            or ("layer", layer.layer_id) in self._render_modifier_sources
            else layer.opacity
        )
        inverse, valid = self.layer_world_transform(layer.layer_id).inverted()
        local_visible = inverse.mapRect(visible_world) if valid else visible_world
        for child in reversed(layer.children):
            if self._child_ignores_parent_mask(child):
                continue
            if child.kind == "object":
                self._render_object(
                    painter, self.chapter.objects[child.entity_id], opacity,
                    local_visible,
                )
                continue
            candidate = self.chapter.layers[child.entity_id]
            if candidate.compound_operation == "ignore":
                self._render_layer(
                    painter, candidate, opacity, visible_world
                )
            elif candidate.visible:
                self._render_compound_contributor(
                    painter, candidate, opacity, visible_world
                )
        if layer.compound_enabled:
            self._render_compound_reference_objects(
                painter, layer.layer_id, opacity, visible_world
            )
        painter.restore()
        for child in reversed(layer.children):
            if not self._child_ignores_parent_mask(child):
                continue
            if child.kind == "object":
                self._render_object(
                    painter, self.chapter.objects[child.entity_id],
                    opacity, local_visible,
                )
            else:
                self._render_layer(
                    painter, self.chapter.layers[child.entity_id],
                    opacity, visible_world,
                )
        painter.restore()

    def _child_ignores_parent_mask(self, child: ChildRef) -> bool:
        entity = (
            self.chapter.layers.get(child.entity_id)
            if child.kind == "layer"
            else self.chapter.objects.get(child.entity_id)
        )
        return bool(entity and (
            entity.ignore_parent_mask or self._entity_has_smudge_overflow(entity)
        ))

    def _child_explicitly_ignores_parent_mask(self, child: ChildRef) -> bool:
        entity = (
            self.chapter.layers.get(child.entity_id)
            if child.kind == "layer"
            else self.chapter.objects.get(child.entity_id)
        )
        return bool(entity and entity.ignore_parent_mask)

    def _entity_has_smudge_overflow(self, entity) -> bool:
        return any(
            isinstance(modifier, DistortModifier)
            and modifier.modifier_type == "distort_smudge"
            and modifier.parameters.get("strokes")
            and modifier.intensity > 0
            for modifier in self._active_modifier_instances(entity.modifier_ids)
        )

    @staticmethod
    def _is_outward_gradient(obj: GradientObject) -> bool:
        return bool(
            (
                obj.field_type == "radial"
                and obj.radial_field.reverse_direction
            )
            or (
                obj.field_type == "parent_shape"
                and obj.shape_field.reverse_direction
            )
        )

    def _render_outward_gradient_children(
        self, painter: QPainter, layer: LayerNode, opacity: float,
        local_visible: QRectF,
    ) -> None:
        """Render outward gradients before their direct parent artwork."""
        previous = self._rendering_outward_gradient
        self._rendering_outward_gradient = True
        try:
            for child in reversed(layer.children):
                if child.kind != "object":
                    continue
                obj = self.chapter.objects.get(child.entity_id)
                if (
                    isinstance(obj, GradientObject)
                    and self._is_outward_gradient(obj)
                ):
                    self._render_object(
                        painter, obj, opacity, local_visible
                    )
        finally:
            self._rendering_outward_gradient = previous

    def _render_compound_reference_objects(
        self, painter: QPainter, compound_id: str, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        compound_inverse, valid = self.layer_world_transform(
            compound_id
        ).inverted()
        if not valid:
            return
        references: list[DocumentObject] = []

        def collect(layer: LayerNode) -> None:
            if layer.layer_id != compound_id and not layer.visible:
                return
            for child in reversed(layer.children):
                if child.kind == "object":
                    obj = self.chapter.objects[child.entity_id]
                    closest = self.chapter.closest_compound_ancestor(
                        obj.parent_layer_id, include_self=True
                    )
                    if (
                        obj.geometry_reference == "compound"
                        and closest is not None
                        and closest.layer_id == compound_id
                    ):
                        references.append(obj)
                else:
                    collect(self.chapter.layers[child.entity_id])

        collect(self.chapter.layers[compound_id])
        self._rendering_compound_references = True
        try:
            for obj in references:
                parent_transform = self.layer_world_transform(
                    obj.parent_layer_id
                )
                branch_opacity = parent_opacity
                cursor = self.chapter.layers[obj.parent_layer_id]
                while cursor.layer_id != compound_id:
                    branch_opacity *= cursor.opacity
                    if cursor.parent_id is None:
                        break
                    cursor = self.chapter.layers[cursor.parent_id]
                painter.save()
                painter.setTransform(
                    parent_transform * compound_inverse, True
                )
                parent_inverse, invertible = parent_transform.inverted()
                local_visible = (
                    parent_inverse.mapRect(visible_world)
                    if invertible else visible_world
                )
                self._render_object(
                    painter, obj, branch_opacity, local_visible
                )
                painter.restore()
        finally:
            self._rendering_compound_references = False

    @staticmethod
    def _vector_cache_entry_bytes(
        value: tuple[QImage, QRectF],
    ) -> int:
        image = value[0]
        return max(0, int(image.sizeInBytes()))

    def _store_vector_render_cache(
        self, key: tuple, value: tuple[QImage, QRectF],
    ) -> None:
        entry_bytes = self._vector_cache_entry_bytes(value)
        previous = self._vector_render_cache.pop(key, None)
        if previous is not None:
            self._vector_render_cache_bytes -= (
                self._vector_cache_entry_bytes(previous)
            )
        if entry_bytes > self._vector_cache_budget():
            self._vector_render_cache_bytes = max(
                0, self._vector_render_cache_bytes
            )
            return
        self._vector_render_cache[key] = value
        self._vector_render_cache_bytes += entry_bytes
        while (
            self._vector_render_cache
            and self._vector_render_cache_bytes
            > self._vector_cache_budget()
        ):
            oldest = next(iter(self._vector_render_cache))
            removed = self._vector_render_cache.pop(oldest)
            self._vector_render_cache_bytes -= (
                self._vector_cache_entry_bytes(removed)
            )
        self._vector_render_cache_bytes = max(
            0, self._vector_render_cache_bytes
        )

    def _requested_vector_render_scale(self) -> float:
        from comic_editor.render.sampling import artwork_density
        if self._vector_render_scale_override is not None:
            return artwork_density(self._vector_render_scale_override)
        return 1.0

    def _vector_stroke_indexes(
        self, drawing: VectorDrawingObject, visible: QRectF | None,
    ) -> list[int]:
        """Return visible stroke indexes in their original paint order."""
        if visible is None:
            return list(range(len(drawing.strokes)))
        # Point/handle previews can move geometry without touching the model
        # revision.  Do not consult stale cells while such an edit is live.
        if (
            drawing.object_id == self.selected_object_id
            and (
                self._selection_vector_preview
                or self._vector_gesture_mode in {
                    "edit_drag", "redraw", "simplify", "connect",
                }
            )
        ):
            return list(range(len(drawing.strokes)))
        revision = (drawing.drawing_revision, len(drawing.strokes))
        index = self._vector_spatial_indexes.get(drawing.object_id)
        if index is None or index["revision"] != revision:
            cell = VECTOR_RENDER_INDEX_CELL
            cells: dict[tuple[int, int], list[int]] = {}
            global_strokes: list[int] = []
            for stroke_index, stroke in enumerate(drawing.strokes):
                if not stroke.points:
                    continue
                bounds = QRectF(*stroke.derived_bounds())
                if not all(math.isfinite(value) for value in (
                    bounds.left(), bounds.right(),
                    bounds.top(), bounds.bottom(),
                )):
                    global_strokes.append(stroke_index)
                    continue
                left = math.floor(bounds.left() / cell)
                right = math.floor(bounds.right() / cell)
                top = math.floor(bounds.top() / cell)
                bottom = math.floor(bounds.bottom() / cell)
                cell_count = (right - left + 1) * (bottom - top + 1)
                if cell_count > 4096:
                    global_strokes.append(stroke_index)
                    continue
                for y in range(top, bottom + 1):
                    for x in range(left, right + 1):
                        cells.setdefault((x, y), []).append(stroke_index)
            index = {
                "revision": revision,
                "cells": cells,
                "global": global_strokes,
            }
            self._vector_spatial_indexes[drawing.object_id] = index
        cell = VECTOR_RENDER_INDEX_CELL
        if not all(math.isfinite(value) for value in (
            visible.left(), visible.right(),
            visible.top(), visible.bottom(),
        )):
            return list(range(len(drawing.strokes)))
        left = math.floor(visible.left() / cell)
        right = math.floor(visible.right() / cell)
        top = math.floor(visible.top() / cell)
        bottom = math.floor(visible.bottom() / cell)
        candidates = set(index["global"])
        query_cells = (right - left + 1) * (bottom - top + 1)
        if query_cells > max(4096, len(index["cells"]) * 4):
            for (x, y), stroke_indexes in index["cells"].items():
                if left <= x <= right and top <= y <= bottom:
                    candidates.update(stroke_indexes)
        else:
            for y in range(top, bottom + 1):
                for x in range(left, right + 1):
                    candidates.update(index["cells"].get((x, y), ()))
        return sorted(candidates)

    def _vector_stroke_image(
        self, drawing: VectorDrawingObject, stroke: VectorStroke,
        *, cache_token: object | None = None,
    ) -> tuple[QImage, QRectF] | None:
        """Rasterize one stroke opacity mask, then colorize it exactly once."""
        if not stroke.points:
            return None
        requested_scale = self._requested_vector_render_scale()
        key = (
            drawing.object_id,
            stroke.stroke_id,
            stroke.render_revision if cache_token is None else cache_token,
            getattr(stroke, "_scene_generation", None),
            stroke.color,
            stroke.closed,
            stroke.start_cap,
            stroke.end_cap,
            tuple(stroke.clip_polygon or ()), stroke.tiling_group,
            round(requested_scale, 3),
            current_contract().signature, color_environment(current_contract()),
        )
        cached = self._vector_render_cache.get(key)
        if cached is not None:
            self._vector_render_cache.pop(key, None)
            self._vector_render_cache[key] = cached
            return cached
        left, top, width, height = stroke.derived_bounds()
        padding = 3.0 / requested_scale
        target = QRectF(
            left - padding, top - padding,
            max(1.0, width + padding * 2),
            max(1.0, height + padding * 2),
        )
        if stroke.clip_polygon:
            target = aligned(target)
        render_scale = requested_scale
        maximum_dimension = max(target.width(), target.height()) * render_scale
        if maximum_dimension > 8192:
            render_scale *= 8192 / maximum_dimension
        pixel_width = max(1, math.ceil(target.width() * render_scale))
        pixel_height = max(1, math.ceil(target.height() * render_scale))
        mask = QImage(pixel_width, pixel_height, QImage.Format_Alpha8)
        mask.fill(0)
        mask_painter = QPainter(mask)
        mask_painter.setRenderHint(QPainter.Antialiasing, True)
        mask_painter.setCompositionMode(QPainter.CompositionMode_Lighten)
        mask_painter.scale(render_scale, render_scale)
        mask_painter.translate(-target.left(), -target.top())
        if stroke.clip_polygon:
            from comic_editor.core.tiling import polygon_path
            mask_painter.setClipPath(polygon_path(stroke.clip_polygon))
        if len(stroke.points) == 1:
            point = stroke.points[0]
            mask_painter.setPen(Qt.NoPen)
            mask_painter.setBrush(QColor(
                255, 255, 255,
                round(max(0.0, min(1.0, point.opacity)) * 255),
            ))
            mask_painter.drawEllipse(
                QPointF(point.x, point.y), point.width / 2, point.width / 2
            )
        else:
            samples = flatten_stroke(
                stroke.points, closed=stroke.closed, tolerance=0.3
            )
            raster_samples: list[
                tuple[tuple[float, float], float, float]
            ] = []
            for first, second in zip(samples, samples[1:]):
                length = math.dist(first.point, second.point)
                steps = max(1, math.ceil(length * render_scale / 3))
                if not raster_samples:
                    raster_samples.append(
                        (first.point, first.width, first.opacity)
                    )
                for step in range(1, steps + 1):
                    amount = step / steps
                    current_point = (
                        first.point[0]
                        + (second.point[0] - first.point[0]) * amount,
                        first.point[1]
                        + (second.point[1] - first.point[1]) * amount,
                    )
                    current_width = (
                        first.width + (second.width - first.width) * amount
                    )
                    current_opacity = (
                        first.opacity
                        + (second.opacity - first.opacity) * amount
                    )
                    raster_samples.append(
                        (current_point, current_width, current_opacity)
                    )
            for first, second in zip(raster_samples, raster_samples[1:]):
                opacity = max(
                    0.0, min(1.0, (first[2] + second[2]) / 2)
                )
                pen = QPen(
                    QColor(255, 255, 255, round(opacity * 255)),
                    max(1.0, (first[1] + second[1]) / 2),
                    Qt.SolidLine,
                    Qt.FlatCap,
                    Qt.RoundJoin,
                )
                mask_painter.setPen(pen)
                mask_painter.drawLine(
                    QPointF(*first[0]), QPointF(*second[0])
                )
            mask_painter.setPen(Qt.NoPen)
            for point, width, opacity in raster_samples[1:-1]:
                mask_painter.setBrush(QColor(
                    255, 255, 255,
                    round(max(0.0, min(1.0, opacity)) * 255),
                ))
                mask_painter.drawEllipse(
                    QPointF(*point), width / 2, width / 2
                )

            def draw_cap(
                endpoint, neighbor, cap: str, outward: bool,
            ) -> None:
                point, width, opacity = endpoint
                direction = QPointF(
                    point[0] - neighbor[0][0],
                    point[1] - neighbor[0][1],
                )
                magnitude = math.hypot(direction.x(), direction.y())
                if magnitude <= 1.0e-8:
                    return
                direction /= magnitude
                if not outward:
                    direction = -direction
                normal = QPointF(-direction.y(), direction.x())
                radius = width / 2
                mask_painter.setBrush(QColor(
                    255, 255, 255,
                    round(max(0.0, min(1.0, opacity)) * 255),
                ))
                if cap == "round":
                    mask_painter.drawEllipse(
                        QPointF(*point), radius, radius
                    )
                elif cap == "point":
                    center = QPointF(*point)
                    mask_painter.drawPolygon(QPolygonF([
                        center + normal * radius,
                        center + direction * radius,
                        center - normal * radius,
                    ]))
                elif cap == "square":
                    center = QPointF(*point) + direction * (radius / 2)
                    mask_painter.drawPolygon(QPolygonF([
                        center + normal * radius - direction * (radius / 2),
                        center - normal * radius - direction * (radius / 2),
                        center - normal * radius + direction * (radius / 2),
                        center + normal * radius + direction * (radius / 2),
                    ]))

            if raster_samples and not stroke.closed:
                draw_cap(
                    raster_samples[0], raster_samples[1],
                    stroke.start_cap, True,
                )
                draw_cap(
                    raster_samples[-1], raster_samples[-2],
                    stroke.end_cap, True,
                )
            elif raster_samples and stroke.closed:
                point, width, opacity = raster_samples[0]
                mask_painter.setBrush(QColor(
                    255, 255, 255,
                    round(max(0.0, min(1.0, opacity)) * 255),
                ))
                mask_painter.drawEllipse(
                    QPointF(*point), width / 2, width / 2
                )
        mask_painter.end()
        image = QImage(
            pixel_width, pixel_height, current_contract().image_format
        )
        image.fill(Qt.transparent)
        image_painter = QPainter(image)
        image_painter.fillRect(image.rect(), working_color(stroke.color))
        image_painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        image_painter.drawImage(0, 0, mask)
        image_painter.end()
        result = image, target
        self._store_vector_render_cache(key, result)
        return result

    def _vector_stroke_with_selection_preview(
        self, stroke: VectorStroke,
    ) -> VectorStroke:
        if not self._selection_vector_preview:
            return stroke
        preview_points = {
            point.point_id: self._selection_vector_preview[point.point_id]
            for point in stroke.points
            if point.point_id in self._selection_vector_preview
        }
        if not preview_points:
            return stroke
        clip_polygon = copy.deepcopy(stroke.clip_polygon)
        if clip_polygon and len(preview_points) == len(stroke.points):
            drawing = self._drawing_selection_object()
            if drawing is not None and self._selection_transform_start_quad and self._selection_transform_quad:
                world = self._quad_to_quad_transform(self._selection_transform_start_quad, self._selection_transform_quad)
                mapping = self._drawing_selection_transform(drawing)
                local = mapping*world*mapping.inverted()[0]
                clip_polygon = [local.map(QPointF(*p)).toTuple() for p in clip_polygon]
        return VectorStroke(
            stroke_id=stroke.stroke_id,
            color=stroke.color,
            closed=stroke.closed,
            start_cap=stroke.start_cap,
            end_cap=stroke.end_cap,
            clip_polygon=clip_polygon, tiling_group=stroke.tiling_group,
            points=[
                VectorStrokePoint(
                    point_id=point.point_id,
                    x=preview_points.get(point.point_id, {}).get(
                        "position", point.position
                    )[0],
                    y=preview_points.get(point.point_id, {}).get(
                        "position", point.position
                    )[1],
                    incoming=preview_points.get(
                        point.point_id, {}
                    ).get("incoming", point.incoming),
                    outgoing=preview_points.get(
                        point.point_id, {}
                    ).get("outgoing", point.outgoing),
                    width=preview_points.get(
                        point.point_id, {}
                    ).get("width", point.width),
                    opacity=point.opacity,
                )
                for point in stroke.points
            ],
            render_revision=stroke.render_revision,
        )

    def _render_vector_drawing(
        self, painter: QPainter, drawing: VectorDrawingObject,
        local_visible: QRectF | None = None,
    ) -> None:
        painter.save()
        destination = (
            list(self._multi_transform_preview_quads[drawing.object_id])
            if drawing.object_id in self._multi_transform_preview_quads
            else
            list(self._transform_preview_quad)
            if (
                drawing.object_id == self.selected_object_id
                and self._transform_preview_quad is not None
            )
            else list(drawing.transform_quad)
            if drawing.transform_quad is not None else None
        )
        if destination is not None:
            painter.setTransform(
                self._drawing_object_transform(drawing, destination), True
            )
        painter.translate(drawing.x, drawing.y)
        drawing_visible = (
            self._drawing_local_visible_rect(
                drawing, local_visible, destination
            )
            if local_visible is not None else None
        )
        drawn_tiling_groups = set()
        for stroke_index in self._vector_stroke_indexes(
            drawing, drawing_visible
        ):
            stroke = drawing.strokes[stroke_index]
            if stroke.tiling_group:
                if stroke.tiling_group not in drawn_tiling_groups:
                    self._render_tiled_vector_group(painter, drawing, stroke.tiling_group)
                    drawn_tiling_groups.add(stroke.tiling_group)
                continue
            if (
                drawing_visible is not None
                and not QRectF(*stroke.derived_bounds()).intersects(
                    drawing_visible
                )
            ):
                continue
            if (
                self._vector_gesture_mode == "eraser"
                and drawing.object_id == self.selected_object_id
                and stroke.stroke_id in self._vector_eraser_preview
            ):
                for replacement in self._vector_eraser_preview[
                    stroke.stroke_id
                ]:
                    rendered = self._vector_stroke_image(
                        drawing, replacement,
                        cache_token=(
                            "eraser-preview",
                            self._vector_eraser_preview_versions.get(
                                stroke.stroke_id, 0
                            ),
                            replacement.stroke_id,
                        ),
                    )
                    if rendered is not None:
                        image, target = rendered
                        painter.drawImage(target, image)
                continue
            promoted = self._promoted_vector_preview
            requested_scale = self._requested_vector_render_scale()
            if (
                promoted is not None
                and promoted["drawing_id"] == drawing.object_id
                and promoted["stroke_id"] == stroke.stroke_id
                and promoted["render_revision"] == stroke.render_revision
                and requested_scale <= 1.25
            ):
                tile_size = promoted["tile_size"]
                for (tile_x, tile_y), image in promoted["tiles"].items():
                    painter.drawImage(
                        tile_x * tile_size, tile_y * tile_size, image
                    )
                continue
            render_stroke = (
                self._vector_stroke_with_selection_preview(stroke)
                if (
                    drawing.object_id == self.selected_object_id
                    and self._selection_vector_preview
                ) else stroke
            )
            cache_token = None
            if (
                render_stroke is not stroke
                and drawing.object_id == self.selected_object_id
            ):
                cache_token = (
                    "selection-preview", self._selection_vector_preview_revision
                )
            rendered = self._vector_stroke_image(
                drawing, render_stroke, cache_token=cache_token
            )
            if rendered is not None:
                image, target = rendered
                painter.drawImage(target, image)
        painter.restore()

    @staticmethod
    def _apply_ramp_stops(
        gradient: QLinearGradient | QRadialGradient,
        ramp: ColorGradientRamp, *, reverse: bool = False,
    ) -> None:
        ramp.validate()
        for stop in ramp.stops:
            position = 1.0 - stop.position if reverse else stop.position
            gradient.setColorAt(position, working_color(stop.color))

    @staticmethod
    def _gradient_path_signature(path: QPainterPath) -> tuple:
        return tuple(
            (
                round(path.elementAt(index).x, 3),
                round(path.elementAt(index).y, 3),
                path.elementAt(index).type.value,
            )
            for index in range(path.elementCount())
        )

    @staticmethod
    def _gradient_ramp_signature(ramp: ColorGradientRamp) -> tuple:
        ramp.validate()
        return tuple(
            (stop.stop_id, round(stop.position, 6), stop.color)
            for stop in ramp.stops
        )

    def _cache_gradient_value(
        self, cache: dict, key: tuple, value: object, limit: int = 32,
    ) -> object:
        cache[key] = value
        while len(cache) > limit:
            cache.pop(next(iter(cache)))
        return value

    @staticmethod
    def _gradient_grid(bounds: QRectF, maximum: int = 768) -> tuple[int, int]:
        width = max(2.0, bounds.width())
        height = max(2.0, bounds.height())
        ratio = width / height
        if ratio >= 1:
            return maximum, max(2, round(maximum / ratio))
        return max(2, round(maximum * ratio)), maximum

    def _gradient_grid_for_preview(
        self, bounds: QRectF,
    ) -> tuple[int, int]:
        # Geometry drags should remain interactive.  A final full-resolution
        # image is rebuilt when the gesture is released.
        return self._gradient_grid(
            bounds, 256 if self._gradient_preview_active else 768
        )

    @staticmethod
    def _gradient_coordinates(
        bounds: QRectF, width: int, height: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        xs = np.linspace(
            bounds.left() + bounds.width() / (2 * width),
            bounds.right() - bounds.width() / (2 * width),
            width,
            dtype=np.float32,
        )
        ys = np.linspace(
            bounds.top() + bounds.height() / (2 * height),
            bounds.bottom() - bounds.height() / (2 * height),
            height,
            dtype=np.float32,
        )
        return np.meshgrid(xs, ys)

    def _path_projection_arrays(
        self, path: QPainterPath, bounds: QRectF, width: int, height: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        signature = self._gradient_path_signature(path)
        key = (
            "projection", signature,
            round(bounds.x(), 3), round(bounds.y(), 3),
            round(bounds.width(), 3), round(bounds.height(), 3),
            width, height,
        )
        cached = self._gradient_geometry_cache.get(key)
        if cached is not None:
            return cached
        polygons = path.toSubpathPolygons()
        segment_pairs: list[
            tuple[tuple[float, float], tuple[float, float]]
        ] = []
        for polygon in polygons:
            points = [(point.x(), point.y()) for point in polygon]
            segment_pairs.extend(zip(points, points[1:]))
        if not segment_pairs:
            empty = np.zeros((height, width), dtype=np.float32)
            return empty, empty, empty
        starts = np.asarray(
            [pair[0] for pair in segment_pairs], dtype=np.float32
        )
        ends = np.asarray(
            [pair[1] for pair in segment_pairs], dtype=np.float32
        )
        vectors = ends - starts
        lengths = np.sqrt(np.sum(vectors * vectors, axis=1))
        usable = lengths > 1e-5
        starts, vectors, lengths = (
            starts[usable], vectors[usable], lengths[usable]
        )
        if not len(lengths):
            empty = np.zeros((height, width), dtype=np.float32)
            return empty, empty, empty
        cumulative = np.concatenate((
            np.zeros(1, dtype=np.float32), np.cumsum(lengths)
        ))
        total = max(float(cumulative[-1]), 1e-6)
        grid_x, grid_y = self._gradient_coordinates(
            bounds, width, height
        )
        best_distance = np.full(
            (height, width), np.inf, dtype=np.float32
        )
        best_amount = np.zeros((height, width), dtype=np.float32)
        best_signed = np.zeros((height, width), dtype=np.float32)
        for index, (start, vector, length) in enumerate(
            zip(starts, vectors, lengths)
        ):
            relative_x = grid_x - start[0]
            relative_y = grid_y - start[1]
            length_squared = float(length * length)
            along = np.clip(
                (relative_x * vector[0] + relative_y * vector[1])
                / length_squared,
                0.0, 1.0,
            )
            dx = relative_x - along * vector[0]
            dy = relative_y - along * vector[1]
            distance = dx * dx + dy * dy
            replace = distance < best_distance
            best_distance[replace] = distance[replace]
            amount = (cumulative[index] + along * length) / total
            best_amount[replace] = amount[replace]
            signed = (
                vector[0] * relative_y - vector[1] * relative_x
            ) / length
            best_signed[replace] = signed[replace]
        result = best_amount, best_signed, np.sqrt(best_distance)
        return self._cache_gradient_value(
            self._gradient_geometry_cache, key, result
        )

    def _path_coverage(
        self, path: QPainterPath, bounds: QRectF, width: int, height: int,
    ) -> np.ndarray:
        signature = self._gradient_path_signature(path)
        key = (
            "coverage", signature,
            round(bounds.x(), 3), round(bounds.y(), 3),
            round(bounds.width(), 3), round(bounds.height(), 3),
            width, height,
        )
        cached = self._gradient_geometry_cache.get(key)
        if cached is not None:
            return cached
        mask = QImage(width, height, QImage.Format.Format_Alpha8)
        mask.fill(0)
        mask_painter = QPainter(mask)
        transform = QTransform(
            width / max(bounds.width(), 1e-6), 0, 0,
            0, height / max(bounds.height(), 1e-6), 0,
            -bounds.left() * width / max(bounds.width(), 1e-6),
            -bounds.top() * height / max(bounds.height(), 1e-6), 1,
        )
        mask_painter.setTransform(transform)
        mask_painter.fillPath(path, Qt.GlobalColor.white)
        mask_painter.end()
        stride = mask.bytesPerLine()
        raw = np.frombuffer(mask.bits(), dtype=np.uint8).reshape(
            height, stride
        )
        coverage = raw[:, :width].copy() > 0
        return self._cache_gradient_value(
            self._gradient_geometry_cache, key, coverage
        )

    @staticmethod
    def _gradient_ramp_lut(
        ramp: ColorGradientRamp, size: int = 1024,
    ) -> np.ndarray:
        ramp.validate()
        positions = np.asarray(
            [stop.position for stop in ramp.stops], dtype=np.float32
        )
        contract = current_contract()
        colors = np.asarray([working_rgba(stop.color, contract) for stop in ramp.stops],
                            dtype=np.float32) if contract.floating else np.asarray([
            [
                QColor(stop.color).red(),
                QColor(stop.color).green(),
                QColor(stop.color).blue(),
                QColor(stop.color).alpha(),
            ]
            for stop in ramp.stops
        ], dtype=np.float32)
        values = np.linspace(0.0, 1.0, size, dtype=np.float32)
        right = np.searchsorted(positions, values, side="right")
        right = np.clip(right, 1, len(positions) - 1)
        left = right - 1
        spans = positions[right] - positions[left]
        amounts = np.divide(
            values - positions[left], spans,
            out=np.ones_like(values), where=spans > 1e-8,
        )
        result = (
            colors[left] * (1.0 - amounts[:, None])
            + colors[right] * amounts[:, None]
        )
        result[values <= positions[0]] = colors[0]
        result[values >= positions[-1]] = colors[-1]
        return result if contract.floating else np.clip(np.rint(result), 0, 255).astype(np.uint8)

    def _cached_gradient_ramp_lut(
        self, ramp: ColorGradientRamp, size: int = 1024,
    ) -> np.ndarray:
        # Pure ramp data can survive document/preview cache resets. Geometry
        # drags and separate gradients sharing a preset reuse the same sampler.
        ramp.validate()
        contract = current_contract()
        key = (size, tuple((stop.position, stop.color) for stop in ramp.stops),
               contract.signature, color_environment(contract))
        cache = getattr(self, "_gradient_ramp_cache", None)
        if cache is None:
            cache = self._gradient_ramp_cache = {}
        cached = cache.get(key)
        if cached is not None:
            return cached
        return self._cache_gradient_value(
            cache, key, self._gradient_ramp_lut(ramp, size),
        )

    def _gradient_image_from_scalar(
        self, scalar: np.ndarray, coverage: np.ndarray,
        ramp: ColorGradientRamp, bounds: QRectF, scalar_key: tuple,
    ) -> tuple[QImage, QRectF]:
        ramp_key = self._gradient_ramp_signature(ramp)
        contract = current_contract()
        key = ("colored", scalar_key, ramp_key, contract.signature,
               color_environment(contract))
        cached = self._gradient_render_cache.get(key)
        if cached is not None:
            return cached
        lut = self._cached_gradient_ramp_lut(ramp)
        indices = np.clip(
            np.rint(np.clip(scalar, 0.0, 1.0) * (len(lut) - 1)),
            0, len(lut) - 1,
        ).astype(np.int32)
        rgba = lut[indices].copy()
        if contract.floating:
            rgba[..., 3] *= coverage.astype(np.float32).clip(0., 1.)
            rgba[..., :3] *= rgba[..., 3:4]
            result = working_image(rgba, contract), QRectF(bounds)
            return self._cache_gradient_value(self._gradient_render_cache, key, result)
        if coverage.dtype == np.bool_:
            rgba[~coverage] = 0
        else:
            coverage_alpha = np.clip(
                coverage.astype(np.float32), 0.0, 1.0
            )
            rgba[..., 3] = np.clip(np.rint(
                rgba[..., 3].astype(np.float32) * coverage_alpha
            ), 0, 255).astype(np.uint8)
        alpha = rgba[..., 3:4].astype(np.uint16)
        rgba[..., :3] = (
            rgba[..., :3].astype(np.uint16) * alpha + 127
        ) // 255
        rgba = np.ascontiguousarray(rgba)
        height, width = scalar.shape
        image = QImage(
            rgba.data, width, height, rgba.strides[0],
            QImage.Format.Format_RGBA8888_Premultiplied,
        ).copy()
        result = image, QRectF(bounds)
        return self._cache_gradient_value(
            self._gradient_render_cache, key, result
        )

    def _shape_gradient_center(
        self, obj: GradientObject, path: QPainterPath,
    ) -> QPointF:
        field = obj.shape_field
        if not field.center_auto and field.manual_center is not None:
            return QPointF(*field.manual_center)
        bounds = path.boundingRect()
        center = bounds.center()
        if path.contains(center):
            return center
        # A stable interior fallback for concave and multi-contour shapes.
        for polygon in path.toSubpathPolygons():
            candidate = polygon.boundingRect().center()
            if path.contains(candidate):
                return candidate
            for point in polygon:
                toward = QPointF(
                    point.x() * 0.9 + bounds.center().x() * 0.1,
                    point.y() * 0.9 + bounds.center().y() * 0.1,
                )
                if path.contains(toward):
                    return toward
        return center

    def _shape_gradient_image(
        self, obj: ColorFillGradientObject, path: QPainterPath,
    ) -> tuple[QImage, QRectF] | None:
        bounds = path.boundingRect()
        if bounds.isEmpty():
            return None
        field = obj.shape_field
        if field.reverse_direction:
            bounds = bounds.adjusted(
                -field.distance, -field.distance,
                field.distance, field.distance,
            )
        width, height = self._gradient_grid_for_preview(bounds)
        path_signature = self._gradient_path_signature(path)
        scalar_key = (
            "shape", path_signature, width, height,
            field.reverse_direction,
            field.uniform, round(field.distance, 4),
            () if field.reverse_direction else (
                field.center_auto, field.manual_center,
            ),
        )
        cached = self._gradient_scalar_cache.get(scalar_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.ramp, cached_bounds, scalar_key
            )
        if field.reverse_direction:
            # Outward fields change their visible rectangle as Distance is
            # edited.  Build one canonical padded boundary field and sample
            # it for the current viewport so distance drags only redo the
            # scalar normalization and ramp lookup.
            canonical_bounds = path.boundingRect().adjusted(
                -1000.0, -1000.0, 1000.0, 1000.0
            )
            canonical_width, canonical_height = (
                self._gradient_grid_for_preview(canonical_bounds)
            )
            boundary_key = (
                "shape-boundary", path_signature,
                canonical_width, canonical_height,
            )
            boundary_data = self._gradient_geometry_cache.get(boundary_key)
            if boundary_data is None:
                _amount, _signed, canonical_boundary = (
                    self._path_projection_arrays(
                        path, canonical_bounds,
                        canonical_width, canonical_height,
                    )
                )
                canonical_inside = self._path_coverage(
                    path, canonical_bounds,
                    canonical_width, canonical_height,
                )
                boundary_data = (
                    canonical_boundary, canonical_inside, canonical_bounds,
                )
                self._cache_gradient_value(
                    self._gradient_geometry_cache,
                    boundary_key, boundary_data,
                )
            canonical_boundary, canonical_inside, canonical_bounds = boundary_data
            target_x, target_y = self._gradient_coordinates(
                bounds, width, height
            )
            x_index = np.clip(
                ((target_x - canonical_bounds.left())
                 / max(canonical_bounds.width(), 1e-6)
                 * (canonical_width - 1)).astype(np.int32),
                0, canonical_width - 1,
            )
            y_index = np.clip(
                ((target_y - canonical_bounds.top())
                 / max(canonical_bounds.height(), 1e-6)
                 * (canonical_height - 1)).astype(np.int32),
                0, canonical_height - 1,
            )
            boundary = canonical_boundary[y_index, x_index]
            inside = canonical_inside[y_index, x_index]
        else:
            _amount, _signed, boundary = self._path_projection_arrays(
                path, bounds, width, height
            )
            inside = self._path_coverage(path, bounds, width, height)
        if field.reverse_direction:
            scalar = np.clip(
                boundary / max(field.distance, 0.001), 0.0, 1.0
            )
            coverage = ~inside
        elif field.uniform:
            scalar = np.clip(
                boundary / max(field.distance, 0.001), 0.0, 1.0
            )
            coverage = inside
        else:
            grid_x, grid_y = self._gradient_coordinates(
                bounds, width, height
            )
            center = self._shape_gradient_center(obj, path)
            center_distance = np.hypot(
                grid_x - center.x(), grid_y - center.y()
            )
            denominator = boundary + center_distance
            scalar = np.divide(
                boundary, denominator,
                out=np.ones_like(boundary),
                where=denominator > 1e-6,
            )
            coverage = inside
        self._cache_gradient_value(
            self._gradient_scalar_cache, scalar_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.ramp, bounds, scalar_key
        )

    @staticmethod
    def _radial_boundary_path(field: RadialGradientField) -> QPainterPath:
        radius_y = field.radius_y if field.ellipse_enabled else field.radius_x
        path = QPainterPath()
        path.addEllipse(QRectF(
            -field.radius_x, -radius_y,
            field.radius_x * 2, radius_y * 2,
        ))
        transform = QTransform()
        transform.translate(field.origin_x, field.origin_y)
        transform.rotate(field.rotation)
        return transform.map(path)

    def _radial_uniform_image(
        self, obj: ColorFillGradientObject,
    ) -> tuple[QImage, QRectF] | None:
        field = obj.radial_field
        path = self._radial_boundary_path(field)
        bounds = path.boundingRect()
        if bounds.isEmpty():
            return None
        width, height = self._gradient_grid_for_preview(bounds)
        path_signature = self._gradient_path_signature(path)
        scalar_key = (
            "radial-uniform", path_signature, width, height,
            round(field.distance, 4),
        )
        cached = self._gradient_scalar_cache.get(scalar_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.ramp, cached_bounds, scalar_key
            )
        _amount, _signed, boundary = self._path_projection_arrays(
            path, bounds, width, height
        )
        coverage = self._path_coverage(path, bounds, width, height)
        scalar = np.clip(
            boundary / max(field.distance, 0.001), 0.0, 1.0
        )
        self._cache_gradient_value(
            self._gradient_scalar_cache, scalar_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.ramp, bounds, scalar_key
        )

    def _line_gradient_image(
        self, obj: ColorFillGradientObject, path: QPainterPath,
        bounds: QRectF,
    ) -> tuple[QImage, QRectF] | None:
        if bounds.isEmpty():
            return None
        width, height = self._gradient_grid_for_preview(bounds)
        field = obj.line_field
        circular = obj.gradient_shape == "circular"
        if circular:
            first, second = field.geometry.nodes[0], field.geometry.nodes[-1]
            signature = (first.x, first.y, second.x, second.y)
        else:
            signature = self._gradient_path_signature(path)
        scalar_key = (
            "circular" if circular else "line", signature, width, height,
            None if circular else field.direction_mode,
            field.reverse_direction,
            None if circular else round(field.perpendicular_distance, 4),
            round(bounds.x(), 3), round(bounds.y(), 3),
            round(bounds.width(), 3), round(bounds.height(), 3),
        )
        cached = self._gradient_scalar_cache.get(scalar_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.ramp, cached_bounds, scalar_key
            )
        if circular:
            grid_x, grid_y = self._gradient_coordinates(bounds, width, height)
            radius = max(math.hypot(second.x - first.x, second.y - first.y), 1e-6)
            scalar = np.clip(
                np.hypot(grid_x - first.x, grid_y - first.y) / radius,
                0.0, 1.0,
            )
        else:
            amount, signed, _distance = self._path_projection_arrays(
                path, bounds, width, height
            )
            if field.direction_mode == "perpendicular":
                direction = 1.0 if field.perpendicular_distance > 0 else -1.0
                scalar = np.clip(
                    signed * direction / abs(field.perpendicular_distance),
                    0.0, 1.0,
                )
            else:
                scalar = amount
        if field.reverse_direction:
            scalar = 1.0 - scalar
        coverage = np.ones_like(scalar, dtype=bool)
        self._cache_gradient_value(
            self._gradient_scalar_cache, scalar_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.ramp, bounds, scalar_key
        )

    def _radial_outward_image(
        self, obj: ColorFillGradientObject,
    ) -> tuple[QImage, QRectF] | None:
        field = obj.radial_field
        radius_y = field.radius_y if field.ellipse_enabled else field.radius_x
        extent_x = field.radius_x + field.distance
        extent_y = radius_y + field.distance
        radius = math.hypot(extent_x, extent_y)
        bounds = QRectF(
            field.origin_x - radius, field.origin_y - radius,
            radius * 2, radius * 2,
        )
        width, height = self._gradient_grid_for_preview(bounds)
        scalar_key = (
            "radial-out", width, height,
            round(field.origin_x, 4), round(field.origin_y, 4),
            round(field.radius_x, 4), round(radius_y, 4),
            round(field.rotation, 4), round(field.distance, 4),
        )
        cached = self._gradient_scalar_cache.get(scalar_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.ramp, cached_bounds, scalar_key
            )
        grid_x, grid_y = self._gradient_coordinates(bounds, width, height)
        angle = math.radians(-field.rotation)
        dx, dy = grid_x - field.origin_x, grid_y - field.origin_y
        local_x = dx * math.cos(angle) - dy * math.sin(angle)
        local_y = dx * math.sin(angle) + dy * math.cos(angle)
        normalized = np.sqrt(
            (local_x / field.radius_x) ** 2
            + (local_y / radius_y) ** 2
        )
        ray_length = np.hypot(local_x, local_y)
        boundary_length = np.divide(
            ray_length, normalized,
            out=np.zeros_like(ray_length), where=normalized > 1e-6,
        )
        outside_distance = np.maximum(0.0, ray_length - boundary_length)
        scalar = np.clip(
            outside_distance / max(field.distance, 0.001),
            0.0, 1.0,
        )
        coverage = normalized >= 1.0
        self._cache_gradient_value(
            self._gradient_scalar_cache, scalar_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.ramp, bounds, scalar_key
        )

    def _color_gradient_local_bounds(self, obj: ColorFillGradientObject) -> QRectF:
        """Full painted frame, independent of the gradient's control handles."""
        bounds = self.layer_effective_path(obj.parent_layer_id).boundingRect()
        if obj.field_type == "parent_shape" and obj.shape_field.reverse_direction:
            distance = obj.shape_field.distance
            return bounds.adjusted(-distance, -distance, distance, distance)
        if obj.field_type == "radial":
            field = obj.radial_field
            if field.reverse_direction:
                radius_y = field.radius_y if field.ellipse_enabled else field.radius_x
                radius = math.hypot(field.radius_x + field.distance, radius_y + field.distance)
                return QRectF(field.origin_x - radius, field.origin_y - radius, radius * 2, radius * 2)
            if field.uniform:
                return self._radial_boundary_path(field).boundingRect()
            if obj.ignore_parent_mask:
                # A padded radial field can fill beyond its direct parent.
                # Capture its containing page so adding an effect does not
                # replace that field with a rectangle around the handles.
                page = self.chapter.page_for_layer(obj.parent_layer_id)
                inverse, valid = self.layer_world_transform(obj.parent_layer_id).inverted()
                if valid:
                    bounds = bounds.united(inverse.mapRect(
                        self.layer_world_transform(page.layer_id).mapRect(
                            self.layer_effective_path(page.layer_id).boundingRect())))
        return bounds

    def _render_color_gradient(
        self, painter: QPainter, obj: ColorFillGradientObject,
        local_visible: QRectF,
    ) -> None:
        parent_path = self.layer_effective_path(obj.parent_layer_id)
        if parent_path.isEmpty():
            return
        if obj.field_type == "line":
            rendered = self._line_gradient_image(
                obj, QPainterPath() if obj.gradient_shape == "circular"
                else self.bound_path(obj.line_field.geometry),
                parent_path.boundingRect(),
            )
            if rendered is not None:
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver
                )
                painter.drawImage(rendered[1], rendered[0])
            return
        if obj.field_type == "radial":
            field = obj.radial_field
            if field.reverse_direction:
                rendered = self._radial_outward_image(obj)
                if rendered is not None:
                    painter.setCompositionMode(
                        QPainter.CompositionMode.CompositionMode_SourceOver
                    )
                    painter.drawImage(rendered[1], rendered[0])
                return
            if field.uniform:
                rendered = self._radial_uniform_image(obj)
                if rendered is not None:
                    painter.setCompositionMode(
                        QPainter.CompositionMode.CompositionMode_SourceOver
                    )
                    painter.drawImage(rendered[1], rendered[0])
                return
            center_x, center_y = field.center()
            angle = math.radians(-field.rotation)
            dx, dy = center_x - field.origin_x, center_y - field.origin_y
            radius_y = (
                field.radius_y
                if field.ellipse_enabled else field.radius_x
            )
            focal = QPointF(
                (dx * math.cos(angle) - dy * math.sin(angle))
                / field.radius_x,
                (dx * math.sin(angle) + dy * math.cos(angle))
                / radius_y,
            )
            gradient = QRadialGradient(QPointF(0, 0), 1.0, focal)
            gradient.setSpread(QRadialGradient.Spread.PadSpread)
            self._apply_ramp_stops(gradient, obj.ramp, reverse=True)
            brush = QBrush(gradient)
            transform = QTransform()
            transform.translate(field.origin_x, field.origin_y)
            transform.rotate(field.rotation)
            transform.scale(field.radius_x, radius_y)
            brush.setTransform(transform)
            painter.fillRect(local_visible, brush)
            return
        rendered = self._shape_gradient_image(obj, parent_path)
        if rendered is not None:
            image, target = rendered
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver
            )
            painter.drawImage(target, image)

    def _render_gradient(
        self, painter: QPainter, obj: GradientObject,
        local_visible: QRectF,
    ) -> None:
        if isinstance(obj, SpeedLinesGradientObject):
            # Legacy Speed Lines records are omitted during load.  Keep this
            # guard for in-memory documents created by older integrations so
            # the removed feature can never re-enter the renderer.
            return
        if isinstance(obj, ColorFillGradientObject):
            self._render_color_gradient(painter, obj, local_visible)

    def _render_object(
        self, painter: QPainter, obj: DocumentObject, parent_opacity: float,
        local_visible: QRectF,
    ) -> None:
        if not self._solo_content_visible("object", obj.object_id):
            return
        if self._render_exclude_text and isinstance(obj, TextObject):
            return
        if obj.object_id == self._render_excluded_object_id:
            return
        if obj.mask_only and not self._mask_only_render_visible("object", obj.object_id):
            return
        if (
            not self._rendering_compound_references
            and obj.geometry_reference == "compound"
            and self.chapter.closest_compound_ancestor(
                obj.parent_layer_id, include_self=True
            ) is not None
        ):
            return
        if not obj.visible:
            return
        if not self._render_bounds.object_visible(obj, local_visible):
            return
        if not self._render_bounds.painter_clip_visible(painter, local_visible):
            return
        if (
            isinstance(obj, GradientObject)
            and self._is_outward_gradient(obj)
            and not self._rendering_outward_gradient
        ):
            return
        if (obj.blend_mode != "normal" and not self._render_base_alpha
                and not self._render_cage_source and not self._rendering_mask_contributor
                and ("object", obj.object_id) not in self._render_modifier_sources
                and obj.object_id not in getattr(self, "_blend_capture_objects", ())):
            from comic_editor.ui.object_blending import render_blended_object
            render_blended_object(self, painter, obj, parent_opacity, local_visible)
            return
        if (
            (
                self._has_active_modifiers(obj.modifier_ids)
                or obj.opacity_mask is not None
                or isinstance(obj, RasterObject) and obj.modifier_source_frame is not None
            )
            and not self._render_base_alpha
            and ("object", obj.object_id) not in self._render_modifier_sources
        ):
            self._render_modified_object(
                painter, obj, parent_opacity, local_visible
            )
            return
        if self._render_base_alpha and ("object", obj.object_id) not in self._render_modifier_sources:
            if self._render_tiled_target(painter, obj, parent_opacity, self.layer_world_transform(obj.parent_layer_id).mapRect(local_visible)):
                return
        painter.save()
        opacity = (
            parent_opacity
            if obj.opacity_locked else parent_opacity * obj.opacity
        )
        if obj.object_id == self._live_underlay_object_id:
            opacity *= 1.0 - self._live_underlay_amount
        painter.setOpacity(opacity)
        self._render_object_content(painter, obj, local_visible)
        painter.restore()

    def _render_object_content(
        self, painter: QPainter, obj: DocumentObject,
        local_visible: QRectF,
    ) -> None:
        if not self._render_cage_source and self._cage_object_preview(painter, obj, 1., local_visible):
            return
        if isinstance(obj, VectorDrawingObject):
            self._render_vector_drawing(painter, obj, local_visible)
        elif isinstance(obj, GradientObject):
            self._render_gradient(painter, obj, local_visible)
        elif isinstance(obj, RasterObject):
            if self._render_tiling_raster_capture(painter, obj, local_visible):
                return
            self._render_raster_content(
                painter, obj, local_visible, use_transform_preview=True
            )
        elif isinstance(obj, ImageObject):
            self._render_image_object(painter, obj)
        elif isinstance(obj, TextObject):
            self._draw_text_object(painter, obj)

    @staticmethod
    def _rect_signature(rect: QRectF) -> tuple[float, float, float, float]:
        return (
            round(rect.x(), 5), round(rect.y(), 5),
            round(rect.width(), 5), round(rect.height(), 5),
        )

    @staticmethod
    def _modifier_mapping_signature(mapping: QTransform) -> tuple:
        return tuple(getattr(mapping, f"m{i}{j}")()
                     for i in range(1, 4) for j in range(1, 4))

    def _modifier_viewport_region(self, fallback_world: QRectF) -> QRectF:
        """Dirty rectangles clip painting, not an unchanged effect's cache window."""
        viewport = getattr(self, "_effect_viewport_world", None)
        if (viewport is not None and self._interactive_render
                and getattr(self, "_effect_preview_channel", "canvas") in {"canvas", "overflow"}
                and not self._render_modifier_sources and not self._render_base_alpha
                and self._rendering_mask_contributor <= 0 and not self._render_cage_source
                and not getattr(self, "_rendering_halftone_source", False)
                and getattr(self, "_tiling_capture_geometry", None) is None):
            return QRectF(viewport)
        return QRectF(fallback_world)

    def _modifier_parameter_signature(self, ids: Iterable[str]) -> tuple[str, ...]:
        from comic_editor.render.modifier_rendering import modifier_render_settings
        from comic_editor.ui.transform_modifier_preview import effective_preview_modifier
        result: list[str] = []
        capturing_colors = getattr(self, "_rendering_halftone_source", False)
        if capturing_colors:
            result.append("halftone-color-source")
        mask_ids: set[str] = set()
        for item in ids:
            modifier = self.chapter.modifiers.get(item)
            if modifier is None or modifier.muted:
                continue
            modifier = effective_preview_modifier(self, modifier)
            result.append(json.dumps(
                modifier_render_settings(modifier), sort_keys=True, separators=(",", ":"),
            ))
            if (isinstance(modifier, HalftoneModifier)
                    and modifier.color_mode == "target_layer" and not capturing_colors):
                from comic_editor.ui.halftone_source import source_signature
                result.append(repr(source_signature(self, modifier.target_layer_id)))
            mask_ids.update(
                binding.mask_id
                for binding in modifier.parameter_masks.values()
            )
        result.extend(
            repr(self._tone_mask_signature(mask_id))
            for mask_id in sorted(mask_ids)
        )
        return tuple(result)

    def _tone_mask_signature(
        self, mask_id: str, _stack: frozenset[str] = frozenset(),
        *, include_paint: bool = True,
    ) -> tuple:
        mask = self.chapter.masks.get(mask_id)
        if mask is None:
            return (mask_id, "missing")
        from comic_editor.ui.attached_translation import effective_preview_mask
        mask = effective_preview_mask(self, mask)
        if mask_id in _stack:
            return (mask_id, "cycle")
        stack = _stack | {mask_id}
        capturing_colors = getattr(self, "_rendering_halftone_source", False)

        def modifier_signature(modifier):
            from comic_editor.render.modifier_rendering import modifier_render_settings
            source = ()
            if (isinstance(modifier, HalftoneModifier)
                    and modifier.color_mode == "target_layer" and not capturing_colors):
                from comic_editor.ui.halftone_source import source_signature
                source = source_signature(self, modifier.target_layer_id)
            return (json.dumps(modifier_render_settings(modifier), sort_keys=True,
                               separators=(",", ":")), source)

        def entity_signature(kind: str, entity_id: str) -> tuple:
            entity = self.chapter.mask_contributor(kind, entity_id)
            if entity is None:
                return kind, entity_id, "missing"
            pixels: tuple = ()
            if isinstance(entity, RasterObject):
                pixels = self.tiles.object_signature(entity.object_id)
                preview = self._raster_selection_preview_state(entity)
                if preview is not None:
                    before_tiles, moving_tiles, source_path, _transform, copying = preview
                    # A mask contributor can be another selected raster whose
                    # committed tiles have not changed during the drag. Keep
                    # its preview in dependent modifier keys without recursing
                    # through the contributor's modifier/mask signatures.
                    pixels = (pixels, (
                        "raster-selection", self._gradient_path_signature(source_path),
                        tuple(self._selection_transform_start_quad),
                        tuple(self._selection_transform_quad), copying,
                        tuple(sorted((key, int(image.cacheKey()))
                                     for key, image in before_tiles.items())),
                        tuple(sorted((key, int(image.cacheKey()))
                                     for key, image in moving_tiles.items())),
                    ))
            elif isinstance(entity, ImageObject):
                pixels = self.images.pixel_signature(entity.object_id)
            if isinstance(entity, DocumentObject):
                pixels = (pixels, self._modifier_object_preview_signature(entity))
            children: tuple = ()
            if isinstance(entity, LayerNode):
                pixels = (pixels, self._modifier_layer_preview_signature(entity.layer_id))
                children = tuple(
                    entity_signature(child.kind, child.entity_id)
                    for child in entity.children
                )
                ancestor_layers = self.chapter.ancestor_layers(
                    entity.layer_id
                )[:-1]
            else:
                ancestor_layers = self.chapter.ancestor_layers(
                    entity.parent_layer_id
                )
            ancestors = tuple(
                (
                    json.dumps(self._modifier_entity_settings(layer), sort_keys=True),
                    self._modifier_layer_preview_signature(layer.layer_id),
                    tuple(
                        modifier_signature(modifier)
                        for modifier in self._active_modifier_instances(
                            layer.modifier_ids
                        )
                    ),
                )
                for layer in ancestor_layers
            )
            dependent_mask_ids: set[str] = set()
            if entity.opacity_mask is not None:
                dependent_mask_ids.add(entity.opacity_mask.mask_id)
            entity_modifiers = self._active_modifier_instances(
                entity.modifier_ids
            )
            for modifier in entity_modifiers:
                dependent_mask_ids.update(
                    binding.mask_id
                    for binding in modifier.parameter_masks.values()
                )
            return (
                kind, entity_id,
                json.dumps(self._modifier_entity_settings(entity), sort_keys=True),
                tuple(
                    modifier_signature(modifier)
                    for modifier in entity_modifiers
                ),
                pixels, children, ancestors,
                tuple(
                    self._tone_mask_signature(dependent, stack)
                    for dependent in sorted(dependent_mask_ids)
                ),
            )

        paint = (
            self.tiles.object_signature(mask_id)
            if include_paint else ()
        )
        return (
            json.dumps(copy.deepcopy(mask).to_dict(), sort_keys=True), paint,
            tuple(entity_signature(*item) for item in mask.contributors),
            capturing_colors,
        )

    def _ancestor_mask_path(
        self, kind: str, entity_id: str,
    ) -> QPainterPath | None:
        if kind == "object":
            entity = self.chapter.objects.get(entity_id)
            if entity is None:
                return QPainterPath()
            layer_id = entity.parent_layer_id
            layers = self.chapter.ancestor_layers(layer_id)
            direct_ignore = bool(entity.ignore_parent_mask)
        else:
            entity = self.chapter.layers.get(entity_id)
            if entity is None:
                return QPainterPath()
            layers = self.chapter.ancestor_layers(entity_id)
            layers = layers[:-1]
            direct_ignore = bool(entity.ignore_parent_mask)
        skipped: set[str] = set()
        if direct_ignore and layers:
            skipped.add(layers[-1].layer_id)
        chain_id = (
            entity.parent_id if kind == "layer" else layer_id
        )
        full_chain = (
            self.chapter.ancestor_layers(chain_id) if chain_id else []
        )
        for parent, child in zip(full_chain, full_chain[1:]):
            if child.ignore_parent_mask:
                skipped.add(parent.layer_id)
        result: QPainterPath | None = None
        for layer in layers:
            if not layer.visible:
                return QPainterPath()
            if layer.bound is None or layer.layer_id in skipped:
                continue
            path = self.layer_world_transform(layer.layer_id).map(
                self.layer_effective_path(layer.layer_id)
            )
            result = path if result is None else result.intersected(path)
        return result

    def _render_base_mask_contributor(
        self, painter: QPainter, kind: str, entity_id: str,
        visible_world: QRectF,
    ) -> None:
        entity = self.chapter.mask_contributor(kind, entity_id)
        if entity is None or not entity.visible:
            return
        ancestors = (
            self.chapter.ancestor_layers(entity.parent_layer_id)
            if kind == "object"
            else self.chapter.ancestor_layers(entity_id)[:-1]
        )
        if any(not layer.visible for layer in ancestors):
            return
        painter.save()
        clip = self._ancestor_mask_path(kind, entity_id)
        if clip is not None:
            if clip.isEmpty():
                painter.restore()
                return
            painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        self._rendering_mask_contributor += 1
        self._suppress_outline_for_mask = True
        try:
            if kind == "object":
                parent_transform = self.layer_world_transform(
                    entity.parent_layer_id
                )
                inverse, valid = parent_transform.inverted()
                painter.setTransform(parent_transform, True)
                ancestor_opacity = 1.0
                for ancestor in self.chapter.ancestor_layers(
                    entity.parent_layer_id
                ):
                    ancestor_opacity *= ancestor.opacity
                self._render_object(
                    painter, entity, ancestor_opacity,
                    inverse.mapRect(visible_world) if valid else visible_world,
                )
                if isinstance(entity, VectorDrawingObject):
                    self._render_modified_vector_pencil_preview(
                        painter, entity.parent_layer_id
                    )
                return
            layer = entity
            parent_transform = (
                self.layer_world_transform(layer.parent_id)
                if layer.parent_id else QTransform()
            )
            inverse, valid = parent_transform.inverted()
            painter.setTransform(parent_transform, True)
            ancestor_opacity = 1.0
            if layer.parent_id:
                for ancestor in self.chapter.ancestor_layers(layer.parent_id):
                    ancestor_opacity *= ancestor.opacity
            self._render_layer(
                painter, layer, ancestor_opacity,
                inverse.mapRect(visible_world) if valid else visible_world,
            )
        finally:
            self._rendering_mask_contributor -= 1
            self._suppress_outline_for_mask = False
            painter.restore()

    @staticmethod
    def _add_image_alpha_to_field(field: np.ndarray, image: QImage) -> None:
        """Accumulate a large painted mask without a second full float image."""
        if image.isNull():
            raise MemoryError("Could not allocate tone mask image")
        if current_contract().floating:
            # Coverage is a data channel. Read it at its source precision,
            # without an integer conversion or a display/color transform.
            np.add(field, premultiplied_pixels(image)[..., 3], out=field)
            return
        native_argb = image.format() in (
            QImage.Format.Format_ARGB32,
            QImage.Format.Format_ARGB32_Premultiplied,
            QImage.Format.Format_RGB32,
        )
        converted = image if native_argb else image.convertToFormat(QImage.Format.Format_RGBA8888)
        if converted.isNull():
            raise MemoryError("Could not convert tone mask image")
        alpha_byte = (3 if sys.byteorder == "little" else 0) if native_argb else 3
        pixels = np.frombuffer(converted.constBits(), dtype=np.uint8,
                               count=converted.sizeInBytes()).reshape(
                                   converted.height(), converted.bytesPerLine())
        alpha = pixels[:, alpha_byte:converted.width() * 4:4]
        rows = max(1, 262144 // max(converted.width(), 1))
        for top in range(0, converted.height(), rows):
            bottom = min(converted.height(), top + rows)
            values = alpha[top:bottom].astype(np.float32)
            values *= 1. / 255.
            np.add(field[top:bottom], values, out=field[top:bottom])

    def render_tone_mask_field(
        self, mask_id: str, width: int, height: int,
        world_to_image: QTransform, visible_world: QRectF,
        *, include_paint: bool = True,
    ) -> np.ndarray:
        mask = self.chapter.masks.get(mask_id) if self.chapter else None
        width, height = max(1, int(width)), max(1, int(height))
        if mask is None:
            return np.zeros((height, width), dtype=np.float32)
        from comic_editor.ui.attached_translation import effective_preview_mask
        mask = effective_preview_mask(self, mask)
        transform_signature = tuple(round(value, 6) for value in (
            world_to_image.m11(), world_to_image.m12(), world_to_image.m13(),
            world_to_image.m21(), world_to_image.m22(), world_to_image.m23(),
            world_to_image.m31(), world_to_image.m32(), world_to_image.m33(),
        ))
        full_key = (
            mask_id, width, height, transform_signature,
            self._rect_signature(visible_world),
            self._tone_mask_signature(mask_id), "complete",
        ) if include_paint else None
        if full_key is not None:
            cached_full = self._tone_mask_contributor_cache.pop(full_key, None)
            if cached_full is not None:
                self._tone_mask_contributor_cache[full_key] = cached_full
                return cached_full.copy()
        contributor_key = (
            mask_id, width, height, transform_signature,
            self._rect_signature(visible_world),
            self._tone_mask_signature(mask_id, include_paint=False),
        )
        cached = self._tone_mask_contributor_cache.pop(
            contributor_key, None
        )
        if cached is None:
            result = np.zeros((height, width), dtype=np.float32)
            if mask.gradient is not None:
                self._render_mask_gradient_field(
                    mask.gradient, width, height, world_to_image, output=result)
            for kind, entity_id in mask.contributors:
                image = QImage(
                    width, height, current_contract().image_format
                )
                image.fill(Qt.GlobalColor.transparent)
                painter = QPainter(image)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(
                    QPainter.RenderHint.SmoothPixmapTransform, True
                )
                painter.setTransform(world_to_image)
                self._render_base_mask_contributor(
                    painter, kind, entity_id, visible_world
                )
                painter.end()
                self._add_image_alpha_to_field(result, image)
            for limited in mask.limited_gradients:
                np.clip(result, 0.0, 1.0, out=result)
                self._render_mask_gradient_field(
                    limited.gradient, width, height, world_to_image, limited,
                    output=result, subtract=limited.operation == "subtract")
            np.clip(result, 0.0, 1.0, out=result)
            size = int(result.nbytes)
            if 0 < size <= min(self._tone_mask_contributor_cache_budget, 8 * 1024 * 1024):
                try:
                    snapshot = result.copy()
                except MemoryError:
                    snapshot = None
                if snapshot is not None:
                    self._tone_mask_contributor_cache[contributor_key] = snapshot
                    self._tone_mask_contributor_cache_bytes += size
                    while (self._tone_mask_contributor_cache
                           and self._tone_mask_contributor_cache_bytes
                           > self._tone_mask_contributor_cache_budget):
                        _old_key, old = self._tone_mask_contributor_cache.popitem(last=False)
                        self._tone_mask_contributor_cache_bytes -= int(old.nbytes)
        else:
            self._tone_mask_contributor_cache[contributor_key] = cached
            result = cached.copy()
        if include_paint:
            paint = QImage(
                width, height, QImage.Format.Format_ARGB32_Premultiplied
            )
            paint.fill(Qt.GlobalColor.transparent)
            painter = QPainter(paint)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setTransform(world_to_image)
            painter.translate(*mask.paint_offset)
            for (tile_x, tile_y), tile in self.tiles.iter_tiles(
                mask_id, visible_world.translated(-mask.paint_offset[0], -mask.paint_offset[1])
            ):
                painter.drawImage(
                    tile_x * self.tiles.tile_size,
                    tile_y * self.tiles.tile_size,
                    tile,
                )
            painter.end()
            if mask.paint_has_subtractions:
                result += self._signed_mask_paint(paint)
            else:
                self._add_image_alpha_to_field(result, paint)
        np.clip(result, 0.0, 1.0, out=result)
        if full_key is not None:
            size = int(result.nbytes)
            if 0 < size <= min(self._tone_mask_contributor_cache_budget, 8 * 1024 * 1024):
                try:
                    snapshot = result.copy()
                except MemoryError:
                    snapshot = None
                if snapshot is not None:
                    self._tone_mask_contributor_cache[full_key] = snapshot
                    self._tone_mask_contributor_cache_bytes += size
                    while (self._tone_mask_contributor_cache
                           and self._tone_mask_contributor_cache_bytes
                           > self._tone_mask_contributor_cache_budget):
                        _old_key, old = self._tone_mask_contributor_cache.popitem(last=False)
                        self._tone_mask_contributor_cache_bytes -= int(old.nbytes)
        return result

    @staticmethod
    def _world_to_image_transform(
        parent_transform: QTransform, bounds: QRectF,
        width: int, height: int,
    ) -> QTransform:
        world = parent_transform.map(QPolygonF([
            bounds.topLeft(), bounds.topRight(),
            bounds.bottomRight(), bounds.bottomLeft(),
        ]))
        destination = QPolygonF([
            QPointF(0, 0), QPointF(width, 0),
            QPointF(width, height), QPointF(0, height),
        ])
        result = QTransform.quadToQuad(world, destination)
        return result if isinstance(result, QTransform) else QTransform()

    def _modifier_mask_fields(
        self, modifiers, width: int, height: int,
        world_to_image: QTransform, visible_world: QRectF,
    ) -> dict[tuple[str, str], np.ndarray]:
        result: dict[tuple[str, str], np.ndarray] = {}
        rendered: dict[str, np.ndarray] = {}
        for modifier in modifiers:
            if modifier.muted:
                continue
            for attribute, binding in modifier.parameter_masks.items():
                field = rendered.get(binding.mask_id)
                if field is None:
                    field = self.render_tone_mask_field(
                        binding.mask_id, width, height,
                        world_to_image, visible_world,
                    )
                    rendered[binding.mask_id] = field
                result[(modifier.modifier_id, attribute)] = field
        return result

    @staticmethod
    def _modifier_maximum(modifier, attribute: str, fallback: float) -> float:
        binding = modifier.parameter_masks.get(attribute)
        return max(
            float(fallback),
            float(binding.black_value) if binding is not None else fallback,
            float(binding.white_value) if binding is not None else fallback,
        )

    def _mask_only_render_visible(self, kind: str, identifier: str) -> bool:
        if getattr(self, "_disk_cache_capture", False) and self._rendering_mask_contributor <= 0:
            return False
        return self._rendering_mask_contributor > 0 or bool(
            self._interactive_render
            and not getattr(self, "_rendering_halftone_source", False)
            and self.selected_kind == kind and self.selected_id == identifier
        )

    @staticmethod
    def _modifier_entity_settings(entity) -> dict:
        """Saved editor bookkeeping does not change captured artwork."""
        # Saved serializers normalize their nested geometry. Signature reads
        # must not validate compiler-shared immutable records in place.
        settings = copy.deepcopy(entity).to_dict()
        for name in ("name", "custom_name", "fill_reference", "grid_override", "last_raster_id", "blend_mode"):
            settings.pop(name, None)
        return settings

    def _modifier_object_preview_signature(self, obj: DocumentObject) -> tuple:
        """Only a live preview that changes this object's pixels is a dependency."""
        live = ()
        selection_preview = ()
        if isinstance(obj, RasterObject):
            raster_state = self.__dict__.get("_selection_raster_states", {}).get(obj.object_id)
            if raster_state is not None:
                before_tiles = raster_state["before_tiles"]
                source_path = raster_state["source_path"]
                overlay_tiles = raster_state.get("overlay_tiles")
            elif obj.object_id == self.selected_object_id:
                before_tiles = self._selection_before_tiles
                source_path = self._drawing_selection_path
                overlay_tiles = self._selection_overlay_tiles
            else:
                before_tiles = None
            if (before_tiles is not None and self._selection_transform_start_quad
                    and self._selection_transform_quad and not source_path.isEmpty()):
                selection_preview = (
                    self._gradient_path_signature(source_path),
                    tuple(self._selection_transform_start_quad),
                    tuple(self._selection_transform_quad),
                    tuple(sorted((key, int(image.cacheKey())) for key, image in before_tiles.items())),
                    None if overlay_tiles is None else tuple(sorted(
                        (key, int(image.cacheKey())) for key, image in overlay_tiles.items())),
                )
        if selection_preview:
            live = (*live, ("raster-selection", selection_preview))
        if obj.object_id == self.selected_object_id:
            if isinstance(obj, VectorDrawingObject):
                if self._vector_gesture_mode == "eraser" and self._vector_eraser_preview:
                    live = (*live, ("vector-eraser", self._vector_eraser_preview_revision))
                if self._vector_gesture_mode == "pencil" and self._vector_samples:
                    preview = self._vector_preview_tiles.object_signature(self._vector_preview_id)
                    if preview:
                        live = (*live, ("vector-pencil", preview))
            if self._transform_preview_quad:
                live = (*live, ("transform", tuple(self._transform_preview_quad)))
        if obj.object_id in self._multi_transform_preview_quads:
            live = (*live, ("object-transform", tuple(self._multi_transform_preview_quads[obj.object_id])))
        if self._cage_session is not None and ("object", obj.object_id) in self._cage_session["targets"]:
            live = (*live, repr(self._cage_session["grid"].grid_dict()))
        return live

    def _modifier_object_signature(self, obj: DocumentObject, *, pixel_signature=None) -> tuple:
        pixels: tuple = ()
        if isinstance(obj, RasterObject):
            pixels = self.tiles.object_signature(obj.object_id) if pixel_signature is None else pixel_signature
        elif isinstance(obj, ImageObject):
            pixels = self.images.pixel_signature(obj.object_id)
            if obj.placement_mode == "fit_parent":
                # Parent geometry can change the fitted destination within
                # unchanged aligned bounds. Preserve its complete native quad.
                pixels = (pixels, ("image-fit-quad", tuple(self._image_model_local_quad(obj))))
        elif isinstance(obj, ColorFillGradientObject):
            # Shape gradients and line-field coverage also depend on the
            # effective parent shape, including edits with unchanged bounds.
            pixels = ("gradient-parent", self._gradient_path_signature(
                self.layer_effective_path(obj.parent_layer_id)))
        elif isinstance(obj, TextObject) and obj.layout_mode == "strict":
            # Strict wrapping follows the parent, even when the text's own
            # stored frame and typography have not changed.
            pixels = ("text-layout", self._rect_signature(self._strict_text_rect(obj)))
        live = self._modifier_object_preview_signature(obj)
        if self.chapter.pixel_contract.floating:
            live = (*live, ('pixel-environment', self.chapter.pixel_contract.signature,
                            color_environment(self.chapter.pixel_contract)))
        if isinstance(obj, TextObject) and getattr(self, "_render_dependencies", None) is not None:
            pixels = (pixels, self._render_dependencies.font(obj))
        if getattr(self, "_tiling_capture_geometry", None) is not None:
            live = (*live, repr(self._tiling_capture_geometry))
        if obj.mask_only:
            live = (*live, ("mask-only-visible", self._mask_only_render_visible("object", obj.object_id)))
        return (
            json.dumps(
                self._modifier_entity_settings(obj), sort_keys=True, separators=(",", ":")
            ),
            self._modifier_parameter_signature(obj.modifier_ids),
            self._tone_mask_signature(obj.opacity_mask.mask_id)
            if obj.opacity_mask is not None else (),
            pixels, live,
        )

    def _modifier_layer_preview_signature(self, layer_id: str) -> tuple:
        if (self._transform_preview_quad and self._geometry_transform_target
                in {("layer_group", layer_id), ("layer", layer_id)}):
            return (self._geometry_transform_target[0],
                    tuple(self._transform_start_quad or ()), tuple(self._transform_preview_quad))
        return ()

    def _modifier_layer_signature(self, layer_id: str) -> tuple:
        layer = self.chapter.layers[layer_id]
        children = []
        for reference in layer.children:
            if reference.kind == "layer":
                children.append(self._modifier_layer_signature(
                    reference.entity_id
                ))
            else:
                obj = self.chapter.objects[reference.entity_id]
                # Own blend selection does not change an object's filtered
                # source, but does change its parent's assembled source.
                children.append((obj.blend_mode, self._modifier_object_signature(obj)))
        preview = self._modifier_layer_preview_signature(layer_id)
        if self.chapter.pixel_contract.floating:
            preview = (*preview, ('pixel-environment', self.chapter.pixel_contract.signature,
                                  color_environment(self.chapter.pixel_contract)))
        excluded = self.chapter.objects.get(self._render_excluded_object_id)
        if excluded is not None and any(parent.layer_id == layer_id
                for parent in self.chapter.ancestor_layers(excluded.parent_layer_id)):
            # Background captures must not become the cached visible subtree.
            preview = (*preview, ("excluded-object", self._render_excluded_object_id))
        if layer.mask_only:
            preview = (*preview, ("mask-only-visible", self._mask_only_render_visible("layer", layer_id)))
        if self._solo_signature():
            # Source caches consume this preview portion independently of the
            # owner's modifier parameters, including mirror and blur captures.
            preview = (*preview, ("solo", self._solo_signature()))
        if self._show_on_top_signature():
            preview = (*preview, ("show-on-top", self._show_on_top_signature()))
        return (
            json.dumps(
                self._modifier_entity_settings(layer), sort_keys=True, separators=(",", ":")
            ),
            self._modifier_parameter_signature(layer.modifier_ids),
            self._tone_mask_signature(layer.opacity_mask.mask_id)
            if layer.opacity_mask is not None else (),
            tuple(children), preview,
        )

    def _cached_modifier_output(self, key, scope, opacity_mask, bounds, mapping, world_bounds):
        """Recover completed pixels before allocating another source or mask field."""
        completed = self._effect_jobs.retained_get(("output", scope), key)
        if completed is not None:
            return completed[0]
        processed = self._effect_jobs.result(scope, ("interactive-stack", key))
        if processed is None:
            return None
        if opacity_mask is not None:
            width, height = processed.width(), processed.height()
            world_to_image = self._world_to_image_transform(mapping, bounds, width, height)
            processed = apply_opacity_mask(processed, self.render_tone_mask_field(
                opacity_mask.mask_id, width, height, world_to_image, world_bounds),
                opacity_mask.black_value, opacity_mask.white_value)
        self._modifier_cache_put(key, processed)
        return processed

    def _retain_modifier_output(self, key, scope, image):
        from comic_editor.render.effect_regions import exact_reference_sampling
        if ((self._interactive_render or exact_reference_sampling(self)) and not self._render_base_alpha
                and self._rendering_mask_contributor <= 0
                and getattr(self, "_effect_preview_channel", "canvas") != "navigator"):
            # Keep the latest displayed exact output separate from temporary
            # captures/drafts. This also promotes a synchronous export/cache hit
            # when it is next displayed, without making exports evict the view.
            self._effect_jobs.retained_put(("output", scope), key, image)

    def _modifier_cache_get(self, key: tuple) -> QImage | None:
        from comic_editor.ui.cache_dependencies import cache_get, cache_put
        image = self._modifier_render_cache.pop(key, None)
        if image is None:
            image = cache_get(self, "effect", key)
            if image is None:
                return None
            self._modifier_cache_put(key, image)
            return QImage(image)
        self._modifier_render_cache[key] = image
        cache_put(self, "effect", key, image)
        return QImage(image)

    def _modifier_cache_put(self, key: tuple, image: QImage) -> None:
        from comic_editor.ui.cache_dependencies import cache_put
        cache_put(self, "effect", key, image)
        size = int(image.sizeInBytes())
        if size <= 0:
            return
        previous = self._modifier_render_cache.pop(key, None)
        if previous is not None:
            self._modifier_render_cache_bytes -= int(previous.sizeInBytes())
        self._modifier_render_cache[key] = QImage(image)
        self._modifier_render_cache_bytes += size
        # Admit one oversized result exclusively so exact worker completions
        # remain consumable even when a document image exceeds the usual LRU.
        while (
            self._modifier_render_cache
            and self._modifier_render_cache_bytes
            > max(self._modifier_render_cache_budget, size)
        ):
            _old_key, old_image = self._modifier_render_cache.popitem(
                last=False
            )
            self._modifier_render_cache_bytes -= int(old_image.sizeInBytes())

    def _modifier_source_cache_get(self, key: tuple) -> QImage | None:
        from comic_editor.ui.cache_dependencies import cache_get, cache_put
        image = self._modifier_source_cache.pop(key, None)
        if image is None:
            image = cache_get(self, "source", key)
            if image is None:
                return None
            self._modifier_source_cache_put(key, image)
            return QImage(image)
        self._modifier_source_cache[key] = image
        cache_put(self, "source", key, image)
        return QImage(image)

    def _modifier_source_cache_put(self, key: tuple, image: QImage) -> None:
        from comic_editor.ui.cache_dependencies import cache_put
        cache_put(self, "source", key, image)
        size = int(image.sizeInBytes())
        if size <= 0:
            return
        previous = self._modifier_source_cache.pop(key, None)
        if previous is not None:
            self._modifier_source_cache_bytes -= int(previous.sizeInBytes())
        self._modifier_source_cache[key] = QImage(image)
        self._modifier_source_cache_bytes += size
        # A single large source keeps its identity across asynchronous stages.
        while (
            self._modifier_source_cache
            and self._modifier_source_cache_bytes
            > max(self._modifier_source_cache_budget, size)
        ):
            _old_key, old = self._modifier_source_cache.popitem(last=False)
            self._modifier_source_cache_bytes -= int(old.sizeInBytes())

    def _render_modified_object(
        self, painter: QPainter, obj: DocumentObject,
        parent_opacity: float, local_visible: QRectF,
    ) -> None:
        if self._render_tiled_target(painter, obj, parent_opacity, self.layer_world_transform(obj.parent_layer_id).mapRect(local_visible)):
            return
        if (isinstance(obj, VectorDrawingObject) and obj.blend_mode != "normal"
                and self._vector_gesture_mode == "pencil" and obj.object_id == self.selected_object_id):
            # This source path includes live ink in its bounds even before the
            # drawing has a committed stroke, and filters it before blending.
            self._render_mirror_target(painter, obj, parent_opacity, local_visible)
            return
        if self._cage_session is not None and ("object", obj.object_id) in self._cage_session["targets"]:
            self._render_mirror_target(painter, obj, parent_opacity, local_visible)
            return
        if isinstance(obj, RasterObject) and (obj.modifier_source_frame is not None or any(isinstance(m, (RadialBlurModifier, ArrayModifier, HalftoneModifier, PixelateModifier, DistortModifier, CurvesModifier, KuwaharaModifier, DitheringModifier, SharpnessModifier, SolidColorOverlayModifier)) for m in self._active_modifier_instances(obj.modifier_ids))):
            self._render_radial_raster(painter, obj, parent_opacity, local_visible)
            return
        if any(isinstance(m, (MirrorModifier, ArrayModifier, RadialBlurModifier, CageTransformModifier, StrokeModifier, HalftoneModifier, PixelateModifier, DistortModifier, KuwaharaModifier, DitheringModifier, SharpnessModifier, SolidColorOverlayModifier)) for m in self._active_modifier_instances(obj.modifier_ids)):
            self._render_mirror_target(painter, obj, parent_opacity, local_visible)
            return
        if (self._interactive_render
                and getattr(self, "_effect_preview_channel", "canvas") == "navigator"):
            self._render_mirror_target(painter, obj, parent_opacity, local_visible)
            return
        modifiers = self._active_modifier_instances(
            obj.modifier_ids,
            suppress_outline=getattr(
                self, "_suppress_outline_for_mask", False
            ),
        )
        world_bounds = self.object_world_rect(obj.object_id)
        from comic_editor.ui.attached_translation import preview_object_bounds
        world_bounds = preview_object_bounds(self, obj, world_bounds)
        if isinstance(obj, ColorFillGradientObject):
            world_bounds = self.layer_world_transform(obj.parent_layer_id).mapRect(
                self._color_gradient_local_bounds(obj))
        if isinstance(obj, RasterObject):
            preview_bounds = self._raster_selection_preview_world_bounds(obj)
            if preview_bounds is not None:
                world_bounds = (
                    preview_bounds
                    if world_bounds is None else world_bounds.united(
                        preview_bounds
                    )
                )
        if (
            world_bounds is None or world_bounds.isEmpty()
            or (not modifiers and obj.opacity_mask is None)
        ):
            self._render_modifier_sources.add(("object", obj.object_id))
            try:
                self._render_object(painter, obj, parent_opacity, local_visible)
            finally:
                self._render_modifier_sources.discard(("object", obj.object_id))
            return
        layer_transform = self.layer_world_transform(obj.parent_layer_id)
        layer_inverse, valid = layer_transform.inverted()
        if not valid:
            return
        local = layer_inverse.mapRect(world_bounds)
        expansion = sum(
            self._modifier_maximum(
                modifier, "strength", modifier.strength
            ) * 3.0
            if isinstance(modifier, BlurModifier)
            else 25.0 + outline_blur_padding(modifier)
            if isinstance(modifier, OutlineModifier)
            else 0.0
            for modifier in modifiers
        )
        local.adjust(-expansion, -expansion, expansion, expansion)
        bounds = QRectF(
            math.floor(local.left()), math.floor(local.top()),
            max(1, math.ceil(local.right()) - math.floor(local.left())),
            max(1, math.ceil(local.bottom()) - math.floor(local.top())),
        )
        if not bounds.intersects(local_visible):
            return
        from comic_editor.render.tile_effects import generic_target_output
        tiled = generic_target_output(self, obj, bounds, modifiers, layer_transform, local_visible)
        if tiled:
            processed, output_bounds = tiled
            opacity = parent_opacity if obj.opacity_locked else parent_opacity * obj.opacity
            if obj.object_id == self._live_underlay_object_id:
                opacity *= 1.0 - self._live_underlay_amount
            painter.save()
            painter.setOpacity(opacity)
            painter.drawImage(output_bounds.topLeft(), processed)
            painter.restore()
            return
        from comic_editor.ui.interactive_effects import outline_capture_bounds
        if isinstance(obj, RasterObject):
            viewport_world = self._modifier_viewport_region(QRectF())
            viewport_local = (layer_inverse.mapRect(viewport_world)
                              if not viewport_world.isEmpty() else local_visible)
            bounds = outline_capture_bounds(self, painter, bounds, viewport_local, modifiers)
        world_origin = layer_transform.map(bounds.topLeft())
        from comic_editor.render.effect_regions import region_scope
        request_scope = region_scope(self, self._effect_request_scope("object", obj.object_id), bounds)
        object_signature = self._modifier_object_signature(obj)
        from comic_editor.ui import translation_cache
        move_key = translation_cache.output_key(self, obj, bounds, layer_transform, modifiers)
        move_revision = getattr(self, "_effect_provisional_revision", 0)
        cache_key = (
            "object", obj.object_id,
            object_signature,
            self._rect_signature(bounds), world_origin.toTuple(),
            self._modifier_mapping_signature(layer_transform),
        )
        from comic_editor.ui.interactive_effects import render_interactive_stack
        processed = translation_cache.get(self, move_key)
        if processed is None:
            processed = self._modifier_cache_get(cache_key)
        provisional = False
        if processed is None:
            processed = self._cached_modifier_output(
                cache_key, request_scope,
                obj.opacity_mask, bounds, layer_transform, world_bounds)
        if processed is None:
            source_key = (
                "object-source", obj.object_id,
                object_signature[0], object_signature[3], object_signature[4],
                self._rect_signature(bounds), world_origin.toTuple(),
            )
            source_provisional = False
            image = self._modifier_source_cache_get(source_key)
            if image is None:
                revision = getattr(self, "_effect_provisional_revision", 0)
                image = QImage(
                    max(1, math.ceil(bounds.width())),
                    max(1, math.ceil(bounds.height())),
                    current_contract().image_format,
                )
                image.fill(Qt.GlobalColor.transparent)
                source = QPainter(image)
                source.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                source.translate(-bounds.left(), -bounds.top())
                self._render_modifier_sources.add(("object", obj.object_id))
                try:
                    self._render_object_content(source, obj, bounds)
                    if isinstance(obj, VectorDrawingObject):
                        self._render_modified_vector_pencil_preview(
                            source, obj.parent_layer_id
                        )
                finally:
                    self._render_modifier_sources.discard(
                        ("object", obj.object_id)
                    )
                    source.end()
                source_provisional = revision != getattr(self, "_effect_provisional_revision", 0)
                if not source_provisional:
                    self._modifier_source_cache_put(source_key, image)
            width, height = image.width(), image.height()
            world_to_image = self._world_to_image_transform(
                layer_transform, bounds, width, height
            )
            processed, provisional = render_interactive_stack(
                self, image, modifiers, world_origin.toTuple(),
                self._modifier_mask_fields(
                    modifiers, width, height,
                    world_to_image, world_bounds,
                ),
                cache_key=("interactive-stack", cache_key),
                scope=request_scope,
                upstream_provisional=source_provisional,
            )
            if obj.opacity_mask is not None:
                binding = obj.opacity_mask
                processed = apply_opacity_mask(
                    processed,
                    self.render_tone_mask_field(
                        binding.mask_id, width, height,
                        world_to_image, world_bounds,
                    ),
                    binding.black_value, binding.white_value,
                )
            if not provisional:
                self._modifier_cache_put(cache_key, processed)
        if not provisional:
            self._retain_modifier_output(cache_key, request_scope, processed)
            translation_cache.put(self, move_key, processed, move_revision)
        opacity = parent_opacity if self._render_base_alpha else (
            parent_opacity
            if obj.opacity_locked else parent_opacity * obj.opacity
        )
        if obj.object_id == self._live_underlay_object_id:
            opacity *= 1.0 - self._live_underlay_amount
        painter.save()
        painter.setOpacity(opacity)
        painter.drawImage(bounds.topLeft(), processed)
        painter.restore()

    def _mirror_source_text_local_bounds(self, obj):
        """Match current supported free-Text geometry to the source capture."""
        if not isinstance(obj, TextObject) or obj.layout_mode != 'free':
            return None
        preview = self._multi_transform_preview_quads.get(obj.object_id)
        if (preview is None and obj.object_id == self.selected_object_id
                and self._geometry_transform_target is None
                and self._transform_preview_quad is not None
                and self._transform_start_quad is not None):
            preview = self._transform_preview_quad
        if preview is None:
            return None
        return QPolygonF([QPointF(*point) for point in self._text_quad(obj)]).boundingRect()

    def _render_mirror_target(self, painter, target, parent_opacity, visible):
        layer = isinstance(target, LayerNode)
        kind, identifier = ("layer", target.layer_id) if layer else ("object", target.object_id)
        request_scope = self._effect_request_scope(kind, identifier)
        parent_id = target.parent_id if layer else target.parent_layer_id
        mapping = self.layer_world_transform(parent_id) if parent_id else QTransform()
        inverse, valid = mapping.inverted()
        if not valid:
            return
        live_text_bounds = (self._mirror_source_text_local_bounds
            if (self._geometry_transform_target is None
                and self._transform_preview_quad is not None and self._transform_start_quad is not None
                and isinstance(self.chapter.objects.get(self.selected_object_id), TextObject))
                or any(isinstance(self.chapter.objects.get(key), TextObject)
                       for key in self._multi_transform_preview_quads)
            else None)
        world = entity_visual_bounds(self.chapter, self.tiles, kind, identifier,
                                     layer_mapping=self.layer_world_transform,
                                     object_local_bounds=live_text_bounds)
        if not layer:
            from comic_editor.ui.attached_translation import preview_object_bounds
            world = preview_object_bounds(self, target, world, local_aabb=True)
        world = world.united(self._raster_selection_capture_bounds(kind, identifier))
        if layer:
            from comic_editor.ui.baking import visual_bounds
            for child in target.children:
                world = world.united(visual_bounds(self, child.kind, child.entity_id,
                    object_local_bounds=live_text_bounds))
            from comic_editor.ui.compound_strokes import scoped
            if scoped(self, target):
                world = world.united(self.layer_world_transform(identifier).mapRect(self.layer_effective_path(identifier).controlPointRect()))
        if self._cage_session is not None:
            affected = (kind, identifier) in self._cage_session["targets"] or layer and any(
                any(parent.layer_id == identifier for parent in self.chapter.ancestor_layers(self.chapter.objects[ref[1]].parent_layer_id))
                for ref in self._cage_session["targets"])
            if affected:
                from comic_editor.core.cage import deformed_bounds
                world = world.united(QRectF(*deformed_bounds(self._cage_session["grid"])))
        drawing = self._active_vector_drawing()
        preview_bounds = self._vector_preview_tiles.content_bounds(self._vector_preview_id)
        includes_preview = drawing is not None and (
            not layer and drawing.object_id == identifier or layer and
            any(item.layer_id == identifier for item in self.chapter.ancestor_layers(drawing.parent_layer_id))
        )
        if includes_preview and preview_bounds is not None:
            world = world.united(self._drawing_local_to_world_transform(drawing).mapRect(preview_bounds))
        # Gradient handles may describe a zero-height line or a small ellipse.
        # Sample the full painted field in local coordinates so its halftone
        # grid stays stable through parent transforms and viewport cropping.
        bounds = (aligned(self._color_gradient_local_bounds(target))
                  if isinstance(target, ColorFillGradientObject)
                  else aligned(inverse.mapRect(world)))
        modifiers = self._active_modifier_instances(target.modifier_ids, suppress_outline=self._suppress_outline_for_mask)
        if layer and scoped(self, target):
            modifiers = [modifier for modifier in modifiers if not isinstance(modifier, StrokeModifier)]
        from comic_editor.ui.thumbnail_effects import capture_scale, scaled_modifiers
        thumbnail_scale = capture_scale(self, bounds, modifiers)
        navigator = self._interactive_render and getattr(self, "_effect_preview_channel", "canvas") == "navigator"
        stage_mapping = mapping
        capture_bounds = bounds
        if thumbnail_scale < 1.:
            bounds = aligned(QTransform.fromScale(thumbnail_scale, thumbnail_scale).mapRect(bounds))
            capture_bounds = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale).mapRect(bounds)
            stage_mapping = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale) * mapping
            modifiers = scaled_modifiers(modifiers, thumbnail_scale)
        signature = self._modifier_layer_signature(identifier) if layer else self._modifier_object_signature(target)
        # Live transforms are scoped by the target/subtree signature. A global
        # preview quad would evict unrelated artwork's source and every later
        # effect stage whenever another layer is dragged.
        key = ("mirror-source", kind, identifier, signature[0], signature[3], signature[4], self._rect_signature(bounds), self._render_exclude_text)
        if layer:
            # Descendant masks and spatial effects are sampled in world space.
            # Parent transforms can change their pixels without changing this
            # subtree's local bounds or stored layer records.
            key = (*key, self._modifier_mapping_signature(mapping))
        if navigator:
            key = ("navigator-source", thumbnail_scale, key)
        if layer and scoped(self, target):
            key = (*key, self._modifier_parameter_signature([mid for mid in target.modifier_ids
                if isinstance(self.chapter.modifiers.get(mid), StrokeModifier)]))
        has_stroke = any(isinstance(modifier, StrokeModifier) for modifier in modifiers)
        shape_overlay = layer and any(isinstance(modifier, SolidColorOverlayModifier)
                                      and not modifier.apply_to_outline for modifier in modifiers)
        opacity = target.opacity if layer or not target.opacity_locked else 1.0
        direct_mirror = (not has_stroke and modifiers and isinstance(modifiers[-1], MirrorModifier)
                         and not modifiers[-1].parameter_masks and target.opacity_mask is None
                         and parent_opacity * opacity == 1)
        # A regular halftone can use the full gradient frame for its lattice
        # while evaluating only the requested output pixels. Other gradients
        # keep their complete stage capture through projective transforms.
        regional_gradient = (
            isinstance(target, ColorFillGradientObject)
            and self._effect_region_requests and self._projection_exact
            and len(modifiers) == 1
            and isinstance(modifiers[0], HalftoneModifier)
            and modifiers[0].grid_type in {"square", "hexagonal"}
            and modifiers[0].dot_style != "delaunay"
            and modifiers[0].color_mode != "target_layer"
            and not modifiers[0].parameter_masks
        )
        viewport_world = self._modifier_viewport_region(QRectF())
        required = (None if isinstance(target, ColorFillGradientObject)
                    and not regional_gradient
                    else inverse.mapRect(viewport_world) if not viewport_world.isEmpty()
                    else inverse.mapRect(visible) if layer else visible)
        if getattr(self, "_effect_preview_channel", "canvas") == "navigator":
            required = None
        if thumbnail_scale < 1. and required is not None:
            required = QTransform.fromScale(thumbnail_scale, thumbnail_scale).mapRect(required)
        from comic_editor.render.effect_pipeline import cached_stage_output
        from comic_editor.ui import translation_cache
        move_key = None
        move_revision = getattr(self, "_effect_provisional_revision", 0)
        if not has_stroke and not direct_mirror and not shape_overlay and thumbnail_scale == 1. and not navigator:
            from comic_editor.render.effect_pipeline import _stage_plan
            move_plan = _stage_plan(self, bounds, modifiers, stage_mapping, key, isinstance(target, RasterObject), required)
            move_key = translation_cache.output_key(self, target, bounds, stage_mapping, modifiers,
                geometry=move_plan.geometry, opacity=False)
        reused = translation_cache.get(self, move_key)
        completed = ((reused, QRectF(move_plan.targets[-1] if move_plan.targets else bounds))
            if reused is not None else cached_stage_output(self, bounds, modifiers, stage_mapping,
            nearest=isinstance(target, RasterObject), required=required,
            request_scope=request_scope, source_key=key)
            if not has_stroke and not direct_mirror and not shape_overlay else None)
        source_provisional = False
        def capture_region(region):
            revision = getattr(self, "_effect_provisional_revision", 0)
            image = empty_image(region)
            source = QPainter(image)
            source.setRenderHint(QPainter.Antialiasing, True)
            source.setTransform(QTransform.fromScale(thumbnail_scale, thumbnail_scale)
                                * QTransform.fromTranslate(-region.left(), -region.top()))
            capture = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale).mapRect(region)
            self._render_modifier_sources.add((kind, identifier))
            try:
                if layer:
                    if self.chapter.contributing_compound_ancestor(identifier) is not None:
                        self._render_compound_contributor(source, target, 1.0, mapping.mapRect(capture))
                    else:
                        self._render_layer(source, target, 1.0, mapping.mapRect(capture))
                else:
                    self._render_object_content(source, target, capture)
                if includes_preview and not layer:
                    self._render_modified_vector_pencil_preview(source, parent_id)
                elif includes_preview and not self._has_active_modifiers(drawing.modifier_ids):
                    modified_ancestors = [item.layer_id for item in self.chapter.ancestor_layers(drawing.parent_layer_id) if self._has_active_modifiers(item.modifier_ids)]
                    if modified_ancestors and modified_ancestors[-1] == identifier:
                        self._render_modified_vector_pencil_preview(source, parent_id or "")
            finally:
                self._render_modifier_sources.discard((kind, identifier))
                source.end()
            return image, revision != getattr(self, "_effect_provisional_revision", 0)
        if completed is None and not has_stroke and not direct_mirror and not shape_overlay and thumbnail_scale == 1. and not navigator:
            from comic_editor.render.tile_effects import tile_output
            from comic_editor.render.tile_graph import TileCacheMiss
            def exact_capture(region):
                image, provisional = capture_region(region)
                if provisional:
                    raise TileCacheMiss()
                return image
            completed = tile_output(self, None, bounds, modifiers, stage_mapping,
                nearest=isinstance(target, RasterObject), required=required,
                request_scope=request_scope, source_identity=key, capture=exact_capture)
        image = completed[0] if completed is not None else self._modifier_source_cache_get(key)
        if image is None:
            image, source_provisional = capture_region(bounds)
            if not source_provisional:
                self._modifier_source_cache_put(key, image)
        source_provisional |= navigator
        if direct_mirror and not shape_overlay:
            # Axis dragging reuses the source stages without allocating the gap.
            image, bounds = render_stages(self, image, bounds, modifiers[:-1], stage_mapping, nearest=isinstance(target, RasterObject), request_scope=request_scope, provisional=source_provisional, source_key=key)
            if thumbnail_scale < 1.:
                bounds = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale).mapRect(bounds)
            mirror = modifiers[-1]
            painter.save()
            painter.setRenderHint(QPainter.SmoothPixmapTransform,
                                  not isinstance(target, RasterObject)
                                  or getattr(self, "_effect_preview_channel", "canvas") == "navigator")
            painter.setRenderHint(QPainter.Antialiasing, not isinstance(target, RasterObject))
            painter.setOpacity(mirror.intensity / 100)
            painter.setTransform(mapping * reflection_transform(mirror) * inverse, True)
            painter.drawImage(bounds, image)
            painter.restore()
            painter.save()
            painter.setOpacity(1)
            painter.drawImage(bounds, image)
            painter.restore()
            return
        if shape_overlay:
            from comic_editor.ui.overlay_rendering import shape_overlay_stack
            image, bounds = shape_overlay_stack(self, target, image, bounds, modifiers, mapping,
                                                key, request_scope, provisional=source_provisional)
        elif has_stroke:
            from comic_editor.ui.stroke_rendering import render_stroke_stack
            image, bounds = render_stroke_stack(self, target, image, bounds, modifiers, mapping, key, request_scope,
                                                provisional=source_provisional)
        elif completed is not None:
            image, bounds = completed
        else:
            image, bounds = render_stages(self, image, bounds, modifiers, stage_mapping, nearest=isinstance(target, RasterObject), required=required, request_scope=request_scope, provisional=source_provisional, source_key=key)
        if thumbnail_scale < 1.:
            bounds = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale).mapRect(bounds)
        if not source_provisional:
            # A tile-graph crop can be smaller than the reference plan's full
            # spatial stage (e.g. Smudge). The alias restores that planned
            # placement, so admitting a crop would stretch it over the frame.
            # Ordinary tile caches already retain crops with their own bounds.
            if move_key is not None:
                move_bounds = move_plan.targets[-1] if move_plan.targets else capture_bounds
                if bounds == move_bounds:
                    translation_cache.put(self, move_key, image, move_revision)
        if target.opacity_mask is not None:
            from comic_editor.ui.viewport_masking import mask_output
            image, bounds = mask_output(
                self, image, bounds, mapping, target.opacity_mask,
                inverse.mapRect(visible) if layer else visible, painter,
                target=target, source_key=move_key)
            if image is None:
                return
        opacity = target.opacity if layer or not target.opacity_locked else 1.0
        painter.save()
        painter.setOpacity(parent_opacity * opacity)
        if isinstance(target, RasterObject):
            self._set_crisp_raster_transform(painter)
        elif has_stroke:
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(bounds, image)
        painter.restore()

    def _working_raster_tile(self, image):
        """Cache source conversion locally; original tile bytes stay untouched."""
        contract = current_contract()
        if not contract.floating or image.isNull():
            return image
        key = (image.cacheKey(), contract.signature, color_environment(contract))
        cache = self.__dict__.setdefault('_working_raster_cache', OrderedDict())
        cached = cache.pop(key, None)
        if cached is not None:
            cache[key] = cached
            return cached
        result = import_image(image, contract)
        size = int(result.sizeInBytes())
        budget = self._vector_cache_budget()
        if size <= budget:
            cache[key] = result
            resident = self.__dict__.get('_working_raster_cache_bytes', 0) + size
            while cache and resident > budget:
                _, previous = cache.popitem(last=False)
                resident -= int(previous.sizeInBytes())
            self._working_raster_cache_bytes = resident
        return result

    def _render_raster_content(
        self, painter: QPainter, obj: RasterObject, local_visible: QRectF,
        *, use_transform_preview: bool,
    ) -> None:
        preview = (
            use_transform_preview
            and obj.object_id == self.selected_object_id
            and self._transform_preview_quad is not None
        )
        destination = (
            list(self._multi_transform_preview_quads[obj.object_id])
            if obj.object_id in self._multi_transform_preview_quads
            else list(self._transform_preview_quad) if preview
            else list(obj.transform_quad) if obj.transform_quad is not None
            else None
        )
        if destination is not None:
            transform = self._drawing_object_transform(obj, destination)
            painter.save()
            self._set_crisp_raster_transform(painter)
            painter.setTransform(transform, True)
            object_visible = self._drawing_local_visible_rect(
                obj, local_visible, destination
            )
            painter.translate(obj.x, obj.y)
            if self._render_raster_selection_preview(
                painter, obj, object_visible
            ):
                painter.restore()
                return
            # The editing frame can be in destination space after a transform.
            # It is not a source-pixel boundary: only inverse-mapped visibility
            # may restrict the sparse tile query.
            for (tile_x, tile_y), image in self.tiles.iter_tiles(
                obj.object_id, object_visible
            ):
                painter.drawImage(
                    tile_x * obj.tile_size,
                    tile_y * obj.tile_size,
                    self._working_raster_tile(image),
                )
            painter.restore()
            return
        painter.save()
        self._set_crisp_raster_transform(painter)
        painter.translate(obj.x, obj.y)
        object_visible = local_visible.translated(-obj.x, -obj.y)
        if self._render_raster_selection_preview(
            painter, obj, object_visible
        ):
            painter.restore()
            return
        for (tile_x, tile_y), image in self.tiles.iter_tiles(
            obj.object_id, object_visible
        ):
            painter.drawImage(
                tile_x * obj.tile_size, tile_y * obj.tile_size,
                self._working_raster_tile(image)
            )
        painter.restore()

    def _raster_selection_preview_state(
        self, obj: RasterObject,
    ) -> tuple[
        dict[tuple[int, int], QImage], dict[tuple[int, int], QImage],
        QPainterPath, QTransform, bool,
    ] | None:
        raster_state = self.__dict__.get("_selection_raster_states", {}).get(obj.object_id)
        if raster_state is not None:
            before_tiles = raster_state["before_tiles"]
            source_path = raster_state["source_path"]
            overlay_tiles = raster_state.get("overlay_tiles")
        elif obj.object_id == self.selected_object_id:
            before_tiles = self._selection_before_tiles
            source_path = self._drawing_selection_path
            overlay_tiles = self._selection_overlay_tiles
        else:
            return None
        source_quad = self._selection_transform_start_quad
        destination_quad = self._selection_transform_quad
        if (
            before_tiles is None
            or not source_quad
            or not destination_quad
            or source_path.isEmpty()
        ):
            return None
        local_to_world = self._drawing_local_to_world_transform(obj)
        world_to_local, valid = local_to_world.inverted()
        if not valid:
            return None
        source_local = [
            world_to_local.map(QPointF(x, y)).toTuple()
            for x, y in source_quad
        ]
        destination_local = [
            world_to_local.map(QPointF(x, y)).toTuple()
            for x, y in destination_quad
        ]
        transform = self._quad_to_quad_transform(
            source_local, destination_local
        )
        if not transform.isInvertible():
            return None
        moving = before_tiles if overlay_tiles is None else overlay_tiles
        return (
            before_tiles, moving, QPainterPath(source_path),
            transform, overlay_tiles is not None,
        )

    @staticmethod
    def _tile_mapping_bounds(
        tiles: dict[tuple[int, int], QImage], tile_size: int,
    ) -> QRectF:
        bounds = QRectF()
        first = True
        for tile_x, tile_y in tiles:
            tile = QRectF(
                tile_x * tile_size, tile_y * tile_size,
                tile_size, tile_size,
            )
            bounds = tile if first else bounds.united(tile)
            first = False
        return bounds

    def _draw_tile_mapping(
        self, painter: QPainter, tiles: dict[tuple[int, int], QImage],
        tile_size: int, visible: QRectF | None,
    ) -> None:
        for (tile_x, tile_y), image in tiles.items():
            target = QRectF(
                tile_x * tile_size, tile_y * tile_size,
                tile_size, tile_size,
            )
            if visible is not None and not target.intersects(visible):
                continue
            painter.drawImage(target.topLeft(), self._working_raster_tile(image))

    def _set_crisp_raster_transform(self, painter: QPainter) -> None:
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform,
            self._interactive_render and getattr(self, "_effect_preview_channel", "canvas") == "navigator"
            and not self._render_modifier_sources
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _render_raster_selection_preview(
        self, painter: QPainter, obj: RasterObject,
        local_visible: QRectF | None,
    ) -> bool:
        state = self._raster_selection_preview_state(obj)
        if state is None:
            return False
        background_tiles, moving_tiles, source_path, transform, copied = state
        tile_bounds = self._tile_mapping_bounds(
            {**background_tiles, **moving_tiles}, obj.tile_size
        )
        if tile_bounds.isEmpty():
            return True

        painter.save()
        self._set_crisp_raster_transform(painter)
        if not copied:
            unselected = QPainterPath()
            unselected.addRect(tile_bounds)
            unselected = unselected.subtracted(source_path)
            painter.setClipPath(unselected, Qt.ClipOperation.IntersectClip)
        self._draw_tile_mapping(
            painter, background_tiles, obj.tile_size, local_visible
        )
        painter.restore()

        source_visible = None
        if local_visible is not None:
            inverse, valid = transform.inverted()
            if valid:
                source_visible = inverse.mapRect(local_visible)
        painter.save()
        self._set_crisp_raster_transform(painter)
        painter.setTransform(transform, True)
        painter.setClipPath(source_path, Qt.ClipOperation.IntersectClip)
        self._draw_tile_mapping(
            painter, moving_tiles, obj.tile_size, source_visible
        )
        painter.restore()
        return True

    def _raster_selection_preview_world_bounds(
        self, obj: RasterObject,
    ) -> QRectF | None:
        state = self._raster_selection_preview_state(obj)
        if state is None:
            return None
        _background, _moving, source_path, transform, _copied = state
        target_path = transform.map(source_path)
        return self._drawing_local_to_world_transform(obj).map(
            target_path
        ).boundingRect()

    def _raster_selection_capture_bounds(self, kind: str, entity_id: str) -> QRectF:
        """Include moved raster pixels when capturing an object or ancestor effect."""
        if not self._selection_transform_start_quad or not self._selection_transform_quad:
            return QRectF()
        states = self.__dict__.get("_selection_raster_states", {})
        identifiers = states if states else (self.selected_object_id,)
        result = QRectF()
        for object_id in identifiers:
            obj = self.chapter.objects.get(object_id)
            if not isinstance(obj, RasterObject):
                continue
            if kind == "object" and object_id != entity_id:
                continue
            ancestors = self.chapter.ancestor_layers(obj.parent_layer_id)
            if kind == "layer" and not any(layer.layer_id == entity_id for layer in ancestors):
                continue
            bounds = self._raster_selection_preview_world_bounds(obj)
            if bounds is None or bounds.isEmpty():
                continue
            if kind == "layer":
                # Child effects are already applied in this source. Expand in
                # each owner's coordinate space, stopping before this capture's
                # own modifiers (which run after its source has been painted).
                for target in (obj, *reversed(ancestors)):
                    if isinstance(target, LayerNode) and target.layer_id == entity_id:
                        break
                    parent_id = (target.parent_id if isinstance(target, LayerNode)
                                 else target.parent_layer_id)
                    mapping = self.layer_world_transform(parent_id) if parent_id else QTransform()
                    inverse, valid = mapping.inverted()
                    if valid:
                        bounds = mapping.mapRect(effect_bounds(
                            inverse.mapRect(bounds),
                            self._active_modifier_instances(target.modifier_ids), mapping))
            result = result.united(bounds)
        return result

    def _text_document(
        self, obj: TextObject, width: float, *, editing_overlay: bool = False,
    ) -> QTextDocument:
        document = QTextDocument()
        document.setUndoRedoEnabled(False)
        document.setDocumentMargin(0)
        font = QFont(obj.font_family)
        font.setPixelSize(max(1, round(obj.font_size)))
        font.setBold(obj.bold)
        font.setItalic(obj.italic)
        font.setLetterSpacing(QFont.AbsoluteSpacing, obj.kerning)
        document.setDefaultFont(font)
        document.setPlainText(obj.text)
        document.setTextWidth(max(1.0, width))
        cursor = QTextCursor(document)
        cursor.select(QTextCursor.Document)
        character = QTextCharFormat()
        character.setForeground(QColor(Qt.transparent) if editing_overlay else working_color(obj.text_color))
        cursor.mergeCharFormat(character)
        block = QTextBlockFormat()
        block.setAlignment({
            "left": Qt.AlignLeft,
            "center": Qt.AlignHCenter,
            "right": Qt.AlignRight,
        }[obj.horizontal_alignment])
        spacing = max(0.5, min(3.0, float(obj.line_spacing)))
        block.setLineHeight(
            spacing * 100.0,
            QTextBlockFormat.LineHeightTypes.ProportionalHeight.value,
        )
        cursor.mergeBlockFormat(block)
        if editing_overlay:
            # This scratch layout only supplies Qt selection backgrounds and
            # caret geometry. Keeping its actual glyphs transparent avoids
            # antialiasing leaks at the edges of layout selection rectangles.
            return document
        positions = text_indexes_to_qt_positions(
            obj.text, (run[key] for run in obj.color_runs for key in ("start", "end")),
        ) if obj.color_runs else {}
        for run in obj.color_runs:
            start, end = (max(0, min(len(obj.text), run[key])) for key in ("start", "end"))
            cursor.setPosition(positions[start])
            cursor.setPosition(
                positions[end], QTextCursor.KeepAnchor,
            )
            character.setForeground(working_color(run["color"]))
            cursor.mergeCharFormat(character)
        return document

    def _strict_text_rect(self, obj: TextObject) -> QRectF:
        parent = self.chapter.layers[obj.parent_layer_id]
        reference = parent
        if obj.geometry_reference == "compound":
            reference = (
                self.chapter.closest_compound_ancestor(
                    obj.parent_layer_id, include_self=True
                ) or parent
            )
        path = (
            self._layer_operand_path(reference)
            if (
                obj.geometry_reference == "direct"
                and reference.compound_enabled
            )
            else self.layer_effective_path(reference.layer_id)
        )
        bounds = path.boundingRect()
        reference_world = self.layer_world_transform(
            reference.layer_id
        ).map(path)
        parent_inverse, valid = self.layer_world_transform(
            parent.layer_id
        ).inverted()
        local_bounds = (
            parent_inverse.map(reference_world).boundingRect()
            if valid else bounds
        )
        left, top = local_bounds.left(), local_bounds.top()
        width, height = local_bounds.width(), local_bounds.height()
        margin = min(max(0.0, obj.margin), max(0.0, min(width, height) / 2 - 1))
        return QRectF(
            left + margin, top + margin,
            max(1.0, width - margin * 2), max(1.0, height - margin * 2),
        )

    @staticmethod
    def _rect_quad(rect: QRectF) -> list[tuple[float, float]]:
        return [
            (rect.left(), rect.top()), (rect.right(), rect.top()),
            (rect.right(), rect.bottom()), (rect.left(), rect.bottom()),
        ]

    def _text_quad(self, obj: TextObject) -> list[tuple[float, float]]:
        if obj.layout_mode == "free":
            if obj.object_id in self._multi_transform_preview_quads:
                return list(self._multi_transform_preview_quads[obj.object_id])
            if (obj.object_id == self.selected_object_id
                    and self._geometry_transform_target is None
                    and self._transform_preview_quad is not None
                    and self._transform_start_quad is not None):
                # Promoted artwork prevents the cached-background fast path.
                # Normal traversal and effect captures need the same live quad.
                return list(self._transform_preview_quad)
        if obj.transform_quad is None:
            obj.transform_quad = self._rect_quad(QRectF(obj.x, obj.y, obj.width, obj.height))
        return list(obj.transform_quad)

    @staticmethod
    def _quad_transform(
        source: QRectF, quad: list[tuple[float, float]],
    ) -> QTransform:
        if len(quad) == 4 and source.width() != 0 and source.height() != 0:
            a, b, c, d = quad
            # Affine quads should stay affine. Qt's general projective solver
            # introduces tiny perspective terms even for translations, which
            # defeats source caches and changes their sampling precision.
            roundoff_x = max(math.ulp(float(point[0])) for point in quad) * 8
            roundoff_y = max(math.ulp(float(point[1])) for point in quad) * 8
            if (abs((b[0]-a[0])-(c[0]-d[0])) <= roundoff_x
                    and abs((d[1]-a[1])-(c[1]-b[1])) <= roundoff_y
                    and abs((d[0]-a[0])-(c[0]-b[0])) <= roundoff_x
                    and abs((b[1]-a[1])-(c[1]-d[1])) <= roundoff_y):
                xx, xy = (b[0]-a[0])/source.width(), (b[1]-a[1])/source.width()
                yx, yy = (d[0]-a[0])/source.height(), (d[1]-a[1])/source.height()
                return QTransform(xx, xy, yx, yy,
                    a[0]-source.left()*xx-source.top()*yx,
                    a[1]-source.left()*xy-source.top()*yy)
        source_quad = QPolygonF([
            source.topLeft(), source.topRight(), source.bottomRight(), source.bottomLeft()
        ])
        destination = QPolygonF([QPointF(*point) for point in quad])
        transform = QTransform.quadToQuad(source_quad, destination)
        return transform if isinstance(transform, QTransform) else QTransform()

    @staticmethod
    def _quad_to_quad_transform(
        source: list[tuple[float, float]],
        destination: list[tuple[float, float]],
    ) -> QTransform:
        changes = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(source, destination)]
        if len(changes) == 4 and all(abs(dx - changes[0][0]) < 1e-9
                and abs(dy - changes[0][1]) < 1e-9 for dx, dy in changes):
            return QTransform.fromTranslate(*changes[0])
        source_polygon = QPolygonF([QPointF(*point) for point in source])
        destination_polygon = QPolygonF([
            QPointF(*point) for point in destination
        ])
        transform = QTransform.quadToQuad(
            source_polygon, destination_polygon
        )
        return transform if isinstance(transform, QTransform) else QTransform()

    def _text_vertical_offset(
        self, obj: TextObject, document: QTextDocument, available_height: float,
    ) -> float:
        content_height = min(available_height, document.size().height())
        if obj.vertical_alignment == "bottom":
            return max(0.0, available_height - content_height)
        if obj.vertical_alignment == "middle":
            return max(0.0, (available_height - content_height) / 2)
        return 0.0

    def _draw_text_object(self, painter: QPainter, obj: TextObject,
                          *, editing_overlay: bool = False) -> None:
        if obj.layout_mode == "strict":
            rect = self._strict_text_rect(obj)
            document = self._text_document(obj, rect.width(), editing_overlay=editing_overlay)
            offset = self._text_vertical_offset(obj, document, rect.height())
            painter.save()
            painter.setClipRect(rect, Qt.IntersectClip)
            painter.translate(rect.left(), rect.top() + offset)
            self._draw_text_document(painter, obj, document, editing_overlay=editing_overlay,
                                     show_editing=False)
            painter.restore()
            return
        source = QRectF(0, 0, max(1.0, obj.width), max(1.0, obj.height))
        document = self._text_document(obj, source.width(), editing_overlay=editing_overlay)
        offset = self._text_vertical_offset(obj, document, source.height())
        transform = self._quad_transform(source, self._text_quad(obj))
        painter.save()
        painter.setTransform(transform, True)
        painter.setClipRect(source, Qt.IntersectClip)
        painter.translate(0, offset)
        self._draw_text_document(painter, obj, document, editing_overlay=editing_overlay,
                                 show_editing=False)
        painter.restore()

    def _draw_text_document(
        self, painter: QPainter, obj: TextObject, document: QTextDocument,
        *, editing_overlay: bool = False, show_editing: bool = True,
    ) -> None:
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.Text, QColor(Qt.transparent) if editing_overlay else working_color(obj.text_color))
        selections = []
        # Decorations are UI, never source alpha for effects, masks or exports.
        editing = (self._text_editing and obj.object_id == self.selected_object_id
                   and (editing_overlay or show_editing
                        and not self._render_modifier_sources
                        and not self._render_base_alpha
                        and self._rendering_mask_contributor <= 0
                        and not self._object_has_effect_modifiers(obj.object_id)))
        if editing and self._text_cursor_position != self._text_selection_anchor:
            selection = QAbstractTextDocumentLayout.Selection()
            cursor = QTextCursor(document)
            cursor.setPosition(text_index_to_qt_position(obj.text, self._text_selection_anchor))
            cursor.setPosition(
                text_index_to_qt_position(obj.text, self._text_cursor_position),
                QTextCursor.KeepAnchor,
            )
            selection.cursor = cursor
            highlight = QColor("#F2A23A")
            highlight.setAlphaF(0.4)
            selection.format.setBackground(highlight)
            if editing_overlay:
                selection.format.setForeground(QColor(Qt.transparent))
            selections.append(selection)
        context.selections = selections
        document.documentLayout().draw(painter, context)
        if (
            editing and self.hasFocus() and self._text_caret_visible
            and self._text_cursor_position == self._text_selection_anchor
        ):
            caret = self._text_caret_rect(document, self._text_cursor_position)
            pen = QPen(QColor(text_color_at(obj, self._text_cursor_position)), 1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(caret.topLeft(), caret.bottomLeft())

    @staticmethod
    def _text_caret_rect(document: QTextDocument, position: int) -> QRectF:
        cursor = QTextCursor(document)
        cursor.setPosition(text_index_to_qt_position(document.toPlainText(), position))
        block = cursor.block()
        layout = block.layout()
        relative = cursor.position() - block.position()
        line = layout.lineForTextPosition(relative)
        if not line.isValid() and layout.lineCount():
            line = layout.lineAt(layout.lineCount() - 1)
        block_rect = document.documentLayout().blockBoundingRect(block)
        x = line.cursorToX(relative) if line.isValid() else 0.0
        if isinstance(x, tuple):
            x = x[0]
        y = block_rect.top() + (line.y() if line.isValid() else 0.0)
        height = line.height() if line.isValid() else QFontMetricsF(document.defaultFont()).height()
        return QRectF(float(x), y, 1.0, height)

    def _draw_predictive_ink(self, painter: QPainter) -> None:
        if not self.settings.predictive_ink or self._predictive is None:
            return
        from comic_editor.ui.native_artwork import paint_overlay
        if paint_overlay(self, painter, self.visible_document_rect(), lambda target, area: self._draw_predictive_ink(target)):
            return
        if not self.settings.predictive_ink or self._predictive is None:
            return
        if self.chapter is None or self.selected_kind != "object":
            return
        obj = self.chapter.objects.get(self.selected_id)
        if (not isinstance(obj, RasterObject)
                or not self._solo_content_visible("object", obj.object_id)):
            return
        if obj.blend_mode != "normal":
            return
        painter.save()
        for layer in self.chapter.ancestor_layers(obj.parent_layer_id):
            transform = self.layer_world_transform(layer.layer_id)
            if layer.bound is not None:
                painter.setClipPath(
                    transform.map(self.layer_effective_path(layer.layer_id)),
                    Qt.IntersectClip,
                )
        from comic_editor.ui.raster_feedback import paint_prediction
        paint_prediction(painter, self._predictive)
        painter.restore()

    def _draw_live_vector_gesture(self, painter: QPainter) -> None:
        if self._vector_gesture_mode not in {"pencil", "simplify"}:
            return
        from comic_editor.ui.native_artwork import paint_overlay
        if (self._vector_gesture_mode == "pencil" and self._vector_samples
                and paint_overlay(self, painter, self.visible_document_rect(), lambda target, area: self._draw_live_vector_gesture(target))):
            return
        drawing = self._active_vector_drawing()
        if drawing is not None and not self._solo_content_visible("object", drawing.object_id):
            return
        if (
            self._vector_gesture_mode == "simplify"
            and self._vector_sweep
            and self._selected_vector_drawing() is not None
        ):
            drawing = self._selected_vector_drawing()
            radius = 12.0 / max(self.scale, 0.05)
            painter.save()
            painter.setTransform(
                self.layer_world_transform(drawing.parent_layer_id), True
            )
            if drawing.transform_quad is not None:
                painter.setTransform(
                    self._drawing_object_transform(drawing), True
                )
            painter.translate(drawing.x, drawing.y)
            overlay = QColor(255, 139, 30, 72)
            sweep = self._vector_simplify_overlay or self._vector_sweep
            if len(sweep) == 1:
                painter.setPen(Qt.NoPen)
                painter.setBrush(overlay)
                painter.drawEllipse(
                    QPointF(*sweep[0].point), radius, radius
                )
            else:
                path = QPainterPath(QPointF(*sweep[0].point))
                for sample in sweep[1:]:
                    path.lineTo(QPointF(*sample.point))
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(
                    overlay, radius * 2, Qt.SolidLine,
                    Qt.RoundCap, Qt.RoundJoin,
                ))
                painter.drawPath(path)
            painter.restore()
            return
        if (
            self._vector_gesture_mode != "pencil"
            or not self._vector_samples
            or self._active_vector_drawing() is None
        ):
            return
        drawing = self._active_vector_drawing()
        if drawing.blend_mode != "normal" or self._has_active_modifiers(drawing.modifier_ids) or any(
            self._has_active_modifiers(layer.modifier_ids)
            for layer in self.chapter.ancestor_layers(
                drawing.parent_layer_id
            )
        ):
            # The isolated modifier source pass owns this live overlay.
            return
        painter.save()
        parent_transform = self.layer_world_transform(drawing.parent_layer_id)
        painter.setTransform(parent_transform, True)
        parent_inverse, valid = parent_transform.inverted()
        if drawing.transform_quad is not None:
            painter.setTransform(
                self._drawing_object_transform(drawing), True
            )
        painter.translate(drawing.x, drawing.y)
        local_visible = self._drawing_local_visible_rect(
            drawing,
            parent_inverse.mapRect(self.visible_document_rect())
            if valid else self.visible_document_rect(),
        )
        tile_size = self._vector_preview_tiles.tile_size
        for (tile_x, tile_y), image in self._vector_preview_tiles.iter_tiles(
            self._vector_preview_id, local_visible
        ):
            painter.drawImage(tile_x * tile_size, tile_y * tile_size, image)
        painter.restore()

    def _image_fit_quad(self, obj: ImageObject) -> list[tuple[float, float]]:
        parent = self.chapter.layers.get(obj.parent_layer_id)
        if parent is None or parent.bound is None:
            return self._rect_quad(QRectF(
                obj.x, obj.y, obj.pixel_width, obj.pixel_height
            ))
        bounds = self.layer_effective_path(parent.layer_id).boundingRect()
        width = max(1.0, bounds.width())
        height = max(1.0, bounds.height())
        aspect = obj.pixel_width / max(1.0, obj.pixel_height)
        if obj.fit_mode == "auto_width":
            target_width, target_height = width, width / aspect
        elif obj.fit_mode == "auto_height":
            target_width, target_height = height * aspect, height
        elif obj.fit_mode == "fit_inside":
            scale = min(width / obj.pixel_width, height / obj.pixel_height)
            target_width = obj.pixel_width * scale
            target_height = obj.pixel_height * scale
        else:
            target_width, target_height = width, height
        rect = QRectF(
            bounds.center().x() - target_width / 2,
            bounds.center().y() - target_height / 2,
            target_width, target_height,
        )
        return self._rect_quad(rect)

    def _image_model_local_quad(self, obj: ImageObject) -> list[tuple[float, float]]:
        if obj.placement_mode == "fit_parent":
            return self._image_fit_quad(obj)
        if obj.transform_quad is not None:
            return list(obj.transform_quad)
        return self._rect_quad(QRectF(
            obj.x, obj.y, obj.pixel_width, obj.pixel_height
        ))

    def _image_local_quad(self, obj: ImageObject) -> list[tuple[float, float]]:
        return self._image_model_local_quad(obj)

    def _render_image_object(self, painter: QPainter, obj: ImageObject) -> None:
        image = self.images.image(obj.object_id, contract=current_contract())
        if image.isNull() and (not obj.is_blender_linked
                or getattr(self, "_rendering_halftone_source", False)):
            return
        source = QRectF(0, 0, obj.pixel_width, obj.pixel_height)
        destination = self._image_local_quad(obj)
        if obj.object_id in self._multi_transform_preview_quads:
            destination = list(self._multi_transform_preview_quads[obj.object_id])
        elif (
            obj.object_id == self.selected_object_id
            and self._transform_preview_quad is not None
            and self._transform_start_quad is not None
        ):
            destination = list(self._transform_preview_quad)
        transform = self._quad_transform(source, destination)
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setTransform(transform, True)
        if image.isNull():
            painter.fillRect(source, working_color("#322f39"))
            step = max(8.0, min(source.width(), source.height()) / 12.0)
            painter.setPen(Qt.NoPen)
            painter.setBrush(working_color("#3e3a47"))
            rows = max(1, math.ceil(source.height() / step))
            columns = max(1, math.ceil(source.width() / step))
            for row in range(rows):
                for column in range(columns):
                    if (row + column) % 2:
                        painter.drawRect(QRectF(
                            column * step, row * step, step, step
                        ))
            painter.setPen(working_color("#d9d4e5"))
            painter.drawText(
                source, Qt.AlignCenter,
                "Waiting for Blender\nComic View",
            )
        else:
            painter.drawImage(source, image)
        painter.restore()

    def _render_modified_vector_pencil_preview(
        self, painter: QPainter, coordinate_parent_id: str,
    ) -> None:
        drawing = self._active_vector_drawing()
        if (
            drawing is None or self._vector_gesture_mode != "pencil"
            or not self._vector_samples
            or not self._solo_content_visible("object", drawing.object_id)
        ):
            return
        if (drawing.blend_mode != "normal"
                and ("object", drawing.object_id) not in self._render_modifier_sources):
            # The object blend capture already includes its live source ink.
            return
        if coordinate_parent_id != drawing.parent_layer_id:
            ancestors = {
                layer.layer_id for layer in self.chapter.ancestor_layers(
                    drawing.parent_layer_id
                )
            }
            if coordinate_parent_id not in ancestors:
                return
        coordinate_transform = (
            self.layer_world_transform(coordinate_parent_id)
            if coordinate_parent_id else QTransform()
        )
        coordinate_inverse, valid = coordinate_transform.inverted()
        if not valid:
            return
        relative = self.layer_world_transform(
            drawing.parent_layer_id
        ) * coordinate_inverse
        painter.save()
        painter.setTransform(relative, True)
        if drawing.transform_quad is not None:
            painter.setTransform(
                self._drawing_object_transform(drawing), True
            )
        painter.translate(drawing.x, drawing.y)
        tile_size = self._vector_preview_tiles.tile_size
        for (tile_x, tile_y), image in self._vector_preview_tiles.iter_tiles(
            self._vector_preview_id, None
        ):
            painter.drawImage(tile_x * tile_size, tile_y * tile_size, image)
        painter.restore()

    @staticmethod
    def _rotated_gradient_point(
        origin: tuple[float, float], vector: tuple[float, float],
        rotation: float,
    ) -> tuple[float, float]:
        angle = math.radians(rotation)
        cosine, sine = math.cos(angle), math.sin(angle)
        return (
            origin[0] + vector[0] * cosine - vector[1] * sine,
            origin[1] + vector[0] * sine + vector[1] * cosine,
        )

    @staticmethod
    def _focal_points(
        modifier: BlurModifier,
    ) -> tuple[QPointF, QPointF, QPointF]:
        center = QPointF(*modifier.focal_center)
        direction = QPointF(
            math.cos(modifier.focal_angle),
            math.sin(modifier.focal_angle),
        )
        end = center + direction * modifier.focal_radius
        ramp = center + direction * (
            modifier.focal_radius * modifier.focal_ramp
        )
        return center, ramp, end

    def object_world_rect(self, object_id: str) -> QRectF | None:
        quad = self.object_world_quad(object_id)
        if not quad:
            return None
        return QPolygonF([QPointF(*point) for point in quad]).boundingRect()

    def object_world_quad(self, object_id: str) -> list[tuple[float, float]] | None:
        obj = self.chapter.objects.get(object_id)
        if obj is None:
            return None
        layer_transform = self.layer_world_transform(obj.parent_layer_id)
        def world_quad(local_quad):
            return [
                layer_transform.map(QPointF(x, y)).toTuple()
                for x, y in local_quad
            ]
        if isinstance(obj, TextObject):
            local_quad = (
                self._rect_quad(self._strict_text_rect(obj))
                if obj.layout_mode == "strict" else self._text_quad(obj)
            )
            return world_quad(local_quad)
        if isinstance(obj, RasterObject):
            if obj.transform_quad is not None:
                return world_quad(obj.transform_quad)
            bounds = QRectF(*obj.interaction_rect)
            local = QRectF(
                obj.x + bounds.x(), obj.y + bounds.y(),
                bounds.width(), bounds.height(),
            )
            return world_quad(self._rect_quad(local))
        if isinstance(obj, VectorDrawingObject):
            if obj.transform_quad is not None:
                return world_quad(obj.transform_quad)
            left, top, width, height = obj.derived_bounds()
            local = QRectF(
                obj.x + left, obj.y + top, max(1.0, width), max(1.0, height)
            )
            return world_quad(self._rect_quad(local))
        if isinstance(obj, ImageObject):
            return world_quad(self._image_local_quad(obj))
        if isinstance(obj, GradientObject):
            if obj.field_type == "line":
                bounds = self.bound_path(
                    obj.line_field.geometry
                ).boundingRect()
            elif obj.field_type == "radial":
                field = obj.radial_field
                radius_y = (
                    field.radius_y
                    if field.ellipse_enabled else field.radius_x
                )
                corners = [
                    self._rotated_gradient_point(
                        (field.origin_x, field.origin_y), vector,
                        field.rotation,
                    )
                    for vector in (
                        (-field.radius_x, -radius_y),
                        (field.radius_x, -radius_y),
                        (field.radius_x, radius_y),
                        (-field.radius_x, radius_y),
                    )
                ]
                return world_quad(corners)
            else:
                bounds = self.layer_effective_path(
                    obj.parent_layer_id
                ).boundingRect()
            bounds = QRectF(
                bounds.left(), bounds.top(),
                max(1.0, bounds.width()), max(1.0, bounds.height()),
            )
            return world_quad(self._rect_quad(bounds))
        return world_quad(self._rect_quad(QRectF(obj.x, obj.y, 80, 80)))

    def _drawing_selection_object(
        self,
    ) -> RasterObject | VectorDrawingObject | LayerNode | None:
        if self.chapter is None:
            return None
        if len(self.selected_entities) > 1:
            targets = self._drawing_selection_raster_targets()
            return next((obj for obj in targets if obj.object_id == self.selected_object_id),
                        targets[0] if targets else None)
        if self.selected_kind == "layer":
            layer = self.chapter.layers.get(self.selected_id)
            if (
                layer is not None
                and layer.bound is not None
                and layer.bound.primitive == "custom"
            ):
                return layer
        candidate = self.chapter.objects.get(self.selected_object_id)
        return (
            candidate
            if isinstance(candidate, (RasterObject, VectorDrawingObject))
            else None
        )

    def _drawing_selection_transform(
        self, obj: RasterObject | VectorDrawingObject | LayerNode,
    ) -> QTransform:
        if isinstance(obj, LayerNode):
            return self.layer_world_transform(obj.layer_id)
        return self._drawing_local_to_world_transform(obj)

    def _selected_vector_drawing(self) -> VectorDrawingObject | None:
        if self.chapter is None or self.selected_kind != "object":
            return None
        candidate = self.chapter.objects.get(self.selected_id)
        return (
            candidate if isinstance(candidate, VectorDrawingObject) else None
        )

    def _object_transform_frame(
        self, obj: RasterObject | VectorDrawingObject | ImageObject,
    ) -> tuple[float, float, float, float]:
        if obj.transform_frame is not None:
            return tuple(obj.transform_frame)
        if isinstance(obj, RasterObject):
            rect = QRectF(*obj.interaction_rect).translated(obj.x, obj.y)
            return rect.x(), rect.y(), rect.width(), rect.height()
        if isinstance(obj, VectorDrawingObject):
            left, top, width, height = obj.derived_bounds()
            return (
                obj.x + left, obj.y + top,
                max(1.0, width), max(1.0, height),
            )
        return 0.0, 0.0, float(obj.pixel_width), float(obj.pixel_height)

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

    @staticmethod
    def _transform_distort_modifier(modifier, transform):
        old_frame = QRectF(*modifier.frame)
        new_frame = transform.mapRect(old_frame)
        for name in ("points", "source_points"):
            mapped = []
            for u, v in getattr(modifier, name):
                point = transform.map(QPointF(old_frame.x() + u * old_frame.width(), old_frame.y() + v * old_frame.height()))
                mapped.append(((point.x() - new_frame.x()) / max(1e-6, new_frame.width()),
                               (point.y() - new_frame.y()) / max(1e-6, new_frame.height())))
            setattr(modifier, name, mapped)
        center = QPointF(*modifier.center)
        modifier.radius = max(1., math.dist(transform.map(center).toTuple(),
                                           transform.map(center + QPointF(modifier.radius, 0)).toTuple()))
        modifier.center = transform.map(center).toTuple()
        modifier.frame = new_frame.getRect()
        modifier.validate()

    def _render_mask_gradient_field(
        self, obj: ColorFillGradientObject, width: int, height: int,
        world_to_image: QTransform,
        limited: LimitedMaskGradient | None = None,
        *, output: np.ndarray | None = None, subtract: bool = False,
    ) -> np.ndarray:
        """Sample at the requested resolution, including rotated export tiles."""
        inverse, valid = world_to_image.inverted()
        if not valid:
            return output if output is not None else np.zeros((height, width), dtype=np.float32)
        first, second = obj.line_field.geometry.nodes[0], obj.line_field.geometry.nodes[-1]
        dx, dy = second.x - first.x, second.y - first.y
        # Use the standard gradient ramp sampler to preserve coincident stops.
        lut = self._cached_gradient_ramp_lut(obj.ramp)
        circular = obj.gradient_shape == "circular"
        radius = max(math.hypot(dx, dy), 1e-6)
        result = output if output is not None else np.empty((height, width), dtype=np.float32)
        x = np.arange(width, dtype=np.float32)[None, :] + .5
        # Export tiles can be much larger than the editing preview. Keep the
        # coordinate and distance temporaries bounded, without downsampling.
        rows = max(1, 262144 // max(width, 1))
        for start in range(0, height, rows):
            end = min(height, start + rows)
            y = np.arange(start, end, dtype=np.float32)[:, None] + .5
            divisor = inverse.m13() * x + inverse.m23() * y + inverse.m33()
            with np.errstate(divide="ignore", invalid="ignore"):
                world_x = (inverse.m11() * x + inverse.m21() * y + inverse.dx()) / divisor
                world_y = (inverse.m12() * x + inverse.m22() * y + inverse.dy()) / divisor
                if circular:
                    distance = np.hypot(world_x - first.x, world_y - first.y)
                    position = distance / radius
                else:
                    position = ((world_x - first.x) * dx + (world_y - first.y) * dy) / max(dx * dx + dy * dy, 1e-12)
                scalar = np.clip(position, 0, 1)
            if obj.line_field.reverse_direction:
                scalar = 1 - scalar
            indices = np.rint(np.nan_to_num(scalar) * (len(lut) - 1)).astype(np.int32)
            coverage = (
                (world_x >= 0) & (world_x < self.chapter.width)
                & (world_y >= 0) & (world_y < self.chapter.height)
            )
            alpha = lut[indices, 3] / np.float32(255)
            if limited is not None:
                edge = (limited.end_bound-position)*radius
                if not circular or limited.start_bound > 0:
                    edge = np.minimum(edge, (position-limited.start_bound)*radius)
                if not circular:
                    cross = np.abs(-(world_x-first.x)*dy + (world_y-first.y)*dx)/radius
                    edge = np.minimum(edge, limited.half_width-cross)
                falloff = np.clip(edge/limited.feather, 0, 1) if limited.feather > 0 else (edge >= 0)
                alpha = alpha * falloff
            values = np.where(coverage, alpha, 0)
            if output is None:
                result[start:end] = values
            elif subtract:
                np.subtract(result[start:end], values, out=result[start:end])
            else:
                np.add(result[start:end], values, out=result[start:end])
        return result

    @staticmethod
    def _signed_mask_paint(image: QImage) -> np.ndarray:
        # White contributes coverage; opaque black removes coverage, including
        # that supplied by linked gradients and hierarchy contributors.
        rgba = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
        rows = np.frombuffer(rgba.constBits(), dtype=np.uint8).reshape(
            rgba.height(), rgba.bytesPerLine()
        )
        pixels = rows[:, :rgba.width() * 4].reshape(
            rgba.height(), rgba.width(), 4
        )
        return (2 * pixels[..., 0].astype(np.float32) - pixels[..., 3]) / 255

    def _drawing_selection_raster_targets(self) -> list[RasterObject]:
        if self.chapter is None:
            return []
        targets = {}
        visited = set()

        def add_object(obj):
            if not isinstance(obj, RasterObject) or not obj.visible or obj.mask_only:
                return
            if not self._solo_content_visible("object", obj.object_id):
                return
            parent_id = obj.parent_layer_id
            while parent_id:
                parent = self.chapter.layers.get(parent_id)
                if parent is None or not parent.visible or parent.mask_only:
                    return
                parent_id = parent.parent_id
            if self._drawing_local_to_world_transform(obj).isInvertible():
                targets[obj.object_id] = obj

        def visit(layer_id):
            if layer_id in visited:
                return
            visited.add(layer_id)
            layer = self.chapter.layers.get(layer_id)
            if layer is None or not layer.visible or layer.mask_only:
                return
            for child in layer.children:
                if child.kind == "layer":
                    visit(child.entity_id)
                else:
                    add_object(self.chapter.objects.get(child.entity_id))

        for kind, identifier in self.selected_entities:
            if kind == "layer":
                visit(identifier)
            elif kind == "object":
                obj = self.chapter.objects.get(identifier)
                if not isinstance(obj, RasterObject):
                    return []
                add_object(obj)
            else:
                return []
        return list(targets.values())

    def _show_on_top_plan(self):
        chapter = self.chapter
        if chapter is None:
            return TopPlan(frozenset(), frozenset(), frozenset())
        entries = {
            (kind, identifier)
            for kind, records in (("layer", chapter.layers), ("object", chapter.objects))
            for identifier, item in records.items() if item.show_on_top
        }
        content, branches = set(entries), set()
        pending = [identifier for kind, identifier in entries if kind == "layer"]
        visited = set()
        while pending:
            identifier = pending.pop()
            if identifier in visited:
                continue
            visited.add(identifier)
            layer = chapter.layers.get(identifier)
            if layer is None:
                continue
            for child in layer.children:
                content.add((child.kind, child.entity_id))
                if child.kind == "layer":
                    pending.append(child.entity_id)
        for kind, identifier in content:
            if kind == "layer":
                branches.add(identifier)
        for kind, identifier in entries:
            item = (chapter.layers if kind == "layer" else chapter.objects)[identifier]
            parent = item.parent_id if kind == "layer" else item.parent_layer_id
            while parent and parent not in branches:
                branches.add(parent)
                ancestor = chapter.layers.get(parent)
                parent = ancestor.parent_id if ancestor else None
        return TopPlan(frozenset(entries), frozenset(content), frozenset(branches))

    def _show_on_top_bypassed(self):
        return bool(getattr(self, "_rendering_mask_contributor", 0)
                    or getattr(self, "_rendering_halftone_source", False))

    def _is_show_on_top(self, kind, identifier):
        plan = getattr(self, "_active_top_plan", None)
        if plan is not None:
            return (kind, identifier) in plan.content
        chapter = self.chapter
        item = (chapter.layers if kind == "layer" else chapter.objects).get(identifier) if chapter else None
        while item is not None:
            if item.show_on_top:
                return True
            parent = item.parent_id if kind == "layer" else item.parent_layer_id
            kind = "layer"
            item = chapter.layers.get(parent)
        return False

    def _show_on_top_content_visible(self, kind, identifier):
        phase = getattr(self, "_show_on_top_phase", None)
        if phase is None or self._show_on_top_bypassed():
            return True
        return self._is_show_on_top(kind, identifier) == (phase == "top")

    def _show_on_top_branch_visible(self, layer_id):
        phase = getattr(self, "_show_on_top_phase", None)
        if phase is None or self._show_on_top_bypassed():
            return True
        plan = self._active_top_plan
        return layer_id in plan.branches if phase == "top" else ("layer", layer_id) not in plan.content

    def _show_on_top_solo_branch(self, layer_id):
        plan = getattr(self, "_active_top_plan", None) or self._show_on_top_plan()
        return layer_id in plan.branches

    def _show_on_top_standalone_contributor(self, layer):
        # A promoted operand has its own visible artwork above the ordinary
        # compound. Contributors inside a promoted compound remain combined.
        return bool(getattr(self, "_show_on_top_phase", None) == "top"
                    and not self._show_on_top_bypassed()
                    and self._is_show_on_top("layer", layer.layer_id)
                    and not self._is_show_on_top("layer", layer.parent_id))

    def _show_on_top_signature(self):
        phase = getattr(self, "_show_on_top_phase", None)
        if phase is None or self._show_on_top_bypassed():
            return ()
        return phase, tuple(sorted(self._active_top_plan.entries))

    def _effect_request_scope(self, kind, identifier):
        scope = (kind, identifier, getattr(self, "_effect_preview_channel", "canvas"))
        phase = getattr(self, "_show_on_top_phase", None)
        # The worker retains only the latest request per scope. Distinct
        # hierarchy passes must not cancel each other's source/effect work.
        return (*scope, "show-on-top", phase) if phase is not None and not self._show_on_top_bypassed() else scope

    @contextmanager
    def _show_on_top_scene(self):
        previous = (getattr(self, "_active_top_plan", None),
                    getattr(self, "_show_on_top_phase", None))
        plan = self._show_on_top_plan()
        self._active_top_plan = plan
        self._show_on_top_phase = None
        try:
            yield ("base", "top") if plan.entries else (None,)
        finally:
            self._active_top_plan, self._show_on_top_phase = previous

    def _render_scene_layers(self, painter, visible_world, *, underlay=False,
                             page_contents_only=False, live_ink=False, only_phase=None):
        with self._show_on_top_scene() as phases:
            for phase in phases:
                if only_phase is not None and phase != only_phase:
                    continue
                self._show_on_top_phase = phase
                for page_id in reversed(self.chapter.root_page_ids):
                    page = self.chapter.layers[page_id]
                    if not page_contents_only:
                        self._render_layer(painter, page, 1., visible_world)
                        continue
                    if not page.visible or page.opacity <= 0 or not self._solo_branch_visible(page_id):
                        continue
                    # The overflow view bypasses only the page's outer clip.
                    painter.save()
                    transform = self.layer_world_transform(page_id)
                    painter.setTransform(transform, True)
                    inverse, valid = transform.inverted()
                    local_visible = inverse.mapRect(visible_world) if valid else visible_world
                    for child in reversed(page.children):
                        if child.kind == "layer":
                            self._render_layer(painter, self.chapter.layers[child.entity_id],
                                               page.opacity, visible_world)
                        else:
                            self._render_object(painter, self.chapter.objects[child.entity_id],
                                                page.opacity, local_visible)
                    painter.restore()
                if underlay:
                    self._render_selected_drawing_underlay(painter, visible_world)
                if live_ink:
                    # These transient strokes normally paint above the scene.
                    # The phase filter places ordinary ink below promoted art.
                    self._draw_predictive_ink(painter)
                    self._draw_live_vector_gesture(painter)

    def _solo_filter_suspended(self):
        return (getattr(self, "_solo_suspended", False)
                or getattr(self, "_rendering_halftone_source", False)
                or getattr(self, "_rendering_mask_contributor", 0))

    def _mask_wand_isolation_active(self):
        return (getattr(self, "_mask_wand_sample_entities", None) is not None
                and not self._solo_filter_suspended())

    def _solo_filter_entries(self):
        if self._solo_filter_suspended():
            return set()
        samples = getattr(self, "_mask_wand_sample_entities", None)
        if samples is not None:
            return samples
        return self.solo_entities

    def _solo_signature(self) -> tuple:
        entries = tuple(sorted(self._solo_filter_entries()))
        # Ordinary solo retains promoted artwork. A wand sample must exclude
        # unrelated promoted objects too, so its subtree pixels need a distinct
        # source identity even when the selected entries match ordinary solo.
        return ("mask-wand", entries) if self._mask_wand_isolation_active() else entries

    def _solo_content_visible(self, kind: str, identifier: str) -> bool:
        if not self._show_on_top_content_visible(kind, identifier):
            return False
        entries = self._solo_filter_entries()
        sampling = self._mask_wand_isolation_active()
        if ((not entries and not sampling) or (kind, identifier) in entries
                or (not sampling and self._is_show_on_top(kind, identifier))):
            return True
        entity = (self.chapter.layers.get(identifier) if kind == "layer"
                  else self.chapter.objects.get(identifier))
        if entity is None:
            return False
        parent = entity.parent_id if kind == "layer" else entity.parent_layer_id
        while parent:
            if ("layer", parent) in entries:
                return True
            ancestor = self.chapter.layers.get(parent)
            parent = ancestor.parent_id if ancestor else None
        return False

    def _solo_branch_visible(self, layer_id: str) -> bool:
        if not self._show_on_top_branch_visible(layer_id):
            return False
        if self._solo_content_visible("layer", layer_id):
            return True
        if not self._mask_wand_isolation_active() and self._show_on_top_solo_branch(layer_id):
            return True
        for kind, identifier in self._solo_filter_entries():
            entity = (self.chapter.layers.get(identifier) if kind == "layer"
                      else self.chapter.objects.get(identifier))
            if entity is None:
                continue
            parent = entity.parent_id if kind == "layer" else entity.parent_layer_id
            while parent:
                if parent == layer_id:
                    return True
                ancestor = self.chapter.layers.get(parent)
                parent = ancestor.parent_id if ancestor else None
        return False

    def _render_radial_raster(self, painter, obj, parent_opacity, visible):
        """Use the same tile-coordinate stages as Apply, then transform once.

        Blurring an already-warped Raster and subsequently baking in its local
        tiles would resample in two different orders and change its appearance.
        """
        from PySide6.QtGui import QPainter
        from comic_editor.render.effect_pipeline import aligned, empty_image, render_stages
        from comic_editor.render.modifier_rendering import apply_opacity_mask

        destination = self._multi_transform_preview_quads.get(obj.object_id)
        if destination is None and obj.object_id == self.selected_object_id:
            destination = self._transform_preview_quad
        placement = QTransform.fromTranslate(obj.x, obj.y) * self._drawing_object_transform(obj, destination)
        mapping = placement * self.layer_world_transform(obj.parent_layer_id)
        inverse, valid = placement.inverted()
        if not valid:
            return
        bounds = self.tiles.content_bounds(obj.object_id) or QRectF(*obj.interaction_rect)
        if obj.modifier_source_frame is not None:
            bounds = bounds.united(QRectF(*obj.modifier_source_frame))
        selection = self._raster_selection_preview_state(obj)
        if selection is not None:
            bounds = bounds.united(selection[3].map(selection[2]).boundingRect())
        bounds = aligned(bounds)
        modifiers = self._active_modifier_instances(obj.modifier_ids, suppress_outline=self._suppress_outline_for_mask)
        from comic_editor.ui.thumbnail_effects import capture_scale, scaled_modifiers
        thumbnail_scale = capture_scale(self, bounds, modifiers)
        navigator = self._interactive_render and getattr(self, "_effect_preview_channel", "canvas") == "navigator"
        stage_mapping = mapping
        capture_bounds = bounds
        if thumbnail_scale < 1.:
            bounds = aligned(QTransform.fromScale(thumbnail_scale, thumbnail_scale).mapRect(bounds))
            capture_bounds = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale).mapRect(bounds)
            stage_mapping = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale) * mapping
            modifiers = scaled_modifiers(modifiers, thumbnail_scale)
        signature = self._modifier_object_signature(obj)
        key = ("radial-raster-source", obj.object_id, signature[0], signature[3], signature[4], self._rect_signature(bounds))
        if navigator:
            key = ("navigator-source", thumbnail_scale, key)
        def capture_region(region):
            image = empty_image(region)
            source = QPainter(image)
            source.setTransform(QTransform.fromScale(thumbnail_scale, thumbnail_scale)
                                * QTransform.fromTranslate(-region.left(), -region.top()))
            capture = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale).mapRect(region)
            try:
                if not self._render_raster_selection_preview(source, obj, capture):
                    for (x, y), tile in self.tiles.iter_tiles(obj.object_id, capture):
                        source.drawImage(x*obj.tile_size, y*obj.tile_size,
                                         self._working_raster_tile(tile))
            finally:
                source.end()
            return image
        required = inverse.mapRect(visible)
        viewport_world = self._modifier_viewport_region(QRectF())
        if not viewport_world.isEmpty():
            # A stroke dirty rectangle limits painting, while an unchanged
            # raster effect retains one window for the full canvas viewport.
            # Map world to tile space once: rotating a bounding rectangle to
            # the parent and back would produce different capture extents.
            world_inverse, world_valid = mapping.inverted()
            if world_valid:
                required = world_inverse.mapRect(viewport_world)
        if getattr(self, "_effect_preview_channel", "canvas") == "navigator":
            required = None
        elif thumbnail_scale < 1.:
            required = QTransform.fromScale(thumbnail_scale, thumbnail_scale).mapRect(required)
        request_scope = ("object", obj.object_id, getattr(self, "_effect_preview_channel", "canvas"))
        from comic_editor.ui import translation_cache
        from comic_editor.render.effect_pipeline import _stage_plan
        move_key = None
        move_revision = getattr(self, "_effect_provisional_revision", 0)
        if thumbnail_scale == 1. and not navigator:
            move_plan = _stage_plan(self, bounds, modifiers, stage_mapping, key, True, required)
            move_key = translation_cache.output_key(self, obj, bounds, stage_mapping, modifiers,
                tile_space=True, geometry=move_plan.geometry)
        reused = translation_cache.get(self, move_key)
        from comic_editor.render.tile_effects import tile_output
        tiled = ((reused, QRectF(move_plan.targets[-1] if move_plan.targets else bounds))
            if reused is not None else tile_output(self, None, bounds, modifiers, stage_mapping, nearest=True,
            required=required, source_identity=key, request_scope=request_scope, capture=capture_region)
            if thumbnail_scale == 1. and not navigator else None)
        if tiled is not None:
            image, bounds = tiled
        else:
            image = self._modifier_source_cache_get(key)
            if image is None:
                image = capture_region(bounds)
                self._modifier_source_cache_put(key, image)
            image, bounds = render_stages(self, image, bounds, modifiers, stage_mapping, nearest=True,
                required=required, source_key=key, provisional=navigator, request_scope=request_scope)
        if thumbnail_scale < 1.:
            bounds = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale).mapRect(bounds)
        if obj.opacity_mask is not None and reused is None:
            binding = obj.opacity_mask
            field = self.render_tone_mask_field(binding.mask_id, image.width(), image.height(),
                self._world_to_image_transform(mapping, bounds, image.width(), image.height()), mapping.mapRect(bounds))
            image = apply_opacity_mask(image, field, binding.black_value, binding.white_value)
        if not navigator:
            translation_cache.put(self, move_key, image, move_revision)
        opacity = parent_opacity if self._render_base_alpha or obj.opacity_locked else parent_opacity*obj.opacity
        if obj.object_id == self._live_underlay_object_id:
            opacity *= 1-self._live_underlay_amount
        painter.save()
        self._set_crisp_raster_transform(painter)
        painter.setTransform(placement, True)
        painter.setOpacity(opacity)
        painter.drawImage(bounds, image)
        painter.restore()

    def _text_container_bounds(self, layer):
        rect = QRectF()
        for ref in layer.children:
            obj = self.chapter.objects.get(ref.entity_id)
            if isinstance(obj, TextObject):
                quad = self._text_quad(obj)
                box = QPolygonF([QPointF(*p) for p in quad]).boundingRect()
                rect = box if rect.isNull() else rect.united(box)
        return rect if not rect.isEmpty() else QRectF(0, 0, 1, 1)

    def _own_tiling(self, target):
        if not self.chapter or target is None:
            return None
        modifier = next((m for mid in target.modifier_ids
                     if isinstance(m := self.chapter.modifiers.get(mid), TilingModifier)
                     and not m.muted and (m.intensity > 0 or any(
                         max(b.black_value, b.white_value) > 0 for b in m.parameter_masks.values()))), None)
        if modifier is not None:
            from comic_editor.ui.transform_modifier_preview import effective_preview_modifier
            modifier = effective_preview_modifier(self, modifier)
        return modifier

    def _tiling_boundary(self, target):
        layer = target if isinstance(target, LayerNode) else self.chapter.layers.get(target.parent_layer_id)
        while layer is not None and layer.bound is None:
            layer = self.chapter.layers.get(layer.parent_id)
        if layer is not None:
            return self.layer_world_transform(layer.layer_id).map(self.layer_effective_path(layer.layer_id))
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.addRect(QRectF(0, 0, self.chapter.width, self.chapter.height))
        return path

    def _render_tiled_vector_group(self, painter, drawing, group):
        originals = [s for s in drawing.strokes if s.tiling_group == group]
        strokes = []
        for stroke in originals:
            if self._vector_gesture_mode == "eraser" and drawing.object_id == self.selected_object_id:
                strokes.extend(self._vector_eraser_preview.get(stroke.stroke_id, [stroke]))
            else:
                strokes.append(self._vector_stroke_with_selection_preview(stroke))
        bounds = QRectF()
        rendered = []
        for stroke in strokes:
            item = self._vector_stroke_image(drawing, stroke, cache_token=repr(copy.deepcopy(stroke).to_dict()))
            if item is not None:
                rendered.append(item)
                bounds = item[1] if bounds.isEmpty() else bounds.united(item[1])
        if not rendered:
            return
        bounds = aligned(bounds)
        image = empty_image(bounds)
        source = QPainter(image)
        source.setCompositionMode(QPainter.CompositionMode_Lighten)
        for item, rect in rendered:
            source.drawImage(rect.translated(-bounds.topLeft()), item)
        source.end()
        painter.drawImage(bounds.topLeft(), image)

    def _tiling_children(self, painter, layer, visible):
        mapping = self.layer_world_transform(layer.layer_id)
        local_visible = mapping.inverted()[0].mapRect(visible)
        painter.save()
        painter.setTransform(mapping, True)
        old_reference = self._rendering_compound_references
        self._rendering_compound_references = True
        try:
            self._render_outward_gradient_children(painter, layer, 1., local_visible)
            for ref in reversed(layer.children):
                if ref.kind == "object":
                    self._render_object(painter, self.chapter.objects[ref.entity_id], 1., local_visible)
                else:
                    child = self.chapter.layers[ref.entity_id]
                    if layer.compound_enabled and child.compound_operation != "ignore":
                        self._render_compound_contributor(painter, child, 1., visible)
                    else:
                        self._render_layer(painter, child, 1., visible)
            drawing = self._active_vector_drawing()
            if drawing is not None and not self._has_active_modifiers(drawing.modifier_ids):
                if any(p.layer_id == layer.layer_id for p in self.chapter.ancestor_layers(drawing.parent_layer_id)):
                    # The preview routine consumes the drawing parent's coordinates.
                    painter.save()
                    painter.setTransform(self.layer_world_transform(drawing.parent_layer_id) * mapping.inverted()[0], True)
                    self._render_modified_vector_pencil_preview(painter, drawing.parent_layer_id)
                    painter.restore()
        finally:
            self._rendering_compound_references = old_reference
            painter.restore()

    def _tiling_target_signature(self, target):
        """World-space source dependencies, independent of allocated QImages."""
        layer = isinstance(target, LayerNode)
        kind, identifier = ("layer", target.layer_id) if layer else ("object", target.object_id)
        signature = self._modifier_layer_signature(identifier) if layer else self._modifier_object_signature(target)
        mapping = self.layer_world_transform(identifier if layer else target.parent_layer_id)
        excluded_id = self._render_excluded_object_id
        excluded = self.chapter.objects.get(excluded_id)
        if excluded_id != identifier and not (layer and excluded is not None and any(
                parent.layer_id == identifier
                for parent in self.chapter.ancestor_layers(excluded.parent_layer_id))):
            excluded_id = None
        return (kind, identifier, signature,
                tuple(getattr(mapping, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)),
                self._render_exclude_text, self._render_base_alpha, excluded_id,
                self._rendering_mask_contributor, self._rendering_compound_references,
                getattr(self, "_stroke_hide_border_id", None),
                getattr(self, "_effect_preview_channel", "canvas"))

    def _tiling_source(self, target, bounds):
        layer = isinstance(target, LayerNode)
        kind, identifier = ("layer", target.layer_id) if layer else ("object", target.object_id)
        key = ("tiling-source", self._tiling_target_signature(target), self._rect_signature(bounds))
        cached = self._modifier_source_cache_get(key)
        if cached is not None:
            return cached
        revision = getattr(self, "_effect_provisional_revision", 0)
        image = empty_image(bounds)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(-bounds.x(), -bounds.y())
        self._render_modifier_sources.add((kind, identifier))
        previous_scale = self._vector_render_scale_override
        previous_geometry = getattr(self, "_tiling_capture_geometry", None)
        if layer:
            self._tiling_capture_geometry = TilingGeometry.from_modifier(self._own_tiling(target))
        self._vector_render_scale_override = 1.
        try:
            if layer:
                self._tiling_children(painter, target, bounds)
            else:
                mapping = self.layer_world_transform(target.parent_layer_id)
                painter.setTransform(mapping, True)
                self._render_object_content(painter, target, mapping.inverted()[0].mapRect(bounds))
                if isinstance(target, VectorDrawingObject):
                    self._render_modified_vector_pencil_preview(painter, target.parent_layer_id)
        finally:
            self._vector_render_scale_override = previous_scale
            self._tiling_capture_geometry = previous_geometry
            painter.end()
            self._render_modifier_sources.discard((kind, identifier))
        if revision == getattr(self, "_effect_provisional_revision", 0):
            self._modifier_source_cache_put(key, image)
        return image

    def _tiling_raster_pixels(self, obj, geometry, world_bounds=None):
        destination = self._multi_transform_preview_quads.get(obj.object_id)
        if destination is None and obj.object_id == self.selected_object_id:
            destination = self._transform_preview_quad
        mapping = QTransform.fromTranslate(obj.x, obj.y)*self._drawing_object_transform(obj, destination)*self.layer_world_transform(obj.parent_layer_id)
        inverse = mapping.inverted()[0]
        bounds = inverse.map(geometry.path()).boundingRect()
        if world_bounds is not None:
            bounds = bounds.united(inverse.mapRect(world_bounds))
        bounds = aligned(bounds.adjusted(-2, -2, 2, 2))
        key = ("tile-raster-pixels", repr(self._modifier_object_signature(obj)), self._rect_signature(bounds))
        image = self._modifier_source_cache_get(key)
        if image is None:
            image = empty_image(bounds)
            painter = QPainter(image)
            painter.translate(-bounds.x(), -bounds.y())
            try:
                if not self._render_raster_selection_preview(painter, obj, bounds):
                    for (x, y), tile in self.tiles.iter_tiles(obj.object_id, bounds):
                        painter.drawImage(x*obj.tile_size, y*obj.tile_size,
                                          self._working_raster_tile(tile))
            finally:
                painter.end()
            self._modifier_source_cache_put(key, image)
        return image, bounds, mapping

    def _render_tiling_raster_capture(self, painter, obj, local_visible):
        geometry = getattr(self, "_tiling_capture_geometry", None)
        if geometry is None:
            return False
        parent = self.layer_world_transform(obj.parent_layer_id)
        bounds = aligned(local_visible)
        source, source_bounds, mapping = self._tiling_raster_pixels(obj, geometry, parent.mapRect(bounds))
        if not hasattr(self, "_tiling_sampling_cache"):
            self._tiling_sampling_cache = RepeatMapCache()
        image = repeat_image(source, source_bounds, geometry, bounds, mapping, nearest=True,
            output_to_world=parent, repeat=False, cache=self._tiling_sampling_cache)
        painter.drawImage(bounds.topLeft(), image)
        return True

    def _tiling_shape_style(self, painter, layer, *, outline):
        from comic_editor.render.shape_outline import outline_mesh
        path = self.layer_effective_path(layer.layer_id)
        painter.save()
        painter.setTransform(self.layer_world_transform(layer.layer_id), True)
        if outline and layer.border_width > 0 and getattr(self, "_stroke_hide_border_id", None) != layer.layer_id:
            from comic_editor.ui.compound_strokes import scoped, appearance, paint_outline
            if scoped(self, layer):
                from comic_editor.render.shape_outline_compound import OutlineSource
                styled = appearance(self, layer)
                sources = (self._compound_outline_mesh(layer, path, sources_only=True)
                           if layer.compound_enabled else [OutlineSource(
                               styled.bound if styled is not None else layer.bound, layer.border_width,
                               QTransform(), owner_id=layer.layer_id)])
                paint_outline(self, painter, layer, path, sources,
                              self._outline_tolerance(painter, path.boundingRect()))
                painter.restore()
                return
            elif layer.compound_enabled:
                coverage = self._compound_outline_mesh(layer, path, self._outline_tolerance(painter, path.boundingRect()))
            elif layer.layer_kind == "open_shape":
                style = layer.shape_style
                core = self.open_shape_mesh(layer.bound, style.base_thickness, 0, style.start_cap, style.end_cap, cache=self._outline_cache)
                coverage = outline_mesh(layer.bound, layer.border_width, path, core=core,
                    base_width=style.base_thickness, cache=self._outline_cache,
                    start_cap=style.start_cap, end_cap=style.end_cap)
            else:
                coverage = outline_mesh(layer.bound, layer.border_width, path, cache=self._outline_cache)
            painter.fillPath(coverage, working_color(layer.border_color))
        elif not outline:
            if layer.layer_kind == "open_shape":
                style = layer.shape_style
                core = self.open_shape_mesh(layer.bound, style.base_thickness, 0, style.start_cap, style.end_cap, cache=self._outline_cache)
                painter.fillPath(core, working_color(style.primary_color))
            elif layer.fill_color:
                painter.fillPath(path, working_color(layer.fill_color))
        painter.restore()

    def _tiling_stage(self, target, required=None):
        revision = getattr(self, "_effect_provisional_revision", 0)
        modifier = self._own_tiling(target)
        geometry = TilingGeometry.from_modifier(modifier)
        boundary = self._tiling_boundary(target)
        bounds = aligned(boundary.boundingRect())
        if required is not None:
            bounds = aligned(bounds.intersected(required))
        # Rebuilt source captures have new QImage identities but unchanged
        # pixels. Check the semantic output before capturing or repeating them.
        signature = self._tiling_target_signature(target)
        key = ("tiling-output", signature, self._rect_signature(bounds),
               tuple((boundary.elementAt(i).x, boundary.elementAt(i).y, boundary.elementAt(i).type)
                     for i in range(boundary.elementCount())))
        scope = ("tiling-output", self._effect_request_scope(*signature[:2]))
        retain = (self._interactive_render and not self._render_base_alpha
                  and self._rendering_mask_contributor <= 0
                  and getattr(self, "_effect_preview_channel", "canvas") != "navigator")
        cached = self._modifier_cache_get(key)
        if cached is None:
            completed = self._effect_jobs.retained_get(scope, key)
            cached = completed[0] if completed is not None else None
        if cached is not None:
            if retain:
                self._effect_jobs.retained_put(scope, key, cached)
            return cached, bounds
        source_bounds = aligned(geometry.bounds().adjusted(-2, -2, 2, 2))
        source_mapping = QTransform()
        if isinstance(target, RasterObject):
            source, source_bounds, source_mapping = self._tiling_raster_pixels(target, geometry)
        else:
            source = self._tiling_source(target, source_bounds)
        if not hasattr(self, "_tiling_sampling_cache"):
            self._tiling_sampling_cache = RepeatMapCache()
        repeated = repeat_image(source, source_bounds, geometry, bounds, source_mapping,
            nearest=isinstance(target, RasterObject), output_to_world=QTransform(), cache=self._tiling_sampling_cache)
        fields = self._modifier_mask_fields([modifier], repeated.width(), repeated.height(),
            QTransform.fromTranslate(-bounds.x(), -bounds.y()), bounds)
        amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity,
            (repeated.height(), repeated.width()), fields))/100
        if amount.ndim == 2:
            amount = amount[..., None]
        if np.ndim(amount) or float(amount) != 1:
            incoming = self._tiling_source(target, bounds)
            repeated = _premultiplied_qimage(_qimage_premultiplied(incoming)*(1-amount)
                                            + _qimage_premultiplied(repeated)*amount)
        result = empty_image(bounds)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(-bounds.x(), -bounds.y())
        if isinstance(target, LayerNode):
            self._tiling_shape_style(painter, target, outline=False)
        painter.save()
        painter.setClipPath(boundary)
        painter.drawImage(bounds.topLeft(), repeated)
        painter.restore()
        if isinstance(target, LayerNode):
            self._tiling_shape_style(painter, target, outline=True)
        painter.end()
        if revision == getattr(self, "_effect_provisional_revision", 0):
            self._modifier_cache_put(key, result)
            if retain:
                self._effect_jobs.retained_put(scope, key, result)
        return result, bounds

    def _render_tiled_target(self, painter, target, parent_opacity, visible_world):
        modifier = self._own_tiling(target)
        if modifier is None:
            return False
        rest = [m for m in self._active_modifier_instances(target.modifier_ids,
            suppress_outline=self._suppress_outline_for_mask) if not isinstance(m, TilingModifier)]
        if isinstance(target, LayerNode):
            from comic_editor.ui.compound_strokes import scoped
            if scoped(self, target):
                rest = [m for m in rest if not isinstance(m, StrokeModifier)]
        if self._render_base_alpha:
            rest = []
        # Other spatial effects may pull any part of the finite tiled fill.
        required = self._modifier_viewport_region(visible_world)
        revision = getattr(self, "_effect_provisional_revision", 0)
        image, bounds = self._tiling_stage(target, None if rest else required)
        provisional = revision != getattr(self, "_effect_provisional_revision", 0)
        kind, identifier = ("layer", target.layer_id) if isinstance(target, LayerNode) else ("object", target.object_id)
        if rest:
            scope = self._effect_request_scope(kind, identifier)
            source_key = ("tiled-stages", self._tiling_target_signature(target),
                          self._rect_signature(bounds))
            if isinstance(target, LayerNode) and any(isinstance(effect, SolidColorOverlayModifier)
                                                    and not effect.apply_to_outline for effect in rest):
                from comic_editor.ui.overlay_rendering import shape_overlay_stack
                image, bounds = shape_overlay_stack(self, target, image, bounds, rest, QTransform(),
                    source_key, scope, provisional=provisional, tiled=True)
            elif any(isinstance(effect, StrokeModifier) for effect in rest):
                from comic_editor.ui.stroke_rendering import render_stroke_stack
                image, bounds = render_stroke_stack(self, target, image, bounds, rest,
                    QTransform(), source_key, scope, tiled=True,
                    provisional=provisional)
            else:
                image, bounds = render_stages(self, image, bounds, rest, QTransform(),
                    nearest=isinstance(target, RasterObject), required=required, request_scope=scope,
                    provisional=provisional, source_key=source_key)
        if target.opacity_mask is not None:
            binding = target.opacity_mask
            image = apply_opacity_mask(image, self.render_tone_mask_field(binding.mask_id,
                image.width(), image.height(), QTransform.fromTranslate(-bounds.x(), -bounds.y()), bounds),
                binding.black_value, binding.white_value)
        parent = target.parent_id if isinstance(target, LayerNode) else target.parent_layer_id
        mapping = self.layer_world_transform(parent) if parent else QTransform()
        opacity = target.opacity if isinstance(target, LayerNode) or not target.opacity_locked else 1.
        if self._render_base_alpha:
            opacity = 1.
        painter.save()
        painter.setTransform(mapping.inverted()[0], True)
        painter.setOpacity(parent_opacity*opacity)
        painter.drawImage(bounds.topLeft(), image)
        painter.restore()
        return True

