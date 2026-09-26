# CSP SUT investigation

Investigation date: 2026-09-26. Source sample: the user-supplied
`workspace/dw_clip_studio_paint_brushes_free_2018__by_iridescentdelirium_dct4cxw.zip`.
Archive samples were examined from copies under
`.artifacts/brush-investigation/samples`. The archive and CSP's own materials
were not modified; the additional installed-data inspection below is read-only.

## What has been established

The archive contains **28 subtools and 92 distinct embedded material payloads**.
Every subtool has a SQLite header at offset zero and the `Manager`, `Node`,
`Variant`, and `MaterialFile` tables. Every supplied brush imports, including all
its active source tips and textures. All 92 unique material payloads can be
decoded at their original raster resolution. This is format coverage for this
pack, **not a claim that all versions of CSP or all brush behavior are matched**.

The importer in `comic_editor/core/sut_import.py` uses only standard-library
format readers plus the project's existing Pillow/NumPy image dependencies.
`import_sut(path)` returns the renderer-independent `BrushDefinition` model.
Original active settings, opaque binary values, source checksum, material
provenance, and explicit fidelity warnings accompany the imported preset.

### Why extracting a PNG is insufficient

The commonly published extractors search each material for its last PNG. In this
sample that finds `thumbnail/thumbnail.png`, not the registered tip image. An
earlier PNG is the material's `CanvasPreview`, which may be blank, downscaled,
or split across SQLite overflow pages. The first investigated material's preview
is 843 × 1000; its original registered raster is 1351 × 1603. Simply scanning
between PNG/IEND signatures also reads unrelated SQLite pages inserted between
the image's fragments and produces an invalid PNG.

The implementation instead follows the original layer's raster links and
decodes actual `Offscreen` tile data. It retains transparent padding and does
not crop the tip's canvas. This matters for chains, scatter alignment, pattern
sequence, and brush rotation.

## Container and settings

| Data | Observed role | Confidence |
| --- | --- | --- |
| `Manager.Version` | Settings schema version; supplied pack `131`, installed CSP 1.12 tool database `138` | Verified in reference files |
| `Node.NodeName` | Display name | Verified |
| `Node.NodeVariantID` → `Variant.VariantID` | Current settings | Verified |
| `Node.NodeInitVariantID` | Separate initial/default settings; mostly null in this pack | Verified |
| `MaterialFile.OriginalPath` | Exact key referenced by tip/texture lists | Verified |
| `MaterialFile.FileData` | An uncompressed TAR containing the registered material | Verified in all 92 unique payloads |
| `BrushPatternImageArray` | Ordered material reference list | Verified |
| `TextureImage` | Same reference-list structure, used for texture | Verified |

SUT is a subtool format, so a `.sut` extension alone does not establish that a
file is a drawing brush. Import rejects a non-drawing subtool, missing active
variant, unresolved active tip, and tool groups. Future support can add `.sutg`
without changing the stroke renderer. The importer must not take the first
`Variant` row as the current settings; its row order is not the semantic link.

The reference-list binary is big-endian integers containing byte-length-prefixed
UTF-16LE strings. The outer header is `(8, count)`. Each item starts with its
byte length, followed by a path string, flags, a display-name string, catalog
version, catalog-path string, and trailing data. Paths are identifiers, never
filesystem destinations. The importer preserves item order and resolves exact
`OriginalPath` keys, rather than interpreting all material rows as tips.

### Dynamics

The supplied scalar dynamics use a 44-byte header (`11 × uint32`, big-endian).
It contains header size, available-input flags, enabled-input flags, minimum
ratios, pressure/tilt curve byte lengths, and other values. Enabled bits are
`0x10` pressure, `0x20` tilt, `0x40` velocity, and `0x80` random. Minimum values
are percentages. Each stored curve starts with `(12, point count, 16)` and then
big-endian float64 `(x, y)` pairs. Disabled channels still retain their curves.
Nonlinear and nonmonotonic y values are retained; curves are not silently
replaced by linear pressure.

All samples' angle effectors are integer flags rather than these binary blocks:
3 is the baseline, 67 includes the stroke-direction bit (`0x40`), 131 includes
random (`0x80`), and 195 combines them. `BrushRotationRandomScale` controls its
amount. These interpretations are consistent with the sample brush types, but
their angle convention still needs controlled visual comparison with CSP.

## Original material raster decoder

Each material TAR has entries resembling:

```
catalog.zip
info.zip
data/material_0.layer
thumbnail/thumbnail.png
icedata/layerData.xml
```

Despite the `.zip` names, the first two are C2F containers in this pack. The
actual `.layer` is also C2F. It starts `89 43 32 46 0d 0a 1a 0a`. Chunks have a
little-endian 32-bit length, four-character type, payload, and little-endian CRC32
of type plus payload. Observed chunk types are `HEAD`, `dATA`, and `TAIL`.

