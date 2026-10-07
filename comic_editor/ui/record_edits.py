"""Focused property history and typed compatibility notifications."""
from comic_editor.core.document_patch import DocumentPatch, RecordSnapshot


def selected_snapshot(canvas, *, layer_opacity=False, attributes=None):
    refs = canvas.selected_entities or [(canvas.selected_kind, canvas.selected_id)]
    layers = {identifier for kind, identifier in refs if kind == 'layer'}
    objects = {identifier for kind, identifier in refs if kind == 'object'}
    if layer_opacity:
        for identifier in layers:
            objects.update(child.entity_id for child in canvas.chapter.layers[identifier].children
                if child.kind == 'object' and canvas.chapter.objects[child.entity_id].opacity_locked)
    return RecordSnapshot.capture(canvas.chapter, layers=layers, objects=objects, attributes=attributes)


def preview_records(canvas, before, label='Edit properties'):
    after = before.after(canvas.chapter)
    old, new = DocumentPatch.pair(before, after)
    change = new.change_set(old, transient=True, label=label)
    if change.empty:
        return False
    change = canvas._history_change_with_bounds(change, old, new)
    canvas._publish_change_set(change, action='transient')
    canvas._emit_typed_document_changed(None, change)
    canvas.update()
    return True


def commit_records(canvas, before, label, *, hierarchy=False):
    if before.document_identity != id(canvas.chapter):
        return False
    after = before.after(canvas.chapter)
    if before == after:
        return False
    canvas.push_model_change(before, after, label)
    change = canvas._last_published_change
    if hierarchy:
        canvas._emit_typed_hierarchy_changed(change)
    canvas._emit_typed_document_changed(None, change)
    canvas.update()
    return True
