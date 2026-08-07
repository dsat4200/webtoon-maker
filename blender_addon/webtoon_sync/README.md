# Webtoon Maker Comic Frame Sync

This is a private, install-from-disk Blender add-on targeting Blender **4.5.5
LTS**. Zip the `webtoon_sync` folder itself, then use Blender's add-on installer.
The add-on has no third-party Python dependencies.

In Webtoon Maker, use **Copy Add-on Connection Settings** for the active chapter.
Then open Blender's 3D Viewport sidebar, Webtoon tab, and click **Paste / Import
Webtoon Registration**. The add-on validates the clipboard JSON, loopback-only
endpoint, one-process bearer token, chapter/inbox relationship, source identity,
and available frame IDs before changing its settings. The token and endpoint use
Blender's `SKIP_SAVE` option and therefore remain session-only.

Choose a registered frame from the **Comic Frame** dropdown and click **Apply**.
This changes the active add-on frame and restores that frame's explicit
participating-collection checks from its Webtoon sidecar. Newly discovered
collections remain unchecked unless the selected frame explicitly included them.
The raw series/chapter/path fields remain visible for diagnosis, but normal setup
does not require copying them one by one.

`Update Comic Frame` validates stable IDs, restores an active preview, saves the
named `.blend`, exports an embedded GLB cache, captures typed frame metadata, and
atomically renames a hashed transaction to `<uuid>.ready`. The add-on then sends
an authenticated JSON-RPC notification. If Webtoon Maker is closed or cannot be
reached, the ready bundle remains in `blender/inbox` and the UI reports **queued,
not accepted**.

Before publication, every staged GLB is parsed as a bounded GLB 2.0 container
using Blender's standard Python library. External URI and executable references
are rejected. The reusable base export must contain one unambiguous
`webtoon_uuid` extra for every participating object, data block, and material;
evaluated resources must retain their owning object UUID. Blender's exporter
does not provide a proven custom-bone-extras contract, so bone UUIDs are checked
in typed sidecar capture and reported explicitly rather than claimed as a GLB
invariant.

The v1 sync intentionally rejects linked-library data, Geometry Nodes, and
simulation modifiers. Ordinary static modifiers use evaluated export. Mixed
armature/morph topology stacks receive a per-frame baked fallback and cannot be
posed from Webtoon for that fallback revision.

`Apply Comic Frame Preview` changes only the current Blender session. It inserts
no keyframes and never saves. `Restore Blender State` replays the in-memory
snapshot; `Update Comic Frame` always restores a preview before capture.
