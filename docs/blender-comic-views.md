# Blender Comic Views prototype architecture

## Boundary

Blender owns geometry, materials, rigs, scene evaluation, Comic View snapshots,
thumbnails, viewport rendering, and revisioned publication PNGs. Webtoon Maker
owns an ordinary Image Object, its placement and transform, masks, opacity,
hierarchy, compositing, and the last accepted PNG. No drawing or transform
operation is sent to Blender.

```text
Blender scene → saved Comic View → GPUOffScreen RGBA → atomic revision.png
                                                            │
                                              JSON metadata notification
                                                            │
                                                            ▼
                                       ImageStore → normal ImageObject compositor
```

## Blender state and publication

State format version 3 captures the active camera and view layer, object and
pose-control transforms, visibility, rig properties, shape keys, camera/light
values, modifier flags, viewport shading, registered RNA properties, Local
View membership, camera-gate aspect, the camera-relative Stream Frame, and
derived output resolution. It contains no mesh, texture, or evaluated geometry.

Dependency-graph changes affect only the working-state dirty check. **Save**
captures state without rendering and automatically bakes its animated or
view-varying channels at a persistent, extension-owned timeline frame. When a
channel first differs between views, all older views are backfilled from their
stored snapshots. **Load** selects that frame and applies non-keyframable state.
**Revert** swaps current and previous Saves and rebakes the owned frame without
rendering. **Render** transactionally captures working state, applies the
current baked Save, renders exactly once, and restores the original frame,
subframe, working transforms, and viewport navigation in `finally`.

Timeline frames are allocated after the scene range, existing Action keys, and
all earlier Comic View frames. The allocator never reuses retired frames.
Direct writable Actions are extended without changing existing keys; shared
Actions are copied per owner when necessary. Snapshot transitions are constant.
Animation is accessed through the owner's assigned Action slot and its
channelbag, supported by both Blender 4.5 and 5.2. Frame allocation scans all
slots, including unassigned Actions, to avoid colliding with existing keys.
Copying a shared Action preserves the slot binding; rollback restores that
binding and removes only animation data created by the failed transaction.
The internal bake marker advances from 1 to 2. Views marked 1 receive fresh
frames after the complete animation range on their first bake, since their old
frames might overlap user keys in a previously unscanned slot. Old keys remain
untouched; version-2 frame assignments stay stable. This is independent of the
unchanged snapshot format and protocol version.
Legacy views are allocated and backfilled from their stored JSON on first use,
so current timeline evaluation never replaces their saved pose. Conflicting
camera/property drivers, active NLA evaluation, and linked read-only Actions
fail with a channel-specific message. Driver-produced rig deformation channels
are left to evaluate from their baked pose controls.
Channels that Blender explicitly marks non-animatable, including collection
viewport/render visibility, remain verified snapshot values and are applied
after the owned frame is selected rather than receiving invalid FCurves.

Save uses operation-local indexes for object/bone/modifier identity, RNA
metadata, Action-slot curves, drivers, and per-curve key styles. It flattens each
snapshot once per bake and groups its channels for backfill. Existing version-2
frames skip the global animation-range scan; new and migrated frames still
scan all slots and NLA ranges. Key-style restoration visits only the affected
curve, avoiding a quadratic scan across every earlier key. These indexes do
not survive the operation, so edits and Undo cannot leave cached RNA pointers.
Identical transform assignments are skipped, and duplicate-ID repair parses
saved snapshots only when duplicate identities are actually present.

A successful Render encodes the full frame as PNG and writes it beneath:

```text
%LOCALAPPDATA%\Webtoon Maker\Comic View Frames\
  <project UUID>\<view UUID>\<revision>.png
```

The PNG is written to a temporary sibling, flushed, and atomically renamed.
Only then do the view revision, thumbnail, published dimensions, and frame path
advance. The newest two revisions are retained. A rendering, encoding, or file
error leaves the previous publication metadata and image intact. Duplicate
views receive their own copy; deletion performs best-effort cleanup constrained
to that view's managed directory.

Thumbnail publication builds a new packed image and explicitly sets both its
custom image preview and custom icon pixels before swapping the view's image
reference. UI draws only read the prepared previews. Packed thumbnails use a
file image source so they survive reopening; legacy generated thumbnails are
repaired during initialization. Replaced images are removed only when no view
or other Blender user retains them. Successful publication redraws the sidebar.

The orange `POST_PIXEL` overlay maps camera-gate coordinates to screen
coordinates and is editable only in Camera View. `GPUOffScreen` uses the saved
camera matrices plus crop projection; ordinary viewport navigation cannot
affect output. Width is user-controlled while height follows crop aspect.

## Protocol version 3

