"""Compare large-outliner work with the former sibling scans and row sizing.

Run from the repository root. The synthetic document and temporary settings do
not touch an open project. Results are written to .artifacts/large-hierarchy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QModelIndex
from PySide6.QtWidgets import QApplication

from comic_editor.core import settings
from comic_editor.core.models import ChapterDocument, VectorDrawingObject
from comic_editor.core.tiles import TileStore
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.tree_model import HierarchyModel


def measure(operation, repeats=9):
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(samples[1:]), 3)


def legacy_index(self, kind, identifier):
    item = self._items.get((kind, identifier))
    if item is None or item.parent is None:
        return QModelIndex()
    return self.createIndex(item.parent.children.index(item), 0, item)


def legacy_parent(self, index):
    if not index.isValid():
        return QModelIndex()
    item = self.item_for_index(index)
    parent = item.parent
    if parent is None or parent is self.root:
        return QModelIndex()
    grandparent = parent.parent or self.root
    return self.createIndex(grandparent.children.index(parent), 0, parent)


def legacy_expanded(self):
    result = set()
    for identifier in self.chapter.layers:
        index = self.hierarchy_model.index_for_entity("layer", identifier)
        if index.isValid() and self.tree.isExpanded(index):
            result.add(identifier)
    for identifier, obj in self.chapter.objects.items():
        if isinstance(obj, VectorDrawingObject):
            index = self.hierarchy_model.index_for_entity("object", identifier)
            if index.isValid() and self.tree.isExpanded(index):
                result.add(identifier)
    return result


def main():
    app = QApplication.instance() or QApplication([])
    old_settings_path = settings.settings_path
    optimized_index = HierarchyModel.index_for_entity
    optimized_parent = HierarchyModel.parent
    optimized_expanded = MainWindow._expanded_layer_ids
    output = Path(__file__).resolve().parents[1] / ".artifacts" / "large-hierarchy"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory() as temporary:
        settings.settings_path = lambda: Path(temporary) / "settings.json"
        window = MainWindow()
        try:
            for count in (250, 1000, 3000):
                chapter = ChapterDocument()
                page = chapter.add_page()
                for number in range(count):
                    layer = chapter.add_layer(page.layer_id, str(number))
                    chapter.add_object(layer.layer_id, VectorDrawingObject())
                window._set_chapter(chapter, TileStore())
                window.canvas.set_selection("object", next(iter(chapter.objects)))
                row = {"layers": count, "objects": count}
                for legacy in (True, False):
                    HierarchyModel.index_for_entity = legacy_index if legacy else optimized_index
                    HierarchyModel.parent = legacy_parent if legacy else optimized_parent
                    MainWindow._expanded_layer_ids = legacy_expanded if legacy else optimized_expanded
                    window.tree.setUniformRowHeights(not legacy)
                    row["before" if legacy else "after"] = {
                        "capture_expansion_ms": measure(window._expanded_layer_ids),
                        "rebuild_outliner_ms": measure(window._refresh_hierarchy),
                    }
                results.append(row)
                print(json.dumps(row), flush=True)
        finally:
            HierarchyModel.index_for_entity = optimized_index
            HierarchyModel.parent = optimized_parent
            MainWindow._expanded_layer_ids = optimized_expanded
            window.autosave_timer.stop()
            window.layout_settings_timer.stop()
            window.series_preferences_timer.stop()
            window.canvas._effect_jobs.cancel()
            window.hide()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
            settings.settings_path = old_settings_path
    (output / "timings.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
