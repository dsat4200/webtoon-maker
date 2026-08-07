"""Compatibility import path for the widget-free renderer backend."""

from .offscreen import OffscreenRenderer, RenderMetrics, RenderOptions, RendererUnavailable

Renderer = OffscreenRenderer

__all__ = ["OffscreenRenderer", "Renderer", "RenderMetrics", "RenderOptions", "RendererUnavailable"]

