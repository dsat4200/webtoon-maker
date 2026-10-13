# Stroke feedback and tablet responsiveness, October 13, 2026

The investigated capture is `20261013-184153-eb9dcc4456`. Its 900×937 GPU
viewport shows one solo raster with six native source tiles in a
1080×82000 chapter containing 110 layers, 185 objects, 108 modifiers and
32 masks. The selected raster is `b02d3368d6134b99b294b31f94c91d5a` in chapter
`0a72f08009294aa0a3d14e6a38e22bbb`; camera center is
(829.8467936405525, 52263.87461387311), scale .9712636189917223 and rotation
−.663008407605119 degrees. Pencil transform controls are visible.

## Changes

The screenshots' surround colors are #282828 and #242428. Ordinary presentation
already honors the theme. Live flattened patches and native/retained replacement
backgrounds now honor it too. The flattened patch signature and retained view
owner include the surround color. Palette changes clear provisional patches
and continuity; original source and exact artwork cache identities are unchanged.

Ordinary tablet input bypasses the global popup widget lookup when there is no
popup, modal dialog or forwarded contact. Cursor and tooltip setters run only
when their values change. Idle painting hover repaints share a 16 ms deadline
from the last actual paint, and native contact admission promotes pending damage
immediately. Rotation no longer forces an extra update for every overlay packet;
complete guides redraw on annotation readiness changes. Source samples and
ordered press/move/release transactions are preserved.

The opt-in monitor now measures whole tablet handlers, cursor resolution, popup
forwarding and window chrome handling. A separate receipt-to-covered-paint
outcome includes deferred admission. The older first-covered statistic remains
paint duration. Cancelled contacts and pending/replayed presentation have
explicit diagnostic outcomes. These measure application receipt and coverage
metadata, not driver delivery, GPU completion or display scanout.

## Desktop evidence

Baseline source was frozen with the existing checkout changes intact. The
chapter and recovery were copied privately under
`.artifacts/stroke-feedback-20261013/project-private`; no source project or
user settings were written. Native Windows Qt/OpenGL ran through the real
MainWindow, navigator, outliner and input filters on the NVIDIA RTX 5060.

Three pencil and three eraser contacts used 120 Hz packets, .5-second holds,
15 ms contact gaps, the captured camera/solo state and #282828 surround. Both
versions had pencil transform controls enabled. A one-second hover preceded
the contacts. Both matched timing runs disabled the pixel observer and monitor.
No synthetic graphics pressure was added.

| Measurement | Frozen baseline | Final candidate |
| --- | ---: | ---: |
| First covered swap p95, six contacts | 21.75 ms | 20.35 ms |
| Hover frames in one second | 119 | 60 |
| Longest drawing-phase heartbeat gap | 25.34 ms | 25.84 ms |

The final candidate's separate cursor-free pixel witness proves fresh native
ink during all six held contacts, at 11.36–22.92 ms. Selected native source
buffers are byte-identical to baseline at initial, pencil, eraser, final and
undo stages. Undo restores the model and source bytes; restoring the initial
tool before framebuffer comparison also gives zero framebuffer differences.
Project file hashes remain unchanged. Source/effect grids, native precision,
exact tiles and durable caching use their ordinary contracts.

A separate final-code run evicted the selected clean source buffers before its
first contact and enabled monitoring. All six receipt-to-covered-paint outcomes
completed, with a maximum of 37.35 ms and no cancelled receipts. Handler, cursor,
popup and native window timings are present. OS disk caches were not flushed.

## Regression coverage and limits

The focused offscreen run passes 321 tests with four skips. Coverage includes
theme changes during pending feedback, rotated solo input, popup/modal release,
cursor affordances, deferred receipts, native seam/alpha/16-bit composition,
presentation continuity and monitor ownership/disabled behavior. Two additional
native frame-dispatch cases pass on Windows.

The additional native GPU mouse comparison case cannot commit its control
stroke on either the frozen baseline or candidate; it fails before monitoring
is enabled. It supplies no positive validation evidence. The two archived
saved-project continuity cases require chapters outside the chapter-only
fixture and remain skipped. The captured chapter is exercised by the desktop
probe instead.

Neither baseline nor candidate reproduces the manual capture's 1–2 second
pause. Warm stroke startup has no demonstrated speedup in this probe. The
measured reduction is idle hover presentation, and the color mismatch is fixed.
The original pause must not be declared eliminated from these results. Future
captures now distinguish event handling, deferred admission and covered paint
latency. Raw runs, source manifests, comparisons and test reports are retained
under `.artifacts/stroke-feedback-20261013/`.
