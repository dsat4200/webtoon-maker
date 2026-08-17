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

The server listens only on `127.0.0.1`. Comic View state and thumbnails are
stored in the `.blend`; full streamed frames are transient shared memory.

## Workflow

- **New** captures the active camera, current view layer, object/rig state,
  visibility, collections, lights, shape keys, modifier enable flags, and the
  active 3D View shading configuration. It also renders a packed thumbnail.
- **Set Stream Frame** lets you drag the orange output rectangle anywhere in
  the bound 3D View, including beyond the camera border. Its width setting is
  fixed and its height follows the rectangle's screen aspect ratio.
- **Update** replaces that snapshot, thumbnail, viewport framing, and output
  resolution, increments its revision, and publishes one durable full frame. **Revert**
  reapplies the stored snapshot. **Duplicate** creates a new UUID and copied
  state; **Delete** never deletes scene geometry.
- Set each view's stream width from 64–4096 pixels, up to 16 megapixels after
  the derived height is applied. Blender edits do not publish automatically.
  The editor's **Render Once** requests an immediate, nonpersistent preview.
- Right-click a supported scalar, enum, color, or short numeric-array property
  and choose **Include in Comic Views** for controls that are not in the
  built-in capture set.

The extension assigns stable custom UUIDs to editable targets. Read-only linked
data uses a library/path/type/name repair identity and is reported as less
robust. Missing targets produce warnings while the rest of a view still loads.
Objects and collections introduced after an older view are hidden when that
view activates; newly introduced subordinate controls on known objects remain
unchanged and prompt you to update the view.

Selecting an already active view is idempotent: view-list, thumbnail, dirty,
and revision refreshes never reapply its saved scene state. A Comic View is
marked dirty when its captured panel state hash changes, but no frame is
published until **Update**. Geometry edits still affect the next Update or
Render Once without storing geometry in the snapshot.
Switching away from a dirty view requires Update, Revert, or Cancel.

Pixel transport is top-down straight-alpha RGBA8 in a Blender-created,
triple-buffered shared-memory block. Control messages use a token-authenticated
newline-delimited JSON protocol on loopback. Blender never overwrites a slot
until the editor acknowledges it; if all slots are occupied, intermediate work
is dropped and the newest dirty state is rendered next.

If rendering reports that no 3D View or GPU context is available, open a
normal 3D View and use **Render Once** after the
editor reconnects. Stopping Blender does not blank linked images in the editor.
