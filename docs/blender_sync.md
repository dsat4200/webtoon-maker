# Blender-linked 3D frame layers

This document describes the implementation in this repository. The desktop,
renderer, sidecar, sync transport, and private add-on paths are connected. Native
Blender 4.5.5 execution is still a release acceptance step because that Blender
version is not installed in the development environment used for the automated
run.

## Architecture and ownership

The renderer is vendored under `comic_editor.three_d` and has no runtime
dependency on the sibling renderer checkout. Its provenance is recorded in
`comic_editor/three_d/renderer/PROVENANCE.md` at audited commit
`d13b75fc437fad8e990c87a4e2c0d7e6bdf7e73d`. Human, character,
randomized-view, saved-project, frame-library, and asset-library features are
excluded.

| Owner | Authoritative data |
| --- | --- |
| Blender | Geometry, evaluated modifier results, hierarchy, rest rigs, source transforms, source material slots and assignments, cameras, lights, and raw Freestyle marks |
| Webtoon Maker | The closed 2D boundary and layer order, sparse presentation overrides, drawing materials, app projection/render settings, navigation, and local lights/cubes/cylinders |
| Comic-frame sidecar | Captured Blender base state plus Webtoon overrides, collection inclusion, revisions, warnings, and content hashes; never mesh bytes |

All exchange transforms use glTF coordinates: right-handed, Y-up, meters, with
16-value column-major local/world matrices. Imported nodes retain authored
origins, parents, signed scale, and residual shear. A 3D viewport is a real leaf
`LayerNode` with `layer_kind="blender"`, a unique `comic_frame_id`, and a closed
rectangle, ellipse, or free-form boundary. Its visible `Blender` outliner group
is virtual; the layer has no ordinary children.

## Schemas, files, and recovery

The chapter/series schema is 16. The Blender chapter document, comic frame,
drawing material, cache manifest, sync bundle/receipt, and render request/result
use independently validated version-1 contracts. Future versions are rejected.

```text
<series>/
  series.json
  chapters/<chapter-id>/
    chapter.json
    blender/
      manifest.json
      frames/<frame-id>.json
      cache/blobs/<sha256>.glb
      cache/revisions/<revision>.json
      inbox/<transaction>.ready/
      inbox/receipts/<transaction>.json
    autosave/
      chapter.json
      blender/...
    last_good/
      chapter.json
      blender/...
```

Immutable GLB blobs are published first, frame/cache records next, the Blender
manifest after them, and `chapter.json` last. Manual save, autosave,
interrupted-save restoration, `last_good`, recovery open, and Save As include
the mutable sidecar. Save As rebinds every copied chapter sidecar to the clone's
new series ID, including chapters that were not open in a tab.

After a successful manual save, cache collection validates every current,
autosave, last-good, inbox, and candidate revision record before changing
anything. It preserves those roots plus hashes retained by undo state, atomically
narrows the revision catalog, and then removes obsolete revisions/blobs. A
cleanup failure cannot turn an already durable chapter save into a failure; it
is reported as a save warning and retried on a later save.

A missing `.blend` leaves the latest valid cache usable. Missing or corrupt
frame/cache data adds bounded warnings and displays a clipped placeholder while
the rest of the 2D chapter remains open. Rendered raster results are retained in
the active controller but are not persisted as a separate cross-session cache.

Each 3D layer owns one frame. Duplicate creates an independent frame copy;
delete tombstones/removes that frame transactionally and defers blob cleanup to
save. A chapter accepts exactly one Blender file UUID. Replacing it requires the
explicit default-No **Replace Blender Association** confirmation and follows the
normal dirty-tab protections.

## Install and register the private Blender add-on

The add-on targets **Blender 4.5.5 LTS**, uses `bl_info`, and has no third-party
Python dependencies.

1. Zip `blender_addon/webtoon_sync` so the archive contains the
   `webtoon_sync` package and its `__init__.py`.
2. In Blender, use **Edit > Preferences > Add-ons > Install from Disk** and
   enable **Webtoon Maker Comic Frame Sync**.
3. Open a 3D Viewport, press **N**, and choose the **Webtoon** tab.

