# Webtoon Maker LLM documentation

This folder is a source-verified guide to the current `webtoon-maker` codebase. It is intended for language models and developers who need a compact but complete mental model before changing the application.

## Recommended ingestion order

1. [01-product-features-and-modes.md](01-product-features-and-modes.md) — what the application does, its complete feature set, every canonical canvas tool, aliases, internal creation tools, and user-selectable operating modes.
2. [02-canvas-rendering-and-drawing.md](02-canvas-rendering-and-drawing.md) — the 2D rendering pipeline, coordinate systems, masks, raster and vector drawing, gradients, text, hit testing, transforms, and caches.
3. [03-data-session-and-persistence.md](03-data-session-and-persistence.md) — the saved document graph, sparse raster data, runtime session state, undo/redo, settings, autosave, recovery, and on-disk layout.
4. [04-codebase-structure-and-scripts.md](04-codebase-structure-and-scripts.md) — repository hierarchy and a one-by-one description of every runtime, support, and test script.

## Scope and authority

- The executable application is the root `main.py` plus the `comic_editor/` package.
- `tests/` is part of the engineering codebase and is documented file by file.
- `lighter-novel/` contains story/planning material and Obsidian workspace files. It is not imported by the editor.
- `paint/handles.png` is an unreferenced image asset in the current source.
- `program-map.txt` is a useful historical generated map, but it is not authoritative. Some names and counts in it lag the current source. These documents follow the current code.
- The current document schema is version 16, editor settings are version 12, and the package reports version `0.1.0`.

## One-paragraph architecture

Webtoon Maker is a native PySide6 desktop editor for fixed-width, vertically growing comics. `MainWindow` owns application workflow and contextual controls; `_CanvasLogic` owns rendering and nearly all pointer/stylus/touch interaction; `ChapterDocument` owns the validated layer/object graph; `TileStore` owns sparse raster pixels; the vector geometry module supplies cubic fitting, projection, erasing, simplification, connection, intersection, and face tracing; and `SeriesRepository` publishes portable JSON/PNG series folders with autosave and interrupted-save recovery. Rendering is QPainter-based in both software and OpenGL-backed widgets.

## Important terminology

- **Series**: a portable project folder with preferences and a list of chapters.
- **Chapter**: one 1080-pixel-wide, vertically growing canvas and its complete object graph.
- **Page**: a root shape layer positioned within a chapter. Pages need not tile the chapter and may overlap.
- **Layer**: a shape/mask container, open stroked shape, or boundless fill leaf.
- **Object**: raster, text, vector drawing, vector fill, or gradient data attached to a container layer.
- **Direct parent mask**: the immediate shape used to clip a child.
- **Compound mask**: the Boolean result of a compound layer and contributing descendants.
- **Interaction frame**: a raster object's editable rectangle; it does not crop or clip stored pixels.
- **Underlay**: a live editing aid that redraws the selected raster/vector drawing outside its normal mask at reduced opacity. It is not part of preview output.
