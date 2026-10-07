"""Shared semantic source policy for memory and disk-backed artwork."""
from .pixels import LEGACY_PIXELS, current_contract, working_representation


def source_color_context(document_contract, scoped_contract=None):
    """Describe the actual source route without loading unused color configs.

    Temporary float effects in legacy documents still use display source pixels.
    Only jointly floating document and scope policies enter the native working
    source conversion. Ordinary legacy keys retain their established identity.
    """
    actual = document_contract if document_contract is not None else LEGACY_PIXELS
    scoped = current_contract() if scoped_contract is None else scoped_contract
    if not actual.floating and not scoped.floating:
        return ()
    color = working_representation(scoped)[1] if actual.floating and scoped.floating else None
    return ('source-color-context', actual.signature, scoped.signature, color)


def contextual_source_key(key, context):
    """Keep compact source/prefix shapes while appending their semantic policy."""
    if key is None or not context:
        return key
    if (isinstance(key, tuple) and len(key) == 3
            and key[:1] in {('live-effect-draft-source',), ('navigator-source',)}):
        return key[0], key[1], contextual_source_key(key[2], context)
    if isinstance(key, tuple):
        return key if key and key[-1] == context else (*key, context)
    return ('source-context-preview', key, context)