This remains a private legacy add-on; public Extension packaging is deferred.
See Blender's [add-on deployment guide](https://docs.blender.org/manual/en/4.5/advanced/deploying_blender.html)
and [Extension packaging guide](https://docs.blender.org/manual/en/4.5/advanced/extensions/getting_started.html).

For normal registration:

1. Create/select a Webtoon 3D layer and click **Copy Add-on Connection
   Settings** on its View ribbon page.
2. In Blender, click **Paste / Import Webtoon Registration**.
3. Choose a registered **Comic Frame** and click **Apply**.
4. Refresh the participating-collection tree and check only the collections
   that belong to this frame. Newly discovered collections remain unchecked.

The copied JSON includes the active loopback endpoint, random bearer token,
chapter/inbox paths, series/chapter identity, valid frame IDs, and current
revisions. The endpoint/token are session-only Blender properties. Diagnostic
raw fields remain visible, but ordinary setup does not require copying them
one-by-one. The same `.blend` may register explicit bindings for multiple
`(series_id, chapter_id)` pairs.

## Update, preview, and conflicts

**Update Comic Frame** requires a named `.blend`. If Blender or newly minted IDs
are dirty, the operator asks for **Save and Sync**. It restores an active preview
before capture, validates identities, exports/captures, stages and hashes the
whole transaction, atomically renames it to `<transaction>.ready`, then sends an
authenticated JSON-RPC notification to Webtoon Maker.

If Webtoon is closed, the ready bundle remains explicitly **queued, not
accepted**. When its chapter is opened, the desktop coordinator scans the
offline inbox. Accepted publication is one atomic, undoable Webtoon command;
undo/redo create monotonic sidecar revisions rather than rewinding counters.

Receipt states are:

- `accepted`: identity, hashes, common-base revisions, and publication all
  succeeded;
- `queued`: no reachable registered Webtoon process has accepted the bundle;
- `conflicts`: Blender and Webtoon changed the same shared presentation field;
- `rejected`: validation failed and visible state was not changed.

The desktop conflict dialog groups fields by category. **Keep Webtoon
Override** is the default; **Use Blender Value** removes that sparse override.
A choice can be applied to the remaining fields in its category. Geometry,
hierarchy, source assignments, and Freestyle metadata follow Blender. Drawing
materials, app-only projection/render fields, and local entities never get
overwritten by Blender.

**Apply Comic Frame Preview** changes only the current Blender session, records
an in-memory restoration snapshot, inserts no keyframes, and never saves.
**Restore Blender State** replays the snapshot. Capture always restores preview
state first so presentation overrides cannot be mistaken for source edits.

## Capture and modifier policy

Typed capture includes:

- scene/timeline identity, active camera, participating collections, and
  collection visibility;
- exact object local/world/parent-inverse matrices and visibility;
- pose-bone matrices/custom values and shape-key values/ranges;
- camera transform, lens/FOV, ortho scale, clip range, shift, and sensor fit;
- light transform/type/color/energy/range/area/spot/shadow values plus raw
  Blender semantics;
- material slots/assignments, opaque resolved keyframed values, and Freestyle
  edge indices with a source-topology hash.

Constraints and drivers are captured through evaluated values. Webtoon applies
captured pose matrices and morph weights to reusable skin/morph resources, but
does not yet expose general-purpose pose or shape-key editing controls.

The add-on uses Blender's embedded glTF/GLB exporter because it carries
textures, vertex colors, skins, morphs, and hierarchy. See the
[Blender glTF 2.0 manual](https://docs.blender.org/manual/en/4.5/addons/import_export/scene_gltf2.html).
Before hashing/publication, a bounded standard-library GLB parser proves unique
`webtoon_uuid` extras for participating objects, data blocks, and materials and
proves the owning object UUID for evaluated resources. External URIs,
executable references, malformed chunks, and duplicate/noncanonical IDs are
rejected. Bone IDs remain sidecar-validated because Blender's exporter does not
provide a proven custom-bone-extras contract; that limitation is emitted as a
visible warning.

Modifier behavior is:

- no modifiers: reusable base geometry;
- compatible armature/morph deformation: reusable skin and morph data;
- ordinary static modifiers: object-scoped evaluated GLB with modifiers
  applied;
- incompatible deformable/topology-changing stacks: per-frame evaluated
  fallback with pose/shape controls disabled for that object;
- Geometry Nodes, simulations, and linked/library-override data: rejected.

Geometry changes mark affected fallback resources in other frames stale while
their last valid caches remain visible. Freestyle marks are preserved but not
drawn in v1.

## Transport and hostile-input validation

The desktop server binds only an ephemeral `127.0.0.1` port and accepts JSON-RPC
POSTs bearing a random per-process token of at least 32 characters. The add-on
accepts only endpoints of the form `http://127.0.0.1:<port>/rpc`. Source
revision/digest watermarks, compare-and-swap chapter revisions, stored receipts,
and transaction hashes provide stale-update rejection and idempotent replay.

Validation completes before publication and rejects wrong file/series/chapter
IDs, future schemas, traversal, absolute/backslash/NUL/reserved paths,
symlinks/reparse points/hardlinks, undeclared or partial files, external GLB
URIs, executable content, bad media signatures, malformed JSON/GLB, duplicate
JSON keys, NaN/infinity, hash mismatch, and size/depth/count overflow. Allowed
payload files are `.json`, `.glb`, `.png`, `.jpg`, `.jpeg`, and `.webp` with
matching declared media types. Limits include 512 MiB per bundle, 256 MiB per
file, 16 MiB per JSON document, 2,048 files, path depth 32, JSON depth 64, and
250,000 JSON values.

## Renderer and layer composition

The dedicated ModernGL 3.3 worker owns the offscreen context and returns
premultiplied transparent `QImage` results tagged by generation. It uses
latest-only scheduling, half-resolution navigation renders, delayed full
refinement, and stale-result suppression after tab/chapter/frame/generation
changes. A missing native context shows the cached in-memory result or an
unavailable placeholder and disables 3D mutation controls.

Implemented renderer features are:

- Perspective, Orthographic, and Equidistant, Equisolid-angle,
  Stereographic, and Orthographic fisheye projections;
- Blender camera shift/sensor-fit conventions, FOV, and ortho height;
- stable per-node glTF hierarchy, exact signed transforms, textures,
  `COLOR_0`, skins, morphs, cameras, lights, and internal OBJ/MTL import;
- Diffuse, Toon, and Unshaded materials with base factor x texture x vertex
  color x drawing tint, Toon ramps, and material/selection silhouettes;
- Sun, Point, Rectangle, and Spot lighting, with at most eight enabled lights
  and four shadow maps plus overflow warnings;
- oriented Sun/Spot/Rectangle shadow cameras and shadow-quality controls;
  point-light metadata/rendering is supported, but omnidirectional point
  shadows are explicitly disabled with a warning rather than approximated by a
  misleading single 2D map;
- neutral floor/shadows, floor grid, adaptive XYZ volume grid, colored axes,
  and anti-aliasing off by default with a 4x MSAA final target when enabled;
- world/local move/rotate/scale and trackball gizmo math, ID/depth selection,
  and surface-aligned cube/cylinder placement.

`CanvasWidget._render_layer` draws the latest image in ordinary layer-stack
order, clips it through the translated closed boundary and inherited masks, then
draws the independent 2D boundary outline (black 4 px by default). Geometry,
floor, and overlays are the only opaque pixels; the surrounding viewport is
transparent, so normal 2D drawing layers can sit above and below a 3D layer.

## 3D mode and tools

**Shapes > 3D Layer** reuses rectangle, ellipse, and free-closed creation. If no
chapter association exists, the workflow first asks for a `.blend` path hint;
cancel leaves no partial layer or frame.

Selecting the 3D layer or a virtual descendant cancels active 2D gestures,
stores exact 2D pan/centered zoom/rotation, sets rotation to zero, fits the 3D
boundary, and routes mouse/wheel/tablet/touch/key input to
`ThreeDViewportController`. Selecting a non-3D entity restores the saved 2D
view exactly. **Edit Boundary** temporarily invokes Shape Edit and returns to
3D mode on commit/cancel.

The toolbar provides Transform Object, Add Light (Sun/Point/Rectangle/Spot),
Draw Cube, Draw Cylinder, Rectangle Select, and Lasso Select. Transform uses
projected per-axis move/rotate/scale handles and trackball rotation in global or
local space while preserving parent transforms and object pivots. Primitive
placement uses the picked surface and normal. Middle drag and wheel retain 3D
pan/dolly; selection claims left-button input, so 2D navigation cannot run in
3D mode.

With Multi Select off, click/region selection resolves to the frontmost hit.
With it on, regions replace, Shift adds, Ctrl removes, and Ctrl wins when both
modifiers are held; cursor icons reflect add/remove. Region selection uses
visible ID/depth pixels rather than projected bounding boxes.

The exact six contextual ribbon pages are:

- **View**: floor grid, volume grid, axes, floor, and overlay visibility plus
  Blender registration/association controls;
- **Rendering**: projection, FOV/ortho scale, shadows, quality, fidelity, and
  4x MSAA;
- **Outline Settings**: intentionally blank while Freestyle data is preserved;
- **Materials**: Blender slots/assignments, source-to-drawing mappings, and
  drawing-material create/rename/delete/shader/tint/texture/vertex-color/Toon
  ramp/outline controls;
- **Object Properties**: sparse exact transforms, Reset to Blender, editable
  local primitive/light/camera fields, and read-only Blender metadata;
- **Tool Settings**: Enable Multi Select, default off.

The virtual outliner exposes collections, objects, lights, cameras, and local
entities below a fixed `Blender` group. Virtual rows may be selected and
visibility-toggled but cannot be renamed, dragged, nested, or copied as assets.
The owning 3D layer remains renameable and reorderable.

## Validation and release boundary

Automated tests cover schema migration/invariants, persistence/recovery/Save As,
cache GC, renderer math/import/resources/GPU paths, clipping/stacking, 3D input
and UI, add-on registration and pure-Python capture/GLB validation, protocol
security, offline replay, conflicts, and monotonic undo. The native benchmark in
`tests/smoke_three_d_performance.py` warms each context, records render submit,
readback, navigation, and transform P50/P95 over three calibration rounds, and
fails any metric more than 10% above a GPU-specific baseline.

The remaining release checks are environmental rather than missing code:

- run install/save/reopen, identity repair/fork, Save and Sync, preview/restore,
  modifier export, and offline/conflict workflows inside real Blender 4.5.5;
- rerun the GPU timing gate on an otherwise controlled native system. Native
  ModernGL correctness passes on the available Intel Iris Xe driver, but that
  machine showed large load/power-state timing swings during the latest gate;
- perform the separate physical stylus/touch/driver check. Offscreen Qt cannot
  validate native hardware arbitration.

## Explicitly deferred or out of scope

- true live synchronization and public add-on/Extension distribution;
- human/character tools and either asset library;
- user-facing OBJ/glTF import and Blender scene/collection creation from
  Webtoon;
- modifier editing, Geometry Nodes, simulations, linked-library data, and
  arbitrary PBR/normal-map shader graphs;
- rendered Freestyle lines and broad drawing-side pose/shape-key editors.

## Implementation map

- Documents/storage: `comic_editor/three_d/documents.py`,
  `comic_editor/three_d/repository.py`, `comic_editor/core/persistence.py`
- Protocol/desktop sync: `comic_editor/three_d/protocol.py`,
  `comic_editor/three_d/sync_server.py`, `comic_editor/ui/three_d_sync.py`
- Renderer/worker: `comic_editor/three_d/renderer/`,
  `comic_editor/three_d/frame_scene.py`, `comic_editor/three_d/render_service.py`
- Desktop UI/compositor: `comic_editor/ui/three_d.py`,
  `comic_editor/ui/main_window.py`, `comic_editor/ui/canvas.py`,
  `comic_editor/ui/tree_model.py`
- Blender add-on: `blender_addon/webtoon_sync/`
