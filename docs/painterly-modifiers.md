# Painterly modifiers and limited mask gradients

Select a drawing, image, or shape/layer, then open **Modifiers → Add Modifier**.
All the effects below use the normal ordered, nondestructive modifier stack,
including linking, masks, undo, presets, saved chapters, export, and Raster Apply.

## Kuwahara

The **Kuwahara** category contains **Original Kuwahara**, **Papari Generalized
Kuwahara**, and **Anisotropic Kuwahara**. The Filter dropdown also switches the
variant on an existing modifier.

- **Original** selects the least-variable of four overlapping square regions.
  It produces the characteristic blocky painterly shapes.
- **Papari Generalized** combines eight smoothed circular sectors according to
  their variance, reducing the original filter's abrupt quadrant transitions.
- **Anisotropic** uses the image's local direction to rotate and stretch its
  eight sectors, following edges and long features.

**Size** is the sampling radius in source pixels. **Strength** mixes filtered
color with the incoming color. Both support masks, as does stack **Intensity**.
A size of zero preserves the incoming detail. Transparency is preserved.

Generalized and anisotropic modes expose **Sharpness**, **Hardness**, and
**Sector overlap**. Anisotropic adds **Anisotropy** and **Direction smoothing**.
**Passes** repeats the filtering. **Sampling** trades speed against sampling
artifacts (49, 113, or 317 samples). **Resolution** chooses full, half, or quarter
processing resolution; this is a saved artistic/performance setting used for
export and baking too. Large radii benefit from higher sampling quality.

### Keep a face detailed while abstracting the surroundings

1. Add a Kuwahara modifier. Click the mask button beside **Size** and enter mask
   edit mode.
2. Choose the gradient tool and **+ Limited Circular**. The new circular ramp is
   strongest at its center and falls to zero at the radius.
3. Move the start handle onto the face. Move the end handle to set the radius.
   Edit the ramp stops and their opacity to shape the transition.
4. Set the Size mask's **black endpoint to 20 px** and **white endpoint to 0 px**.
   The face retains detail; outside the circle, Size is 20 px. Intermediate mask
   values continuously change the actual sampling radius.
5. Leave mask edit mode. Bind the same saved mask to Strength, another modifier,
   or an Outline's Thickness when desired; each parameter has its own endpoints.

## Limited mask gradients

These objects belong to the mask and are available only while editing it. Add
multiple **Limited Linear** or **Limited Circular** gradients, select them from
the gradient list, and use **Remove** to delete the selected one. They do not
appear as visible artwork outside mask editing.

The existing ramp editor controls opacity stops. Start/end handles position the
ramp. Additional blue bounds handles control its area of application:

- **Start bound / End bound**: percentages of the start-to-end distance. Circular
  bounds specify inner/outer radii; linear bounds specify the ends of a strip.
- **Half-width**: the linear strip's distance on either side of its axis.
- **Edge feather**: softens the bounds inward, in chapter pixels.
- **Add to mask / Subtract from mask**: combines this region with the mask's
  contributors and earlier gradient regions.

Outside a limited region its contribution is zero. The parameter's black
endpoint controls that exterior value. This is useful for keeping a small focal
area sharp while increasing an effect everywhere else. In a mask, stop opacity
determines strength; the stop's RGB color is ignored.

## Dithering and sharpness

**Dithering** offers Ordered (Bayer) and Noise modes, 2–256 levels per channel,
dither strength, pattern size, and monochrome output. Ordered mode has 2×2,
4×4, and 8×8 matrices; Noise has a repeatable pattern and a new-pattern button.
Intensity, levels, dither strength, and pattern size support masks. Dither
strength controls noise before quantization; Intensity blends the whole effect.

**Sharpness** is an unsharp-mask filter with Strength, Radius, and Threshold.
Threshold leaves small color differences alone. All three parameters and
Intensity support masks. It normalizes the blur by alpha to avoid dark fringes
around transparent artwork. Spatial radius masks interpolate Gaussian levels.

## Brush outlines

In **Outline**, switch **Solid outline** to **Brush outline**. Choose a brush
preset or **Use current brush settings**, then adjust spacing, angle offset,
contour following, and pattern seed. The brush definition and embedded materials
are stored with the modifier, including in presets and copied artwork.

Closed strokes are traced around the source alpha, including disconnected
regions and holes. **Thickness** controls brush size, and a Thickness mask varies
the width along those contours and limits the exterior footprint. Limited
linear/circular gradients work with solid and brush outlines. Opacity, intensity,
antialiasing, and outline blur remain available. Closed outlines omit pen taper,
stationary continuous spraying, wet mixing, and erasing.

## Rendering and references

The original filter uses tiled summed-area tables; sector modes cache their
weights and process bounded tiles with a fixed sample budget. Anisotropic
direction uses a smoothed RGB structure tensor and polynomial sector weights.
Cached filtered colors can be reused when Strength or Intensity changes. Large
interactive renders use detached background jobs and smaller temporary previews;
final export uses the modifier's saved quality and resolution. The implementation
uses NumPy/SciPy on the CPU, without a new GPU/runtime dependency.

These are independent adaptations for this editor's premultiplied-alpha image
pipeline, rather than byte-identical ports of the Unity/ReShade shaders.

- [Acerola: This is the Kuwahara Filter](https://www.youtube.com/watch?v=LDhN-JK3U9g)
- [Acerola's linked shader source](https://github.com/GarrettGunnell/Post-Processing/tree/main/Assets/Kuwahara%20Filter)
- [AcerolaFX documentation](https://github.com/GarrettGunnell/AcerolaFX/wiki)
- [Kyprianidis et al., 2010: polynomial weighting functions](https://www.kyprianidis.com/p/tpcg2010/)
- [Kyprianidis et al., 2009: anisotropic Kuwahara filtering](https://www.kyprianidis.com/p/pg2009/)

The video description and linked source were inspected. YouTube's transcript
export returned no transcript, and its transcript panel remained loading; no
claim of a complete script review is made. The original UMSL PDF link returned
404, so the author's publication page was used instead.
