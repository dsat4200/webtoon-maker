# Webtoon Comic Views

This Blender 5.2 / 4.5 LTS extension stores named, geometry-free Comic View state in
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

### Textures, materials, and UV guides (0.8.0)

Install `webtoon_comic_views-0.8.0.zip` using **Install from Disk**, then restart
Blender when you have saved your work. Existing Comic Views and the configured
external image editor continue to work.

Open **Comic Views → Textures** in the 3D View's N sidebar:

- Enter **Name** and select **Prefix**, **Suffix**, or **Full Name**. Prefix and
  suffix are applied to the active object's name, or the blend filename when no
  object is active. Include separators such as `_` in the name text. The filename
  preview updates immediately. Existing images, files, and Webtoon Maker project
  folders are preserved by adding `.001`, `.002`, and so on when necessary.
- Expand **Texture Creation Settings** for width, height, blank/UV-grid/color-grid
  fill, color, and alpha. The default is a transparent 1024 × 1024 PNG, with up
  to 8192 pixels in each dimension and the resulting megapixel count displayed.
- By default, textures are saved directly in **tex** beside the saved `.blend`;
  **Create Texture** creates that directory. Enable **Use Custom Directory** to
  enter another path. **Save File Settings → Choose Texture Directory** opens a
  directory picker and enables the custom path. A custom absolute directory also
  works before the `.blend` has been saved.
- Expand **Material Settings** and select a **Material Slot** on the active
  object. Slot 1 is the default. **Create Texture** applies the supplied comic
  shader to that slot, names the material after the texture, and assigns the
  image. An existing copy of the preset is updated in place, retaining its
  shader edits. Other slots and objects keep their materials, including linked
  duplicates. A shared preset becomes a local copy when edited.
- **Transparency** controls the final Mix Shader factor (0 opaque, 1 transparent),
  **Color** controls the preset's Color node, and **Metallic** and **Roughness**
  control the corresponding Principled BSDF inputs. Set these before creating a
  texture, or change them live on an existing preset. Each selected slot reads
  its actual node values, including changes made in the Shader Editor. The
  supplied vertex-color, RGB ramps, and texture multiplication graph is retained;
  its Shader to RGB shading is intended for Eevee.
- **Create Texture** creates and saves the PNG, applies the material, then calls Blender's existing
  **Edit Externally** operation with that file. Keep Webtoon Maker configured in
  Blender's **Preferences → File Paths → Applications → Image Editor**. A failed
  launch leaves the saved texture available for **Edit Texture Externally** to
  retry.
- **Include UV Map Layer** sends the active mesh object's active UV map as a
  separate guide for Webtoon Maker. It reads current UVs in both Object and Edit
  modes and does not change mode, geometry, materials, or selections. An object
  without UVs simply opens the texture without a guide. Existing images can be
  selected in the **Texture** field and opened with **Edit Texture Externally**,
  or opened from the Image Editor's **Image → Edit in Webtoon Maker (with UV Map)**.
  Blender's ordinary **Edit Externally** command is unchanged.
- After saving changes in Webtoon Maker, press **Reload Texture** to refresh the
  selected texture from disk and update its material users. Reload keeps the
  existing image, material, and UV guide. A confirmation appears if Blender has
  unsaved image edits; packed images and missing files report an error.

Texture settings, per-object material slot choices, shader values, and the
selected texture are stored in the `.blend`. New image
datablocks have a fake user so they remain available after reopening the file.
PNG generation and encoding use Blender's native image implementation instead
of allocating a Python list for every pixel. UV edges are deduplicated and
limited to 200,000; larger maps report a clear error while keeping the saved
texture, allowing a retry with a simplified map or the guide disabled.

The UV handoff is a small JSON sidecar beside the PNG, for example
`tex/Hero.png.webtoon.json`. Its schema is `webtoon.texture.v1`, its `image` field
matches the image filename, and `uv_overlay` contains `name`, `width`, `height`,
and `segments` of `[u1, v1, u2, v2]` values. Coordinates use Blender's normalized
UV convention, with V increasing upward. The sidecar is published atomically
before opening the image; no full-size temporary UV bitmap is needed.

### Upgrading to 0.6.1

Save your `.blend`, install `webtoon_comic_views-0.6.1.zip` with **Install from
Disk**, then restart Blender and Webtoon Maker. Existing views and their saved
states need no manual conversion. The extension uses Action slots and
channelbags on both supported versions, fixing the `Action.fcurves` error on
Blender 5.2. Shared Actions keep each owner's assigned slot, and failed Saves
restore the prior Action, slot, and animation keys. Protocol 3 and the saved
Comic View format remain unchanged.

On the first Save or Load, views baked by 0.5.1 receive new timeline frames
after all existing animation. The previous keys are retained: older versions
could miss animation in another Action slot when choosing a frame. Subsequent
Saves keep the new frame numbers. Large scenes may take longer on this first
operation while their saved views are rebaked.

Version 0.6.1 speeds up saving large scenes by indexing rigs, channels, and key
styles once per operation. A regular Save keeps its existing frame and skips
the full animation-range scan. These lookups are discarded after the operation,
so subsequent scene edits, Undo, and changed Actions are read afresh.
**Copy Logs** includes Save timings to help diagnose any remaining slow scenes.

`build.ps1` prefers installed Blender 5.2, then 4.5. Set `-BlenderExecutable`
or `BLENDER_EXECUTABLE` to use a custom installation. Package validation must
succeed before the ZIP is built.

### Saving and rendering

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
  Both the list icon and the larger Blender preview update on success and are
  retained when reopening the `.blend`. Duplicated views have independent
  thumbnails, so rendering a duplicate does not change the original's preview.
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
