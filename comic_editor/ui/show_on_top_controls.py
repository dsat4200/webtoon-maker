"""Shared undoable action for settings and the outliner crown."""
from comic_editor.core.document_patch import RecordSnapshot
from comic_editor.ui.record_edits import commit_records


def set_show_on_top(canvas, kind, identifier, enabled):
    chapter = canvas.chapter
    if chapter is None:
        return False
    collection = chapter.layers if kind == "layer" else chapter.objects if kind == "object" else {}
    entity = collection.get(identifier)
    enabled = bool(enabled)
    if entity is None or entity.show_on_top == enabled:
        return False
    # A crown can belong to a different row while typing remains active.
    canvas.commit_active_text_edit()
    group = 'layers' if kind == 'layer' else 'objects'
    before = RecordSnapshot.capture(chapter, **{group: [identifier]}, attributes={group: ('show_on_top',)})
    entity.show_on_top = enabled
    commit_records(canvas, before, "Change show on top", hierarchy=True)
    return True
