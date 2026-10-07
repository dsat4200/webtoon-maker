"""Compatibility alias for the shared non-widget rendering implementation."""
import sys
from comic_editor.render import tile_effects as _implementation
sys.modules[__name__] = _implementation