All 92 sources have the same material layout: the first `dATA` has a 16-bit
encoding value of 1 and 5128 opaque bytes representing the first 5120 SQLite
bytes; the next has encoding 0 followed by intact 1024-byte SQLite pages. The
implementation does **not** decrypt that prefix or claim a general C2F decoder.
It validates the known layout and recovers complete records from available
table-leaf pages, reconstructing overflow chains by the SQLite format rules.
Unknown encodings fail explicitly.

Recovered table schemas and row links establish this path:

```
Layer.ResizableOriginalMipmap
  → Mipmap.MainId → Mipmap.BaseMipmapInfo
  → MipmapInfo.MainId → MipmapInfo.Offscreen
  → Offscreen.MainId → Attribute + BlockData
```

When no resizable source is present, the regular layer render mipmap is used.
For an incomplete schema the reader can select the largest populated original
Offscreen payload and records that fallback in metadata. No thumbnail or canvas
preview is eligible. All 92 supplied materials resolve through the actual links.

`Attribute` contains a UTF-16BE `Parameter` tag followed by dimensions, tile
counts, channel layout and strides. `BlockData` contains `BlockDataBeginChunk`
and `BlockDataEndChunk` records. Each present tile has a length-delimited zlib
stream. Tiles are 256 × 256. The supported sample layouts have a separate alpha
plane followed by either no image plane (alpha mask) or one grayscale plane.
The reader also decodes separate alpha plus interleaved BGRA color. Installed
CSP 1.12 Bead chain confirms the red gemstones and gold chain have the same
colors as the native material thumbnail; its actual registered image is
1617 × 852. That thumbnail is comparison evidence, never an input to decoding.

The installed materials add a 4096-byte SQLite page layout. The reader tries
legal page sizes against complete typed records because the unavailable prefix
contains the original SQLite header. Genuine `Offscreen` records settle the
layout. Synthetic 1024- and 4096-byte fixtures exercise overflow recovery.

`LayerColorTypeIndex`, `LayerColorTypeBlackChecked`, and
`LayerColorTypeWhiteChecked` determine whether the resulting tip recolors as a
mask, uses main/sub-color from its luminance, or retains its own RGB. Of the 92
sample sources, 73 use grayscale with black and white enabled, and 19 use black
only. The decoder emits straight RGBA and retains the source dimensions.

## Field mapping and remaining calibration

