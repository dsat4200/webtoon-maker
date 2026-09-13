"""Shared undoable action for settings and the outliner crown."""


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
    before = chapter.to_dict()
    entity.show_on_top = enabled
    canvas.push_model_change(before, chapter.to_dict(), "Change show on top")
    canvas.documentChanged.emit(None)
    canvas.hierarchyChanged.emit()
    canvas.update()
    return True
