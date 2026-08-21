# Blender Comic Views prototype architecture

## Boundary

Blender owns geometry, materials, rigs, scene evaluation, Comic View snapshots,
thumbnails, and viewport rendering. Webtoon Maker owns an ordinary Image Object,
its placement and transform, masks, opacity, hierarchy, compositing, and the
last accepted PNG. No drawing or transform operation is sent to Blender.

```text
Blender scene → Comic View state → GPUOffScreen RGBA
                                      │
                         shared memory + JSON control
                                      │
                                      ▼
ImageStore runtime override → ImageObject → normal canvas compositor
             │
             └── debounced last-frame.png for offline reopening
```

## Blender state

State format version 3 captures the active camera and view layer, object and
pose-control transforms, object/collection/layer-collection visibility, numeric
rig custom properties, shape keys, camera/light values, modifier enable flags,
viewport shading, explicitly registered RNA properties, active layer
collection, Local View membership, camera-gate render aspect, a camera-relative
Stream Frame, and derived output resolution. It contains no mesh, curve,
texture, or evaluated geometry. Version-1/2 viewport-relative frames migrate
through their saved viewport and camera gate when possible, otherwise to the
full camera gate with a warning.

Local Blender targets receive a `webtoon_comic_uuid` custom property. Capture
repairs duplicated IDs while preferring a target known by an existing view,
then the older Blender session identity. Read-only linked data uses a hash of
library, owner, RNA type/path, and name and emits a robustness warning.

Applying a view first restores collection and visibility state. It then applies
object transforms, rig controls, shape keys, modifiers/registered values,
camera/light data, the active camera, viewport shading, and dependency-graph
evaluation. Missing targets warn without aborting. Objects/collections not in
an older snapshot are hidden; new subordinate controls on known objects remain
unchanged and warn that the view should be saved.

Dependency-graph changes affect only the working-state dirty check. **Save**
captures state without rendering and rotates a changed prior Save into one
backup slot. **Load** applies the current Save. **Render** transactionally
captures working state, applies and publishes the current Save, then restores
working state and viewport navigation in `finally`; only success advances the
revision, thumbnail, and published dimensions. **Revert** swaps current and
previous Saves and loads the restored state without rendering. `RENDER_ONCE`
emits a temporary working-state preview without changing saved state,
thumbnail, or revision. Row selection loads automatically, with Save and
Switch, Discard and Switch, or Cancel for dirty work.

The active view's orange `POST_PIXEL` overlay maps camera-gate coordinates to
screen coordinates on each draw and is visible/editable only in Camera View.
Coordinates may extend beyond `0–1`. `GPUOffScreen` uses the saved camera's
inverse world matrix and camera projection plus the crop projection; ordinary
viewport navigation is absent from the snapshot and cannot affect output.
Width is user-controlled; height follows camera-gate and crop aspect. Local
View is restored in the bound 3D View before rendering. The `.blend` overlay
toggle controls the orange frame, while **Always Hide Overlays** temporarily
suppresses Blender overlays during all offscreen output and always restores
the prior viewport value.

## Protocol version 2

The provider binds only `127.0.0.1`, accepts one authenticated editor, and
queues all Blender RNA/GPU work to its persistent main-thread timer. Control
messages are newline-delimited JSON objects capped at 4 MiB. The supported
message types are:

- editor → Blender: `HELLO`, `PING`, `GET_VIEWS`, `GET_THUMBNAIL`,
  `ACTIVATE_VIEW`, `RESOLVE_DIRTY`, `START_STREAM`, `STOP_STREAM`,
  `RENDER_ONCE`, and `FRAME_CONSUMED`;
- Blender → editor: `HELLO`, `PONG`, `VIEWS_CHANGED`, `THUMBNAIL`,
  `ACTIVE_VIEW`, `SWITCH_REQUIRES_DECISION`, `SWITCH_CANCELED`,
  `STREAM_OPEN`, `FRAME_READY`, `STREAM_STATUS`, and `ERROR`.

`RESOLVE_DIRTY` uses `save`, `discard`, or `cancel`; legacy `update` and
`revert` values remain accepted as aliases. View and project identities are
canonical UUID hex strings. Resolution is
64–4096 per axis and no more than 16,777,216 pixels. Revisions and frame
sequences are monotonic non-negative integers. The editor rejects mismatched,
stale, duplicate, malformed, or out-of-order data.

`STREAM_OPEN` names a Blender-created shared-memory block and declares a
validated `frame_kind` of `committed` or `preview`; `FRAME_READY` repeats that
kind. Its first 256 bytes
contain the little-endian header `<8sIIIIII32s>`: magic
`WCVRGBA\0`, protocol, width, height, stride, three slots, bytes per slot, and a
stream nonce. Three fixed slots follow. Pixels are top-down straight-alpha
RGBA8. Blender writes only a free slot and sends `FRAME_READY`; the editor
copies it immediately into owned premultiplied `QImage` memory and sends
`FRAME_CONSUMED`. If all slots are outstanding, Blender preserves one dirty
flag and renders the latest scene state when a slot becomes free.

## Editor lifecycle

The Blender Views ribbon stores host/port/token only in editor preferences. A
chapter stores the provider project/view UUIDs, display hint, last accepted
revision, and cached image bytes. Selection owns the sole stream: selecting a
linked Image Object activates its view; selecting anything else, changing
tabs, minimizing, disconnecting, or closing stops it.

Incoming preview frames update every matching instance through runtime-only
image and geometry overrides. They never encode PNG data, advance revisions,
dirty the chapter, or enter Undo, and both overrides disappear when preview
ownership ends. Committed frames update the last-good PNG and revision without
an Undo command. If their aspect changes, each free instance keeps its
displayed width and center while the quad's vertical half-edges scale to the
new aspect. Image transforms render through normal hierarchy traversal, so
cached raster/vector artwork above them remains visible throughout the drag.

Unavailable views remain visible from cache. Relink explicitly chooses a new
UUID; names never relink automatically. Detach, Rasterize, and Copy as Asset
freeze the cache as an embedded image and keep the editor transform.

## Prototype limits

One Blender 4.5 LTS process, one Webtoon Maker process, one authenticated
connection, and one active stream are supported. Output is the saved cropped
viewport with captured shading and transparent background, not a final
Cycles/Eevee render. Draw-over attachment, filters, crops, continuous multiple
streams, geometry transfer, and editor-to-Blender transforms are out of scope.
