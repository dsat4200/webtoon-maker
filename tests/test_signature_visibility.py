"""Same-scope visibility changes retain distinct semantic dependency tokens."""
import pytest

from comic_editor.ui.render_signatures import signature_scope
from test_render_signature_scope import scene


def uncached(canvas, function, argument):
    memo = canvas._render_signature_memo
    del canvas._render_signature_memo
    try:
        return function(argument)
    finally:
        canvas._render_signature_memo = memo


@pytest.mark.parametrize('kind', ['object', 'layer'])
@pytest.mark.parametrize('change', ['interactive-export', 'selected-kind', 'selected-id', 'disk-capture'])
def test_same_scope_selected_mask_only_visibility_context(scene, kind, change):
    canvas, layer, obj, _effect, _mask = scene
    target = obj if kind == 'object' else layer
    target.mask_only = True
    identifier = obj.object_id if kind == 'object' else layer.layer_id
    function = canvas._modifier_object_signature if kind == 'object' else canvas._modifier_layer_signature
    argument = obj if kind == 'object' else layer.layer_id
    canvas.set_selection(kind, identifier)
    canvas._interactive_render = True
    canvas._disk_cache_capture = False
    context = canvas._document_projection.revision
    with signature_scope(canvas):
        visible = function(argument)
        assert visible == uncached(canvas, function, argument)
        assert canvas._mask_only_render_visible(kind, identifier)
        assert function(argument) == visible
        if change == 'interactive-export':
            canvas._interactive_render = False
        elif change == 'selected-kind':
            canvas.selected_kind = 'layer' if kind == 'object' else 'object'
        elif change == 'selected-id':
            canvas.selected_id = 'other-selection'
        else:
            canvas._disk_cache_capture = True
        assert canvas._document_projection.revision == context
        assert not canvas._mask_only_render_visible(kind, identifier)
        oracle = uncached(canvas, function, argument)
        assert oracle != visible
        assert function(argument) == oracle
        canvas._interactive_render = True
        canvas.selected_kind, canvas.selected_id = kind, identifier
        canvas._disk_cache_capture = False
        assert function(argument) == visible
    assert not hasattr(canvas, '_render_signature_memo')
    # New captures still rebuild current model dependencies rather than reuse
    # the preceding capture's token after a direct unsignaled change.
    target.opacity = .375
    direct = function(argument)
    assert direct != visible
    with signature_scope(canvas):
        assert function(argument) == direct
