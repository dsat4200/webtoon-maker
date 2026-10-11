# Appearance and window chrome

Open **Settings → Themes** to choose Light, Dark, Botanesque, Blender,
Crona, Kaito, Len, Miku Light, Miku Dark, Shadow, or Unit 01.
The existing tools, ribbon, outliner, navigator and color workspace retain
their arrangement. Window controls share the Project toolbar, which supports
dragging, double-click maximize/restore and edge resizing.

Edit the palette swatches to recolor window surfaces, panels, inputs, buttons,
text/icons, borders, selections, accents and the canvas surround. Hue,
Saturation, Brightness and Contrast transform the base palette together.
They affect application chrome only; drawing colors, images, document settings,
native rendering and memory/disk artwork caches are independent.

Save a built-in variation with **Save as…**. Saved custom themes support
**Save**, **Rename**, **Delete**, **Export** and **Import**. Export includes the
base colors and slider values in a versioned JSON file. Import assigns a new
ID and resolves name collisions without overwriting another theme. Changes
in Settings persist when **OK** is pressed; **Cancel** restores the previous
theme and discards changes in the dialog. Built-in palettes remain available.

Per-user `themes.json` sits beside `settings.json` in Qt's application config
directory and uses the existing atomic file writer. It is separate from series
and chapter storage. Malformed local entries are skipped; import rejects
invalid colors, unknown formats and unsupported versions. No imported code,
stylesheets or paths are evaluated.

## Palette references

The ten controller palettes come from the user's
[Wii Balance Board controller project](https://github.com/dsat4200/Wii-Balance-Board-XInput-Controller/tree/51757d3da7be256a41f07c7ca380af675fb93935/themes).
Their source surface, control, selection, text and accent colors are adapted
to Webtoon Maker's semantic roles. Layout and typography use this editor's
existing dimensions.

Botanesque uses scalar colors from the user's installed Blender Botanesque
1.1.0 preset by polygoniq, published under GPL-3.0-or-later. Brown/taupe
surfaces, cream text, olive controls and green selections reproduce the
provided reference. No Blender code or theme XML is bundled.

The original page-and-pencil mascot and Windows icon are in
`comic_editor/ui/assets/`. Rebuild the launcher with `build-launcher.ps1` to
embed the ICO; the running Qt application also uses it.
