# Raster Brush integration plan

Investigation date: 2026-09-26. The working tree was clean at investigation start. Runtime source was checked against `docs/llm`; some details there predate the current retained projection renderer. No user projects or preferences were changed by this investigation.

## Existing architecture and limits

- `ui/canvas.py::_CanvasLogic` owns dispatch, camera/object coordinate conversion, selection, and raster stroke transactions. Both QWidget and OpenGL canvases render the same sparse QImage pixels. The raster target is a `RasterObject`, not the whole chapter bitmap.
- `core/tiles.py::TileStore` stores signed 256×256 object-local tiles in premultiplied ARGB. Rendering already handles transformed objects, clipping, layers, modifiers, preview, save, and export; a new brush should continue writing these tiles.
- `core/pressure.py::BrushPreset` is the existing Pencil model: size/opacity curves, density, antialiasing, and first/last dab size. Keep it compatible. It is too narrow for image tips, scatter, texture, accumulation, color mixing, or SUT metadata.
- `TileStore.paint_segment` restarts dab spacing at every input segment. That makes pattern frequency dependent on event delivery. A separate stroke scheduler must retain distance/spacing remainder across samples and use a fixed random seed for deterministic replay.
- `_paint_samples` paints circle/square dabs using source-over, source, or clear and batches independent dabs per tile. This batching is safe for ordinary stamps; color pickup and wet mixing require chronological, neighbor-aware sampling. Do not reorder those dabs by tile.

## Tool and UI integration points

Add `ToolKind.BRUSH = "brush"` as a distinct raster-only tool. Do not add it to Pencil's vector or tone-mask branches by a broad replacement.

Canvas changes must cover:

1. `set_tool`: require a selected RasterObject, reject mask mode and multiple selection; clear or finish active Brush state when tool changes.
2. `_tool_press/_tool_move/_tool_release`: route Brush to independent begin/add/finish helpers before legacy pencil/vector dispatch. Existing `_raster_local_point` performs the inverse parent/object projective mapping; `_drawing_local_rect_to_world` maps dirty bounds back.
3. Selection/frame interaction: hide transform handles while Brush draws, and allow strokes to extend beyond the interaction frame. This frame is a selection affordance, never a pixel clip.
4. `tabletEvent`: capture tilt X/Y, barrel rotation, and monotonic time before routing a brush sample. Preserve `_effective_pressure` behavior: genuine pen zero pressure is meaningful; mouse zero defaults to full pressure. Mouse input needs zero tilt and rotation. Do not discard timestamps needed by speed and time-based airbrush modes.
5. `_clear_detached_input_state`, `set_selection`, document/session switches, tool changes, and error handling: finish or roll back the same captured object ID, release stroke timers, clear transient buffers, and restore GC. Never derive the transaction's owner from a potentially changed selection.
6. Hover/cursor size, `_drawing` activity, `interactionFinished`, and dirty emission so drawing remains included in the existing render scheduling and performance instrumentation.

`ui/main_window.py` owns tool creation, hotkey mapping, activation, enable/visibility, contextual ribbon selection, settings persistence, and color refresh. The new button must be visible/enabled only for RasterObject targets. `_activate_tool` may reuse a layer's `last_raster_id`. Existing Pencil and Eraser remain available. `set_selection` currently resets selected rasters to Pencil; preserve Brush when selecting another raster if it was already active.

`ui/tool_ribbon_pages.py::ToolSettingsControls` is a stack of tool pages. Add a Brush page with preset, size, opacity, import, and detailed settings. Implement the larger editor separately to avoid further inflating the canvas and window modules. Keep warnings about imported features visible in the brush details.

## Proposed boundaries and contract

| Owner/module | Responsibility |
|---|---|
| `core/brushes.py` | Serializable versioned BrushDefinition, dynamic curves/channels, tip/texture resources, BrushInput. Preserve original importer data and unsupported-feature diagnostics. No canvas/window dependencies. |
| `core/brush_stroke.py` | Deterministic sample-to-dab/path scheduler: continuous spacing, interpolation, dynamics, random selection/scatter, direction, taper, stabilization. No TileStore or scene dependencies. |
| `core/brush_raster.py` | RasterBrushStroke adapter and tip/texture rasterization, composition, pickup/mixing. Bounds-limited, byte-budgeted caches. Writes TileStore through a captured transaction. |
| `core/sut_import.py` | Bounded SUT decoding to BrushDefinition/resources and an honest import report. No dependency on UI or active document. |
| `ui/brush_features.py` | Gesture lifecycle and raster transaction integration. |
| `ui/brush_controls.py` | Preset library/editor/import controls and diagnostic presentation. |
| `core/settings.py` | Active preset and overrides plus user brush library metadata. |