| CSP fields | Portable model | Status |
| --- | --- | --- |
| `BrushSize`, `BrushSizeUnit` | Pixel size; raw length and conversion DPI provenance | Unit 0 retained; inferred millimeter code 2 converted at selected DPI; unknown codes warn |
| `Opacity`, `BrushFlow` | Stroke opacity and separate per-dab density | Mapped; overlap behavior requires comparison |
| `AntiAlias`, `BrushAtLeast1Pixel` | AA level, one-pixel minimum | Mapped |
| `BrushHardness`, `BrushThickness`, `BrushVerticalThicknes` | Tip softness/aspect/axis | Mapped; exact softness transfer uncalibrated |
| `BrushRotation`, `BrushRotationEffector`, `BrushRotationRandomScale` | Angle, direction, angular randomness | Flags interpreted; convention uncalibrated |
| `BrushInterval`, `BrushAutoIntervalType` | Explicit diameter-relative spacing or automatic approximation | Explicit values mapped; auto-gap requires controlled measurements |
| `BrushAdjustFlowByInterval` | Density adjustment for gap | Mapped |
| `BrushPatternImageArray` and `BrushUsePatternImage` | Ordered original image tips or geometric circle | Verified extraction |
| `BrushPatternOrderType`, `BrushPatternOrderType2`, `BrushPatternReverse` | Tip repeat sequence | Provisional enumeration; CSP Reverse maps to back-and-forth, not descending repeated order |
| `BrushRibbon` | Ribbon rendering | Mapped flag; installed Chain/Thin chain/Bead chain/Zipper/vislon fixtures available |
| `BrushContinuousPlot` | Continuous drawing during movement and stationary holds | Mapped flag; additive time/distance cadence uncalibrated; dormant with primary Post correction |
| `BrushUseSpray`, `BrushSpraySize`, `BrushSpraySizeSyncBrushSize`, `BrushSprayDensity`, `BrushSprayBias` | Spray controls including relative particle size | Scalars and relative flag mapped; distribution uncalibrated |
| `TextureImage`, `TextureScale2`, `TextureRotate`, `TextureDensity`, related flags | Original texture and transform | Extraction verified; ten-mode order corroborated by installed resources, equations uncalibrated; see `csp-texture-mode-evidence.md` |
| `BrushUseWaterColor`, `BrushWaterColor`, `BrushMixColor`, `BrushMixAlpha`, `BrushMixColorExtension` | Paint amount, density, stretch and mixing mode | Behavioral approximation; active dual brushes resolve enabled mixing to Smear per the official guide |
| `BrushUseWaterEdge` and `BrushWaterEdge*` | Watercolor border controls | Mapped; behavior uncalibrated |
| `BrushInOutTarget`, `BrushUseIn/Out`, `BrushInOutType`, length/ratio fields | Parameter taper, length/percentage/Fade, per-parameter minima | Type 0 length, type 1 percentage, type 2 Fade; mode IDs and exact envelopes still need controlled comparison |
| `BrushUseRevision`, `BrushRevision`, related flags | Enabled post-correction strength | Amount mapped /100; curve/speed/view-scale behavior still uncalibrated |
| `*Effector` binary fields | Pressure/tilt/velocity/random channels | Pressure and random decoding tested; velocity normalization provisional |
| `Manager.PressureGraph`, initialization/use flags | Source-specific global input curve on the imported preset | Decoded before brush pressure curves; no change to app-wide tablet preferences |
| `CompositeMode` | Ink blending, distinct from dual combination | Native general blend enumeration and Erase 27 mapped; additional unknown tool modes warn |
| `UseDualBrush`, `Dual*`, `SyncDualBrushSize`, `DualBrushCompositeMode` | Nested brush, its original tips/textures, linked size and combining mode | Distinct 0–12 mode list; Wet wash 12 and Glitter 2 corroborated by official examples |
| `BrushChangePatternColor`, `BrushChangeStrokeColor`, hue/saturation/value/subcolor fields | Per-tip and per-stroke jitter | Enable flags respected; distributions and color-target behavior need calibration |
| `BrushHueChangeEffector`, `BrushSaturationChangeEffector`, `BrushValueChangeEffector` | Signed color response channels | Enabled inputs, curves and signed minima applied; combined equation still uncalibrated |
| `BrushSizeSyncViewScale` | Size specified on screen | Boolean mapped; resolved at pen-down without rewriting nominal preset size |
| `BrushRotationInSpray`, `BrushRotationEffectorInSpray`, `BrushRotationRandomInSpray` | Independent particle angle/direction and additive randomness | Numeric angle retained; direction bits and random amplitude provisionally mapped |
| `BrushPatternReverseHorizontal/Vertical` | Tip flipping | Provisional enumeration |
| `TextureBrightness/Contrast` | Texture brightness/contrast | Scalars mapped |
| `BrushAdjustVelocity` | Retained correction flag | Algorithm switch unapplied; active setting explicitly warns |
| Other unknown fields | Original source metadata | Preserved for later mapping |
| Reference layers, vector erasing/magnet, erase all layers | None in this raster tool | Outside current scope; preserve metadata |

The normalizer stores document-pixel inputs and portable brush settings apart
from raster storage. That allows a later vector feature to retain input paths,
pressure, deterministic random seed, and preset identity without changing the
SUT reader. No vector-layer support is added by this importer.

## Coverage inventory

| Brush | Active image tips | Texture | Mixing | Spray |
| --- | ---: | --- | --- | --- |
| dw Cel | circle | no | no | no |
| dw Charcoal 2 | 8 | no | no | no |
| dw Charcoal | 3 | no | yes | no |
| dw Digi Chalk 2 | 4 | no | no | no |
| dw Digi Chalk | 4 | no | no | no |
| dw Digi Pencil | 3 | no | no | no |
| dw Dust In The Wind | 8 | no | no | no |
| dw Fluff | 2 | no | yes | no |
| dw Fragmented Line | 4 | no | yes | no |
| dw Freckles | 1 | no | no | no |
| dw Glow | circle | no | no | no |
| dw Indubitably | circle | no | no | no |
| dw Ink Splatter | 9 | no | no | no |
| dw Leafy Ink | 4 | no | no | no |
| dw Painterly 2 | 1 | yes | no | no |
| dw Painterly 4 | 1 | yes | yes | no |
| dw Painterly | 1 | yes | yes | no |
| dw Pixel Sponge | 1 | no | yes | no |
| dw Rolling Ink Gathers No Moss | 2 | yes | no | no |
| dw Rough Chalk | 8 | no | no | no |
| dw Soft Pastel | 8 | no | no | no |
| dw Soft Round | circle | no | yes | no |
| dw Squares 2 | 1 | no | no | yes |
| dw Squares | 4 | no | no | no |
| dw Star Splatter | 1 | no | no | no |
| dw Stardust | 1 | no | no | no |
| dw Strands 2 | 1 | no | no | no |
| dw Strands | 1 | no | yes | no |

