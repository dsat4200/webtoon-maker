# Webtoon Maker LLM documentation

This folder is a source-verified guide to the current `webtoon-maker` codebase. It is intended for language models and developers who need a compact but complete mental model before changing the application.

## Recommended ingestion order

1. [01-product-features-and-modes.md](01-product-features-and-modes.md) — what the application does, its complete feature set, every canonical canvas tool, aliases, internal creation tools, and user-selectable operating modes.
2. [02-canvas-rendering-and-drawing.md](02-canvas-rendering-and-drawing.md) — the 2D rendering pipeline, coordinate systems, tone masks, modifier stack, raster and vector drawing, gradients, text, hit testing, transforms, and caches.
3. [03-data-session-and-persistence.md](03-data-session-and-persistence.md) — the saved document graph, sparse raster data, masks and modifiers, image storage, runtime session state, undo/redo, settings, autosave, recovery, and on-disk layout.
4. [04-codebase-structure-and-scripts.md](04-codebase-structure-and-scripts.md) — repository hierarchy and a one-by-one description of every runtime, support, and test script.

## Scope and authority

- The executable application is the root `main.py` plus the `comic_editor/` package.
- `tests/` is part of the engineering codebase and is documented file by file.
- `lighter-novel/` contains story/planning material and Obsidian workspace files. It is not imported by the editor.
- `arst/` and `Test/` are user series data folders produced by running the editor; they are not code.
- `paint/handles.png` is an unreferenced image asset in the current source.
- `program-map.txt` is a useful historical generated map, but it is not authoritative. Its line counts, schema versions, and file lists lag the current source. These documents follow the current code.
- The current chapter schema is version 21, the series schema is version 17, asset manifests are schema 2, editor settings are version 21, the Blender bridge protocol is version 2, and the Blender extension is version `0.3.0`.

## One-paragraph architecture

Webtoon Maker is a native PySide6 desktop editor for fixed-width, vertically growing comics. `MainWindow` owns application workflow, contextual controls, and one `EditorSession` per open series/asset tab; `_CanvasLogic` owns rendering and nearly all pointer/stylus/touch interaction; `ChapterDocument` owns the validated layer/object graph plus chapter-level tone masks and the non-destructive modifier stack; `TileStore` owns sparse raster pixels for both Raster objects and tone-mask paint; `ImageStore` owns embedded image bytes plus transient live Blender overrides; the vector geometry module supplies cubic fitting, projection, erasing, simplification, connection, intersection, and face tracing; and `SeriesRepository` publishes portable JSON/PNG series folders with autosave and interrupted-save recovery. The optional Blender 4.5 extension exposes geometry-free Comic View snapshots and transparent RGBA frames through an authenticated loopback protocol and shared memory; the editor consumes them through the generic Image Object path.

## Important terminology

- **Series**: a portable project folder with preferences and a list of chapters.
- **Chapter**: one 1080-pixel-wide, vertically growing canvas and its complete object graph.
- **Page**: a root shape layer positioned within a chapter. Pages need not tile the chapter and may overlap.
- **Layer**: a bounded shape container or an open stroked shape. Legacy fill layers no longer exist as a runtime kind; they are materialized into sparse raster tiles at load.
- **Object**: raster, image, text, vector drawing, or gradient data attached to a container layer.
- **Tone mask**: a chapter-level grayscale field built from contributor entities plus optional raster paint, used to drive parameter masks and the blue mask-mode overlay.
- **Parameter mask**: a binding that maps a tone mask through black/white endpoint values onto a target parameter, e.g. a layer/object opacity or a modifier attribute.
- **Modifier**: a non-destructive HSL, blur, or outline stage in a layer/object's modifier stack, blended by an intensity that can itself be masked.
- **Mask-only entity**: a layer or object hidden from normal scene rendering that contributes only to tone masks.
- **Comic View**: a geometry-free Blender scene-state snapshot identified by project and view UUIDs.
- **Linked Image**: an Image Object whose replaceable source is a Comic View and whose last accepted frame is persisted as an offline PNG.
- **Direct parent mask**: the immediate shape used to clip a child.
- **Compound mask**: the Boolean result of a compound layer and contributing descendants.
- **Interaction frame**: a raster object's editable rectangle; it does not crop or clip stored pixels.
- **Underlay**: a live editing aid that redraws the selected raster/vector drawing outside its normal mask at reduced opacity. It is not part of preview output.