The provider binds only `127.0.0.1`, accepts one authenticated editor, and
queues Blender RNA/GPU work to its persistent main-thread timer. Control
messages are newline-delimited JSON objects capped at 4 MiB.

- Editor → Blender: `HELLO`, `PING`, `GET_VIEWS`, `GET_THUMBNAIL`,
  `ACTIVATE_VIEW`, and `RESOLVE_DIRTY`.
- Blender → editor: `HELLO`, `PONG`, `VIEWS_CHANGED`, `THUMBNAIL`,
  `ACTIVE_VIEW`, `SWITCH_REQUIRES_DECISION`, `SWITCH_CANCELED`, and `ERROR`.

`VIEWS_CHANGED` includes the canonical project/view UUIDs, revision, published
dimensions, dirty flag, and absolute `frame_path`. An empty path means that a
legacy or new view needs one Render. `RESOLVE_DIRTY` uses `save`, `discard`, or
`cancel`; legacy aliases remain accepted. A protocol mismatch produces a
specific coordinated-upgrade error.

Extension 0.6.0 adds optional `extension_version`, `blender_version`, and
`capabilities` (`layered_actions`, `published_png`) to the provider `HELLO`.
These values are captured before starting socket workers so the network thread
does not read Blender RNA. Older protocol-3 peers can omit or ignore these
fields. The editor displays reported versions, preserves error guidance on
disconnect, and permits retrying a failed view activation.

There is no pixel data in the socket protocol, shared memory, stream lifecycle,
preview frame, slot acknowledgement, or filesystem watcher. Rendering while
the editor is disconnected still publishes the PNG; `GET_VIEWS` reconciles it
on the next connection.

## Editor lifecycle

The editor validates that a publication path is absolute and ends in `.png`,
limits compressed input to 128 MiB, verifies the PNG format and advertised
dimensions, and reads each publication once per session key. Accepted original
bytes go directly into `ImageStore`; there is no transient image override or
second PNG encode.

Every matching linked object in the active chapter updates without entering
Undo. Free-transform instances keep their displayed width and center while the
quad's vertical half-edges follow a new aspect. The source revision advances
only after validation succeeds, and the chapter becomes dirty so normal save
and autosave persistence store the imported bytes.

Missing, corrupt, stale, oversized, or inaccessible publications leave the
last-good embedded frame unchanged. Existing projects keep their current cache.
Existing `.blend` views with no full publication require one new Render.
Relink chooses a UUID explicitly; Detach, Rasterize, and Copy as Asset freeze
the accepted cache as a normal embedded image.

The Blender panel's **Copy Logs** action places a token-redacted diagnostic
report on the clipboard. It includes versions, bridge and active-view state,
published-file availability, the latest render/publication events, and captured
extension tracebacks. Diagnostics are held in memory for the Blender session.

## Prototype limits

One Blender 5.2 or 4.5 LTS process, one Webtoon Maker process, and one authenticated
connection are supported. Output is the saved cropped viewport with captured
shading and transparency, not a final Cycles/Eevee render. Unsaved-scene
previews, geometry transfer, editor-to-Blender transforms, and continuous
rendering are out of scope.

## Compatibility verification

`tests/test_blender_extension.py` runs against installed Blender 5.2 and 4.5,
or paths provided in `BLENDER_52_EXECUTABLE`, `BLENDER_45_EXECUTABLE`, or
`BLENDER_EXECUTABLE`. It covers state save/load/revert, animation isolation and
rollback, publication, authenticated protocol messages, and ZIP installation
with isolated Blender preferences. Set `WEBTOON_BLENDER_LIVE_TEST=1` to also
exercise GPU readback, camera crop rendering, Local View, and overlay restoration.

The migration follows the official [Blender 5.0 Action API changes](https://developer.blender.org/docs/release_notes/5.0/python_api/).

### 0.6.1 performance check

A local Blender 5.2.1 benchmark used the same saved scene (1,404 objects,
1,304 Actions, eight views) in separate processes, without writing the blend.
The edited Save changed the camera lens after the initial Save. Timings include
capture, baking, scene restoration, and verification, with lightweight function
timers enabled in both versions.

| Operation | 0.6.0 | 0.6.1 |
| --- | ---: | ---: |
| Initial Save with old-frame migration | 63.62 s | 4.70 s |
| Save after a camera edit | 72.97 s | 4.86 s |
| Save unchanged view | 1.70 s | 1.02 s |
| Load another view | 1.13 s | 0.75 s |

The bookkeeping regression also checks work growth rather than fragile timing
thresholds: quadrupling the channels requires about four times the RNA access,
while preserving keys, backfill, and rollback. Thumbnail probes cover both
preview sizes, replacement failure, duplicate isolation, save/reopen, and real
GUI icon IDs on both supported Blender versions.