This pack has no ribbon-enabled brush, dual brush, or full-color tip. The local
CSP executable was reported as 1.12.0 by the parent investigation. Installed
references below extend those cases. New current-version native exports remain
useful. A successful import is not a substitute for identical-input stroke
comparison.

## Installed CSP 1.12 reference investigation

The narrow, read-only inspection used
`CELSYSUserData/CELSYS/CLIPStudioPaintVer1_5_0/Tool/EditImageTool.todb` and its
referenced materials under `CLIPStudioCommon/Material`. A SQLite backup to the
workspace retained a consistent snapshot without changing CSP data. This
database has 282 nodes, 438 variants and 111 drawing-brush nodes. Its
`Manager.CommonVariantID` points to a row absent in the snapshot; unknown
inheritance was not invented.

Thirteen **research fixtures reconstructed from installed data**, not native
CSP exports, are under `.artifacts/brush-investigation/installed/reconstructed`:
G-pen, Chain, Thin chain, Bead chain, Leaves, Bloodstain, Pencil, Watercolor
splash, Zipper, Rose, K-Outline, Wet wash and vislon close. Each fixture copies
its current non-null settings and only its required material payloads into a
new local SQLite file. A `ReconstructionInfo` table and importer warning keep
this distinction visible. Six have an enabled secondary brush: Pencil,
Watercolor splash, Zipper, Rose, K-Outline and Wet wash. The inventory and
reconstruction script remain local research artifacts, not distributed assets.

The material reference's catalog path resolves both installed defaults
(`Install/Paint110/...`) and already-downloaded materials (`59/07/...`, for
example). No Asset Store account, credentials, unrelated project files or
external material download was needed.