Preferred adapter: `RasterBrushStroke(tiles, object_id, definition, color: QColor, before: dict, seed=0)`. `begin(BrushInput)`, `add(BrushInput)`, and `finish()` return object-local `QRectF` dirty bounds. Capture immutable definition/color at stroke start. Input fields: `x`, `y`, `pressure`, `tilt_x`, `tilt_y`, `rotation`, and `time`. Empty return means no pixel mutation.

Separating sample scheduling from raster composition leaves future vector support able to retain a brush definition, resource IDs, seed, and source samples and replay identical dabs. Do not introduce new vector objects, fit brush appearance into existing scalar vector widths, or add vector support in this stage.

## Undo, rendering, persistence, and performance

- Capture each touched tile with `QImage(image)` before first mutation; Qt copy-on-write makes this cheap. Update TileStore's `dirty`, alpha-bounds cache, and changed tiles consistently. A `set_tile` route is safe initially, though repeated full-tile alpha scans per dab should be avoided.
- Reuse `TilePatchCommand` with before/after touched tiles and RasterObject frame state. Release must finish deferred dabs before taking `after`. A failed or canceled stroke restores pixels and its original frame and causes no undo entry.
- Use `_emit_raster_dirty` for incremental scene invalidation and frame/chapter growth; `_raster_fill_visual_changed` for undo/redo. Avoid full `documentChanged(None)` per move. Commit once at release. The existing command stack retains up to 200 commands.
- A raster brush needs no new chapter schema: finished pixels persist normally in raster tile PNGs and follow the existing manifest-last save/recovery protocol. User brush definitions belong in editor settings/library storage, not chapter JSON. Large embedded tips should move to content-addressed files outside settings JSON if real imported libraries make JSON impractical. Never persist decoded QImage/runtime caches.
- Keep immutable brush resources and raster caches separate. Include all tip/color/scale/rotation/texture parameters in cache keys, use a byte budget, and avoid a cache entry per continuous pressure/angle value without bounded quantization.
- Preserve the accepted policy in `docs/drawing-stutters-2026-09-26.md`: visible drawing and stroke release render synchronously; asynchronous scene work is limited to navigation of unchanged documents. Do not solve expensive brush work by silently skipping samples or presenting stale final artwork.
- An airbrush timer must paint while the pointer is stationary, stop on every termination path, and use elapsed time so event rate does not change deposited paint. Guard pathological imported density/size/particle counts with documented limits and report changes.

## Focused validation

1. Deterministic scheduler equivalence for dense/sparse samples of the same straight path; pressure peaks; repeated seed; sustained zero-distance input; direction and spacing across corners.
2. Tile-boundary and negative-coordinate stamps, premultiplied alpha, antialiasing, partial-opacity overlap, image-tip recoloring versus color preservation, and texture alignment without seams.
3. New raster Brush tool selection, mouse/tablet sample capture, no activation on vectors/masks, drawing beyond frames, transformed targets, abort/document-switch cleanup, one undo entry, byte-identical redo, and save/load pixel equality.
4. Real SUT fixtures spanning simple pens, pencils, airbrush, image decoration, foliage, chain/ribbon, and paint mixing, with unsupported fields explicitly recorded. Fixture licensing determines which files can enter the repository.
5. CSP and Webtoon Maker comparisons using matched brush size/color, document scale, mouse path, and brush controls. Compare texture/scatter characteristics as well as outline/opacity; random CSP seeds may prevent exact-pixel matching.
6. Existing regression gates: `tests/test_canvas_input_performance.py`, `tests/test_tiles_and_commands.py`, relevant settings/UI tests, and a sustained native input run if the brush implementation changes scheduling. Existing test fixtures isolate application settings; interactive validation must use a disposable project.

The final drawing playground should contain clearly labeled, independent raster targets for each family and keep useful empty space for manual drawing. Include imported brushes only where available and accurately label approximation/unsupported features.
