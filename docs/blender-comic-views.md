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

State format version 2 captures the active camera and view layer, object and
pose-control transforms, object/collection/layer-collection visibility, numeric
rig custom properties, shape keys, camera/light values, modifier enable flags,
viewport shading, explicitly registered RNA properties, the bound viewport
pose, normalized screen-space Stream Frame, and derived output resolution. It
contains no mesh, curve, texture, or evaluated geometry. Version-1 snapshots
migrate using the visible camera rectangle when available, with a centered 80%
frame as fallback.

Local Blender targets receive a `webtoon_comic_uuid` custom property. Capture
repairs duplicated IDs while preferring a target known by an existing view,
then the older Blender session identity. Read-only linked data uses a hash of
library, owner, RNA type/path, and name and emits a robustness warning.

Applying a view first restores collection and visibility state. It then applies
object transforms, rig controls, shape keys, modifiers/registered values,
camera/light data, the active camera, viewport shading, and dependency-graph
evaluation. Missing targets warn without aborting. Objects/collections not in
an older snapshot are hidden; new subordinate controls on known objects remain
unchanged and warn that the view should be updated.

Dependency-graph changes update only the working-state dirty check. They do not
publish pixels and never reapply the stored snapshot. **Update** captures the
working scene, viewport, frame, and resolution; increments the revision;
regenerates the thumbnail; and emits one committed full frame. `RENDER_ONCE`
emits a temporary preview without changing saved state, dirty status,
thumbnail, or revision. Switching away from a dirty view still requires
Update, Revert, or Cancel, and Update-before-switch waits for the outgoing
committed frame acknowledgement.

The active view's orange `POST_PIXEL` overlay marks the exact screen crop.
**Set Stream Frame** is a clamped modal marquee. `GPUOffScreen` renders from
the bound `RegionView3D` matrices with a crop projection, so output may extend
beyond the active camera. Overlay drawing is not included in the framebuffer.
Width is user-controlled; height follows the frame's on-screen aspect ratio.

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

View and project identities are canonical UUID hex strings. Resolution is
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