The official [Zipper Fasteners page by vitamin01](https://assets.clip-studio.com/en-us/detail?id=1745144)
lists **vislon close** as one of its brushes (Content ID 1745144). This exact
name and the matching zipper material support the pack association. The local
material catalog does not contain that numeric Asset Store ID, so this is a
public catalog match rather than an independently decoded account-download ID.

### Avoiding incorrect visual fixes

The installed vislon close source is 164 × 217, Gray expression color with
black and white enabled. Its native material thumbnail matches the actual
decoded original exactly: white tooth interiors with black outlines. Among
pixels with alpha greater than 200, 21,552 are white and 4,269 are black.
Consequently main-black/sub-white produces outlined white teeth, while
main-white/sub-black produces filled black teeth with a white outline. The
user's CSP screenshot has white main and black secondary swatches. This
explains the interior-color difference without changing any source pixels or
misclassifying the tip as an alpha mask. A local diagnostic is
`installed/vislon-color-context.png`. Palette preview color conventions still
need controlled comparison separately from actual painted strokes.

The installed Leaves active variant is size **4.3**, relative particle size
**29.872%**, while its reset variant is size **200**, relative particle size
**100%**. Both have `BrushSizeUnit=0`; so do vislon close, Chain and Rose.
Leaves' tiny current preview is therefore supported by its active values and
is not evidence of a missed millimeter conversion. Nonzero unit code **2**
occurs elsewhere and is now interpreted as millimeters using the corroborated
mapping and an explicit import-DPI context. Raw measurements remain preserved;
interactive imports with active physical lengths offer a resolution choice.
None of these Unit0 Leaves values receives that conversion. Controlled native
exports would still strengthen the raw-enum and physical-sizing calibration.

A separate reset-labelled Leaves fixture is available at
`installed/reset-reconstructed/CSP112-Leaves-reset.reconstructed.sut` for an
appropriate default-size comparison. It deliberately uses reset variant 754,
records this choice in `ReconstructionInfo`, and leaves the active fixture
unchanged. Its material identifiers match; default display names are Japanese.

The installed database also establishes the post-correction field group
`BrushUseRevision`, `BrushRevision`, `BrushRevisionBySpeed`,
`BrushRevisionByViewScale`, and `BrushRevisionBezier`. Strength values range
through 100; enabled values are normalized to 0–1 with a calibration warning.
`BrushInOutType=1` is consistent with native speed/focus-line tools using
`BrushInRatio/BrushOutRatio`, while type 0 uses length fields. Each enabled
`BrushInOutTarget` triplet contains parameter ID, enabled flag and minimum
percentage: Real G-Pen has size minimum 80; Firework has size and opacity
minimums 30. Each active parameter's minimum is retained separately in
`taper_minima`, with `taper_minimum` as the portable fallback. Unknown taper
mode values beyond the documented modes are preserved but disabled, not
silently treated as length.

The same official [drawing-tools guide](https://tips.clip-studio.com/en-us/articles/563)
describes Fade as progressing to each selected minimum within a specified
length, with no Starting component. The [settings manual](https://help.clip-studio.com/en-us/manual_en/810_subtools/S.htm)
adds that the reached minimum is retained for the rest of the stroke. Neither
source specifies an exact interpolation formula or binary enum value.
The importer provisionally assigns mode 2 from the documented menu order,
disables Starting, and maps `BrushOutLength` to the fade extent. A specific
warning preserves the need for a controlled native export of this enum.

The installed Manager also has `PressureGraphInitialized=1`,
`UsePressureFile=1`, and a 124-byte `PressureGraph`. It uses the same curve
format as parameter dynamics: big-endian `(12,7,16)` followed by seven
float64 coordinate pairs. Its curve rises from `(0,0)` to `(0.49624,1)` and
then remains at 1. In contrast, the supplied pack has no global graph. This
is device-pressure context, separate from the brush's own pressure dynamics;
the [official pressure guide](https://help.clip-studio.com/en-us/manual_en/240_brushes/Adjusting_pen_pressure.htm)
states that this calibration applies across tools. The importer retains its
knots as `global_pressure_curve` on both primary and secondary definitions.
Each pressure-dependent parameter evaluates this calibration against the raw
input before its own brush curve, so a secondary brush does not receive an
already-calibrated input. This is calibration retained for the imported preset,
not an alteration of the app's tablet preferences. Explicitly disabled or
uninitialized graphs remain identity; corrupt graphs produce a warning and
remain available as raw metadata.

The installed color-jitter effectors expose an additional limitation: their
44-byte headers can contain signed minimum values, including `FFFFFF9C`
(-100) for random hue, saturation and brightness. Ordinary nonnegative scalar
dynamics cannot express these faithfully. Signed color offsets now use
separate portable fields and signed response ranges, while size/opacity
remain nonnegative. Active color changes still carry a warning about their
uncalibrated combined-input equation and random distribution.
Likewise, active ribbon angle dynamics generate a warning; static ribbon
rotation and path-following are separate supported behaviors.
Additional warnings identify active ribbon color mixing, time-based continuous
ribbon buildup during movement or stationary holds, and secondary-only watercolor edges, canvas blending or
post-correction that the current renderer cannot apply independently. The
`BrushAdjustVelocity` flag is retained without claiming the undocumented
corrected-versus-legacy speed algorithm has been recreated.

## Validation and input handling

`tests/test_sut_import.py` passes 27 tests, including all 28 local sample files
when the private/reference pack is present. The distributed tests generate
their own fixtures; no third-party brush art is embedded in tests or built-ins.
Tests check active-variant selection, original file immutability, Unicode
material references, nonlinear float64 pressure curves and minimum ratios,
rejection of missing tips/non-SUT input, actual raster recovery across SQLite
overflow pages at both page sizes, registered padding, and C2F CRC failure.
They also check enabled-only secondary brushes, enabled-only color jitter and
all 13 local reconstructed fixtures. The sample tests check each embedded
image's dimensions and nonempty alpha. Additional cases keep length and
percentage and Fade taper values distinct, reject silent fallback for unknown
taper modes, retain independent taper minima, and respect the post-correction
enable flag. Global calibration cases cover primary/secondary definitions,
source immutability, disabled graphs and corrupt-graph fallback.
Color cases preserve signed base changes, signed random minima, pressure-only
curves with disabled stored random values, separate hue units, and exact
sub-color amounts across the portable preset round trip. Malformed color
effectors and unsupported full-color-tip sub-color mixing produce warnings.
Small generated fixtures test the aggregate pixel budget across both brushes,
shared-material deduplication without losing either definition's provenance,
shared encoded image strings, and budget rejection for both PNG and C2F data.
The sequence regression distinguishes CSP Reverse (back-and-forth) from
descending order and retains the documented one-time sequence modes.

Input is opened read-only with trusted schemas disabled; fixed table queries
are used, arbitrary source SQL is never executed, and the SQLite instruction
count is bounded. TAR paths are not extracted to disk. File, material, image,
curve, and collection limits bound decoding. Compressed tile expansion has an
exact expected size. Unknown active materials are errors, not substituted
round tips. Large NumPy tests use `OPENBLAS_NUM_THREADS=1` on this machine to
avoid unrelated BLAS worker allocation pressure.

Original decoded images have a 32-Mipixel individual limit and a 64-Mipixel
aggregate limit per import, shared across primary/secondary tips and textures.
The aggregate remaining allowance is checked against original dimensions
before allocating RGBA pixels. A shared cache counts repeated material paths
once and reuses their encoded PNG strings as well. Each definition still has
its own material metadata list and display names. The cache closes all decoded
images on successful import and on failure, avoiding dependence on cyclic GC.
The total encoded image payload is separately capped at 128 MiB, charging
every tip/texture occurrence across both definitions even when its string is
shared in memory. This bounds repeated-image expansion when the portable
preset is later written to JSON. Small mocked payload tests check exact-limit
acceptance and repeated secondary-tip/texture rejection without large images.

## Registered tool previews and additional downloaded fixtures

A read-only inspection of installed material catalogs found 45 registered tool
materials (`data/material_0.tool`). Their adjacent `thumbnail/thumbnail.png`
files are **tool-material thumbnails**, distinct from an image material's
CanvasPreview or tip thumbnail. Some show CSP's recognizable curved stroke
with a small tool icon; others are author-created artwork. The
`isChangeThumbnail` flag alone does not reliably distinguish those cases.
These are useful independent visual references, but they do not include an
input trajectory or pressure trace and are not captures of the current live
menu. No stored stroke bitmap was found in the inspected SUT tables or the
active Manager/Node/Variant tool database. Its one NodeCustomIcon PNG is an
icon, not a stroke.

Private local artifacts under `.artifacts/brush-investigation/installed/`:

- `tool-material-inventory.json`: original tool and thumbnail paths, catalog
  names, and registration metadata; `tool-previews/contact-sheet.png` helps
  distinguish automatic-looking strokes from custom artwork.
- `chain6-reference/registered-tool-preview.png`: the downloaded **chain 6**
  reference from [RAI'S CHAIN BRUSHES, content 1996420](https://assets.clip-studio.com/en-us/detail?id=1996420)
  by RaikaiRan. The local catalog's content ID and child name match the public
  author listing. Its original tip is `chain6-reference/original-tip-1.png`.
- `chain6-reference/chain-6.registered-tool.reconstructed.sut`: a clearly
  labelled research fixture, not a native SUT export. The complete
  `Material.MaterialToolVariantManager` SQLite blob was recovered from an
  intact cell in the available tail of the partly obscured second page and
  its standard overflow chain. The resulting inner database passes SQLite
  `integrity_check`. The opaque prefix was not decrypted or guessed. Embedded
  `ToolPatternInMaterial.FileData` provides the actual original material.
- `flat-pattern-reconstructed/`: nine exact active Flipnote Standard/Dither
  definitions reconstructed from the fresh read-only installed tool snapshot,
  plus the original decoded texture PNGs. No tool preview was found for these
  presets. `flat-pattern-settings.json` records the extracted settings.

Chain 6's stored settings version is 139, with size 140.3, ribbon enabled,
angle 0, thickness 100, interval 28.3, and forward order. Its registered
original image is 205 by 1020 pixels, gray expression with both black and
white enabled. Its nonempty alpha reaches all four edges. The registered
image geometry independently states 205 by 1020, unit scale in both axes,
center (102.5, 510), and corners spanning those dimensions. The enclosing
material canvas is 206 by 1021 at 350 dpi. These data do not establish whether
CSP normalizes ribbon brush size against the cross-stroke width or maximum
source dimension. No independent size-normalization field was found. A
fitted thumbnail is not sufficient evidence to change that formula.

The Flipnote presets use hard circular tips, no anti-aliasing, and no ribbon.
Standard has no texture. Dither Steps 1 and 7 use 3-by-3 texture images;
Steps 2 through 6 use 2-by-2 images; Step ? uses a 4-by-4 image. All have
texture scale 100%, density 100%, angle zero, multiply mode, and
`TextureForPlot=0`, which maps to a canvas-anchored repeating texture. These
tiny registered tiles must retain their exact dimensions and phase rather
than stretch with brush size or reset at each dab. Active brush sizes vary
because the installed settings contain the user's saved choices.

CELSYS's [ribbon creation guide](https://tips.clip-studio.com/en-us/articles/681)
confirms that ribbon tips connect top to bottom and deform along the stroke;
left-to-right artwork requires a 90-degree tip rotation. It also explains why
registered padding and the compatibility of successive images matter. It
does not publish the numerical width-normalization rule.

## Signed color dynamics and remaining calibration

Importer version 4 maps per-tip color changes into `hue_shift`,
`saturation_shift`, and `luminosity_shift`, with corresponding dynamics keys.
It keeps authored legacy `*_jitter` semantics separate, so loading an existing
Webtoon Maker preset does not reinterpret its random amplitudes. Inspection
of actual current variants establishes that their base
color changes are signed: observed hue values include -170 and 200,
saturation includes -10, and brightness includes -20. A generic unsigned
percentage clamp loses valid source values. The active color effector fields
are `BrushHueChangeEffector`, `BrushSaturationChangeEffector`, and
`BrushValueChangeEffector`; they have the familiar 44-byte big-endian header
and optional float64 curve blocks.

| Byte offset | Evidence / interpretation |
| --- | --- |
| 0 | Header size 44 |
| 4 | Available input flags, observed 240 |
| 8 | Enabled input flags: pressure 16, tilt 32, velocity 64, random 128 |
| 12 / 16 / 20 | Pressure / tilt / velocity minimum percentages |
| 24 | **Signed** random minimum percentage, observed -100, -35, -20, -10, 65 |
| 28 | Undetermined, observed zero |
| 32 / 36 | Pressure / tilt curve byte lengths |
| 40 | Undetermined; often 500 for color, plausibly tilt maximum, unverified |

Concrete examples in `installed/color-dynamics-header-evidence.json`:
Leaves saturation and brightness enable pressure only, despite containing
stored random limits. Crayon brightness has no active input and a base
offset of -3. Thin chain brightness is -20 with pressure enabled. Thus the
amount must not automatically imply randomization, nor should disabled
stored inputs affect the result. Source response curves include descending
and nonlinear mappings.

The importer now preserves these signed values and minima and only applies
enabled inputs. `BrushSubColor` becomes the constant/dynamic
`sub_color_amount`, without forced randomization. All four per-tip changes
are gated by `BrushChangePatternColor`; their stored values remain in the
source record when disabled. An active nonstandard tilt maximum is retained
with a warning because that scale is not implemented. An active sub-color
amount on a full-color image tip is also explicitly reported as unsupported.

The [official color palette documentation](https://tips.clip-studio.com/en-us/articles/657)
defines hue in degrees over a full 360-degree cycle, and saturation and
brightness in percent. Interpreting brush hue change as degrees divided by
360 is supported by these units and a
[brush author's controlled examples](https://tips.clip-studio.com/en-us/articles/12505)
using per-tip Hue 360 and per-stroke Hue 180. Both hue imports now use this
conversion. `BrushChangeStrokeColor` gates the existing `stroke_*_jitter`
fields, which sample once per stroke; saturation, brightness, and sub-color
amounts use percentages. The per-stroke random distribution remains an
approximation. The
[official dynamics guide](https://tips.clip-studio.com/en-us/articles/563)
defines minimum values as percentages of the configured parameter and says
each enabled input participates. The [color-jitter manual](https://help.clip-studio.com/en-us/manual_en/810_subtools/C.htm)
distinguishes per-tip dynamics, per-stroke randomization, and the affected
main/sub colors. The source `BrushChangeColorTarget` is mapped provisionally
using that documented menu order: 0 both, 1 main, 2 sub. A warning accompanies
enabled color changes because a controlled native export has not yet verified
every numeric target value. None of these sources publishes the exact
combined-input equation or random distribution. Those remaining uncertainties
stay explicit; field mapping alone does not establish native rendering parity.

Saved imported presets are not automatically remapped. They retain their
saved parameter values; renderer improvements may still change future stroke
output. Re-import the original `.sut` to obtain the corrected source color
mapping. If that import differs from an existing preset with the same source
ID, the brush library creates a uniquely named copy with a new ID, preserving
the old preset and its edits. An identical existing import is selected, and an
unchanged re-import copy is reused on subsequent imports. Copy comparison
ignores only its top-level name and ID; source metadata, compatibility notes,
secondary brushes, and all other settings must still match. Importing changes
the preset library and selection only, without repainting existing artwork.
Seven focused re-import tests cover old mappings, edited names/sizes/dynamics,
secondary-brush edits, occupied copy names, repeated imports, and edited copies.

## Primary references consulted

### Ink and dual blend correction, importer version 5

Ink and dual-brush modes do **not** share an enumeration. The earlier importer
incorrectly mapped Ink 2 to Add and fell back to Normal for erasers. Named layers
in the independent [native blend-mode fixture](https://github.com/LavenderSnek/clipdecode/blob/main/assets/blend-modes.clip)
establish the general CSP ordering (0 Normal, 1 Darken, 2 Multiply, 3 Color burn,
and so on). Read-only installed default watercolor variants use Ink 2, while
the Hard, Soft, Rough and Kneaded erasers all use Ink 27. These now map to
Multiply and Erase. The fixture's hash and decoded layer names are recorded in
`installed/blend-enum-evidence.json`; installed tool evidence is in
`installed/extra-ink-enum-evidence.json`. Reuse of the general blend enumeration
for other Ink modes remains an inference until mode-labelled subtool exports
are available. Numerical glow/nonseparable blending behavior still requires
native raster comparisons.

The [official dual-brush guide](https://tips.clip-studio.com/en-us/articles/4845)
lists 13 modes and identifies Wet wash as Height (Linear) and Glitter as
Add (Glow). Both active and reset installed variants corroborate their stored
values, 12 and 2. The importer now uses the distinct ordered 0–12 list; the
remaining ordinals still need controlled exports. In particular, Wet wash
no longer imports as Soft light. The renderer's Height (Linear) formula is
still an independent approximation, not recovered CSP mathematics.

Additional Ink modes 29 and 31 occur locally but their labels are unverified;
28, 30 and 32–35 are absent from the examined sources. They receive an explicit
unknown-mode warning and retain the original number. The
[official Ink manual](https://help.clip-studio.com/en-us/manual_en/810_subtools/I.htm)
disables canvas blending and opacity dynamics for Blend/Running color. The
engine now respects those inactive controls while keeping their saved values;
Smear and dry painting continue to use them.

### Screen size and independent spray orientation

All 28 supplied brushes have `BrushSizeSyncViewScale=1`. The
[brush-size manual](https://help.clip-studio.com/en-us/manual_en/810_subtools/B.htm)
defines this option as preserving the apparent size at 100% view. It now maps
to `size_by_view`, resolved through a Qt-free helper from a pen-down view-scale
snapshot. Preview rendering uses 100% view. CSP has no separate secondary
screen-size flag in the examined schema; only a size-linked secondary follows
the resolved primary size. Nonuniform/projective raster transformations use
an equal-area local scale and retain their anisotropy; this is not a claim of
perfect circular compensation for arbitrary transforms.

The [spraying manual](https://help.clip-studio.com/en-us/manual_en/810_subtools/S.htm)
separates particle orientation from the whole spray and allows random variation
alongside its chosen direction. The source numeric particle angle is now
imported directly. Enabled flags provisionally map 0x40 to line direction,
0x100 to whole-spray direction, 0x200 toward the center, and 0x80 to additive
random variation. The angle no longer inherits the whole-spray angle unless
that mode is selected. The 0x100/0x200 mappings receive an explicit notice;
the random-amplitude conversion still needs controlled comparison. Earlier
authored portable presets retain their prior orientation through a loader
migration when the new angle field is absent.

`BrushSizeUnit` values observed locally are 0 and 2, not 0 and 1. Other saved
CSP settings associate 0 with pixel canvas dimensions and 2 with A4 dimensions,
strongly supporting pixels/millimeters. Imports now take an explicit DPI
context (300 by default), record the source measurement and converted pixels,
and offer a resolution choice before publishing a brush containing active
millimeter lengths. The code never infers a DPI from brush size. Each physical
field has its own unit; relative sizes and percentages bypass conversion.
The raw unit enumeration still needs a controlled CSP export. See
`docs/csp-brush-units.md` for exact fields and evidence.

Active unsupported stabilization-by-speed, sharp-angle, release-tail candidate,
post-correction speed/view scale/method and starting/ending speed options now
have specific compatibility notices. The investigation and source counts are
in `.artifacts/brush-engine/correction-gap-investigation.md`.

### Other primary references

- [CELSYS: importing and exporting tools](https://help.clip-studio.com/en-us/manual_en/150_tools/Importing_and_exporting_tools.htm)
  establishes `.sut` as tool settings and `.sutg` as a group, and describes the
  supported export workflow. It does not publish a binary specification.
- [MorrowShore CSPBrushExtract source](https://github.com/MorrowShore/CSPBrushExtract/blob/main/cspbrushextract.py)
  demonstrates SQLite parameter extraction and a last-PNG strategy. Its code
  was inspected as evidence, not incorporated; the original material reader
  here uses an independently implemented page/record decoder.
- [CSP2PC converter source](https://github.com/Leon-Schoenbrunn/CSP2PC/blob/main/csp2procreate.py)
  provides another format implementation and labels its mapping approximate.
  Its converter's visual fudge factors are not evidence of CSP equations and
  were not used.
- [LavenderSnek clipdecode Offscreen structure](https://github.com/LavenderSnek/clipdecode/blob/master/src/exta/offscreen.rs)
  documents the block tags and mixed-endian length fields; these were checked
  against the supplied material bytes. No code was copied.
- [SQLite file format](https://www.sqlite.org/fileformat.html)
  defines table-leaf records, serial values, page-local payload sizes and
  overflow chains used to recover the material's intact records.
- [CELSYS: selecting colors](https://help.clip-studio.com/en-us/manual_en/300_color/Selecting_colors.htm)
  confirms that main and secondary colors can both participate in brush tips.
- [CELSYS: brush-tip and size settings](https://help.clip-studio.com/en-us/manual_en/810_subtools/B.htm)
  distinguishes tip shape, thickness, angle, density and displayed size.
- [CELSYS: preferences](https://help.clip-studio.com/en-us/manual_en/720_preferences/Preferences.htm)
  documents application length units in pixels or millimeters, but does not
  identify the stored SUT enumeration or supply an export's document DPI.
- [Brushfactory SUT notes](https://brushfactory.co/formats/sut)
  give independent evidence for table roles and dynamic-input flags. All claims
  used for this pack were checked against the files; its general conversion
  fidelity claims were not treated as proof of compatibility.
