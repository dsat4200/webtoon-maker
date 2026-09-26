# Smudge native verification — 27 September 2026

The new Smudge controls, editable stroke handles and project tool presets work in an isolated native OpenGL canvas with the real modifier panel. Finished native pixels match a fresh synchronous render exactly. Editing the last of three strokes is faster than editing the first because earlier finished stroke results can be reused. Exact release still causes a visible pause; these results do not establish 60 FPS interaction.

## Measured interaction

The synthetic reference is a 1024 × 1024 opaque color-strip image with three curved strokes. The real canvas receives 60 pointer moves per second without waiting for each move to paint. The 248-event run includes first-stroke and last-stroke handle drags, the selected point's left-side Flow slider, and warmed navigation. All events were delivered. This measures the **isolated native canvas plus modifier panel**, not MainWindow or an existing comic project.

| Operation | Preview paint median / p95 | Release to finished exact image |
| --- | ---: | ---: |
| First stroke's handle | 56.2 / 70.2 ms | 444.0 ms |
| Last stroke's handle | 26.5 / 32.4 ms | 182.1 ms |
| Last point's Flow gizmo | 26.9 / 31.5 ms | 181.8 ms |

Warmed navigation took 7.6 ms median and 9.0 ms p95 per paint with **zero Smudge effect calls**. Across all input events, queue delay was 48.0 ms p95 and 61.8 ms maximum. A separate 20 ms event-loop heartbeat reached 446.5 ms during the first stroke's exact release. Paint duration, queue delay and heartbeat gaps are different measurements.

The first-stroke drag produced 17 preview paints; the last-stroke and Flow drags each produced 34. Intermediate pointer events can be coalesced into one display update, while the final endpoint is preserved. No finished projection accepts a reduced-resolution preview.

## Correctness and visible UI

- The nonblank finished native framebuffer and a synchronous rebuild after clearing projection, retained effect, source and preparation caches differ by **zero bytes**. No worker or frame remains pending and no rendering error is reported.
- Real pen events create a fourth two-point stroke with varying pressure: 0.2 → 0.5 → 0.9, retaining the last pressure at release.
- The actual Opacity widget is set to 50%. The rendered result, selected Bézier controls, left-side point controls and all independent pressure-channel controls were visually inspected.
- Saving and reloading a named tool preset restores only future-stroke settings. Existing strokes and opacity stay unchanged. The temporary project's series and chapter both reload correctly.
- The UI's focused tests verify one-step undo, separate endpoint values and channel curves, cancellation across chapter replacement, save failure rollback, modal project-switch protection, and deletion even outside modifier mode. The existing narrow vertical ribbon layout also fits.

Visible repeated brush-stamp texture remains around sharp color transitions. An alternative that swept the brush footprint between dabs was assessed and discarded because it did not clearly improve the image and increased native rendering time. The timings also exclude cold startup and large real-project stacks.

## Artifacts and isolation

Harness: `tests/benchmark_smudge_interaction.py`. Authoritative timing and native exact comparison: `.artifacts/smudge-20260927/native-final/{summary,events}.json`, `finished.png`, `oracle.png`. Authoritative visual UI evidence: `native-visual-final/window-opacity-50.png` and `window-preset-loaded.png`; their canvas-only native captures are saved alongside them. `native-visual-final/summary.json` contains the repeated nonblank exact-oracle and persistence checks. Production source hashes match before and after each accepted run.

The full-window visual combines the actual native framebuffer with the live Qt panel capture: hidden `QWidget.grab()` omits and clears its OpenGL child. Capture order is explicit, and nonblank/source-color assertions prevent accepting an empty framebuffer. The earlier `native-final` before/opacity/window captures were affected by that Qt capture issue and are excluded; its separately captured finished/oracle images are valid. The first exploratory `native` run is rejected because its setup failed to reactivate the Smudge modifier after constructing the panel.

Only new synthetic files below `.artifacts/smudge-20260927` are created. No live editor, original project, Blender connection, network, or user settings are used. UI tests additionally cover project preset persistence and failure recovery in temporary directories.
