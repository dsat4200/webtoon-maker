"""Compatibility alias for the shared non-widget rendering implementation."""
import sys
from comic_editor.render import shape_outline_compound as _implementation
sys.modules[__name__] = _implementation
