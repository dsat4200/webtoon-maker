# Webtoon Comic Views

This Blender 4.5 LTS extension stores named, geometry-free Comic View state in
the current `.blend` file and publishes a cropped 3D viewport as transparent RGBA to
Webtoon Maker.

## Install

1. Run `build.ps1` from this directory, or run
   `blender --command extension build --source-dir <this folder>`.
2. In Blender, open **Edit → Preferences → Get Extensions**, choose
   **Install from Disk**, and select the generated ZIP.
3. Enable **Webtoon Comic Views** and open the **Comic Views** tab in the 3D
   View sidebar.
4. Start the bridge, then copy its host, port, and token into Webtoon Maker's
   **Blender Views** page.

Use **Copy Logs** in the bridge section when reporting a problem. It copies a
token-redacted diagnostic report containing the Blender/extension versions,
bridge state, active Comic View, published-file status, and recent extension
events and tracebacks.

The server listens only on `127.0.0.1`. Comic View state, thumbnails, and the
latest published-frame path are stored in the `.blend`. Full frames are written
as revisioned PNGs below `%LOCALAPPDATA%\Webtoon Maker\Comic View Frames`.

## Workflow

- **New** performs an initial Save and Render. **Save** captures the active
  camera and view layer, every object/rig control, visibility, collection and
  Local View state, lights, shape keys, modifier flags, and 3D View shading,
  without rendering. It assigns a persistent extension-owned timeline frame
  and automatically keys every animated or view-varying channel there. When a
  channel first differs, Save backfills its older saved values into the older
  Comic View frames; users never need to insert Comic View keys manually.
- **Load** applies the latest Save without rendering. Selecting another Comic
  View automatically loads it; dirty work prompts for Save, Discard, or Cancel.
- **Revert** swaps the latest Save with the one previous Save, loads it, and
  never renders. Press Revert again to swap back. It is disabled until a
  previous Save exists.
- **Render** publishes only the latest saved state, regenerates its thumbnail,
  and advances the revision. Unsaved working changes are restored afterward,
  even when rendering fails. The legacy `webtoon.update_comic_view` operator
  remains an alias for Render.
- **Set Stream Frame** is available in Camera View. It stores the orange crop
  in camera-gate coordinates, permits crops beyond the gate, and derives
  height from the camera gate and crop aspect. Viewport pan/zoom/rotation do
  not change saved output; moving a locked camera remains a scene change.
- **Duplicate** copies saved/rendered state without Revert history. **Delete**
  never deletes scene geometry. Duplicate receives a distinct timeline frame;
  deleted or retired Comic View frames are never reused.
- Set each view's output width from 64–4096 pixels, up to 16 megapixels after
  the derived height is applied. Blender edits do not publish automatically.
- Right-click a supported scalar, enum, color, or short numeric-array property
  and choose **Include in Comic Views** for controls that are not in the
  built-in capture set.

The extension assigns stable custom UUIDs to editable targets. Read-only linked
data uses a library/path/type/name repair identity and is reported as less
robust. Missing targets produce warnings while the rest of a view still loads.
Objects and collections introduced after an older view are hidden when that
view activates; newly introduced subordinate controls on known objects remain
unchanged and prompt you to save the view.

Selecting an already active view is idempotent: view-list, thumbnail, dirty,
and revision refreshes never reapply its saved scene state. A Comic View is
marked dirty when its captured panel state hash changes, but no frame is
published until **Render**. Geometry edits remain shared Blender data and are
not duplicated or restored by Comic View snapshots.

Comic View frames are allocated after the existing animation range. Direct
Actions remain supported and shared Actions are isolated when views require
different values. Existing keys outside Comic View frames are retained. A
conflicting camera/property driver, active NLA evaluation, or linked read-only
Action produces a channel-specific error instead of silently loading the wrong
state. Driver-produced rig deformation channels are recomputed from the baked
controller pose rather than keyed as controls.
Blender marks collection viewport/render visibility as non-animatable, so those
flags remain per-view snapshot values applied immediately after selecting the
view's timeline frame; they do not receive Actions or FCurves.

The `.blend`-stored **Hide/Show Stream Frame Overlay** button controls only the
orange camera-relative frame. The add-on preference **Always Hide Overlays**
temporarily disables Blender overlays for offscreen output and restores the
viewport setting after success or failure.

Render encodes a complete PNG, flushes a temporary file, and atomically renames
it before announcing the updated view over the token-authenticated loopback
control protocol. The editor validates and embeds those original PNG bytes.
The newest two revisions are retained for each view. Rendering while the editor
is disconnected is supported; the newest PNG is imported on reconnect.

If rendering reports that no 3D View or GPU context is available, open a normal
3D View and press **Render**. Stopping Blender does not blank linked images in
the editor.
