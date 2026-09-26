# SUT input dynamics

The importer recognizes two bounded, big-endian dynamics layouts. Their first eleven 32-bit words have the same positions. Word 1 is header length (44 or 48 bytes); words 3–7 contain enabled inputs and pressure/tilt/speed/random minima. Words 9 and 10 specify the pressure and tilt graph byte lengths. Word 11 is the physical tilt maximum in percent. A 48-byte header adds the velocity graph length as word 12. The graphs follow the complete header in pressure, tilt, velocity order.

Each graph has a 12-byte header `(12, point_count, 16)` followed by that many big-endian double-precision `(x, y)` pairs. The importer bounds point counts to 256 and checks exact payload length and finite coordinates. Empty graph fields retain default response curves. Exact duplicate knots are normalized on import, so opening settings does not change a saved definition.

Unknown header sizes are reported. An unrecognized extension value is retained in the source metadata and reported rather than interpreted as a graph offset. Valid graph lengths must fit exactly; truncated graphs, invalid counts and extra trailing bytes are reported. All original source bytes remain available in `source.variant` even when a response cannot be interpreted.

The `BrushDynamics.tilt_maximum` ratio defaults to 1 for existing presets and supports physical enlargement up to 10. Tilt uses `minimum + (maximum - minimum) * curve(input)`, then combines with the other enabled inputs. The final factor is allowed to exceed 1 up to the tilt maximum. Settings show this maximum in percent and preserve source precision until it is edited. Color-input maximum scaling remains uncalibrated and retains an explicit import warning; negative color minima continue to use their separate signed range.

The official [tool setting guide](https://vd.clipstudio.net/clipcontent/paint/manual/EN_CSP_toolguide.pdf), Dynamics settings, page 164, describes tilt enlargement beyond the original brush size. The current [brush customization manual](https://help.clip-studio.com/en-us/manual_en/240_brushes/Customizing_brush_tools.htm) documents independent pressure, tilt, velocity and random inputs with minimum and maximum controls. Raw device tilt normalization and speed calibration still use the independent engine's existing conventions; these field mappings do not establish numerical CSP parity.

Local source checks, September 26, 2026:

- SRU karikari line pen, authoring tool 5.1.2 / ManagerVersion 145: 48-byte size header, 60-byte three-knot pressure graph, no velocity graph. Its response was previously skipped because only 44-byte headers were accepted.
- Leaf 2: size tilt minimum 100%, maximum 300%, with an intentionally descending curve. It ranges from 3× at upright input to 1× at full tilt; the curve is retained as authored.
- Sketching / Pencil Brush: size tilt minimum 4%, maximum 257%, ascending curve. Pressure still multiplies that response.
- Bibibi 2306072: size and density both have 48-byte headers plus a 44-byte two-knot velocity graph. Their enabled speed response minima are 60% and 30% respectively.
- Hazy 2308021 and most II2 Watercolor 2307218 opacity records also store three graphs. Velocity is disabled in the observed opacity records, but its graph is preserved independently of the active pressure response.

Reconstructed third-party source fixtures remain local under `.artifacts`; they are not distributed in the repository. Portable synthetic regressions cover both header lengths, signed minima, independent graphs, malformed boundaries, extension warnings, sampling and settings round trips. Local-only regressions additionally compare the installed source fixtures when present.
