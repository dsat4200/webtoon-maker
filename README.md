# Vertical Comic Editor

A native PySide6 drawing editor for 1080-pixel-wide, vertically scrolling
webtoon and hybrid web-novel comics.

This repository is independent from Drawing SRS. It contains no sessions,
ratings, scheduling, references, music, gallery, study database, or practice
workflow.

## Current foundation

- Portable series folders and versioned chapter manifests
- Growable 1080px-wide chapters
- Freely positioned page layers
- Nested rectangle, circle, and polygon bounded layers
- Non-destructive hierarchical masks
- Sparse 256×256 raster tiles
- Explicit, non-clipping raster interaction frames with drag-to-create
- Named pressure-curve pencil presets, independent pressure channels,
  density/taper/antialias controls, and configurable S/M/L sizes
- Live on-canvas text editing with free/projective and strict wrapped layouts
- Eight-handle free/uniform transforms for text and sparse raster objects
- Cached, non-baking raster transform previews and spatially culled commits
- Global formatting-only text presets
- Drag-reorderable layer/object tree
- Named, bounded layers with non-destructive bound translation/editing
- Rounded layer masks, optional fills/borders, and boundless fill leaves
- Unified rectangle, circle, and open/closed vector-Bézier Shape paths
- Per-point width, roundness, linked controls, outlines, and endpoint caps
- Frontmost-first layer/page selection with Ctrl-click candidate menus
- Per-layer inherited or overridden grids
- Pixel-snapped pan, zoom, rotation, and aspect-preserving chapter preview navigation
- Touch navigation controlled only by Tablet Navigation mode
- Command-based undo/redo and atomic autosave recovery

## Run

```powershell
python -m pip install -r requirements.txt
python main.py
```

Navigation defaults:

- `Alt` + drag: pan
- `Shift` + drag: rotate
- `Alt+Shift` + drag: zoom
- Mouse wheel: vertical scroll
- `Ctrl` + mouse wheel: zoom
- `P`, `E`, `S`, `T`, `B`: Pencil, Eraser, Object Select, Transform, Shape Edit

Expand Add Bound to choose Rectangle, Circle, or Shape. In Shape creation,
clicking adds vector points, dragging creates Bézier points, clicking the
first point closes the shape, and Enter or double-click commits an open line.
Shape Edit exposes point, curve, roundness, width, type, lock, and end-cap
gizmos using the blue/orange visual language.

Select a text object to enter Text Edit. Strict text wraps to its parent
layer with a uniform margin and 3×3 alignment; Free text remains editable
through projective transforms. Raster transforms bake into sparse tiles and
offer a contextual one-shot Undo Transform restore.

Use Add Raster and drag a frame before drawing. Shape Edit changes a raster
frame without scaling or cropping its pixels. A stroke that begins inside the
frame can expand it; clicking outside it performs page-scoped object selection.

## Test

```powershell
pytest
```
