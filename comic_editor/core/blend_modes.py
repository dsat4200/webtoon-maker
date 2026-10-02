"""Object blend-mode contract and bounded, premultiplied compositing math."""
from __future__ import annotations

import numpy as np


OBJECT_BLEND_MODES = (
    ("normal", "Normal"), ("multiply", "Multiply"), ("screen", "Screen"),
    ("overlay", "Overlay"), ("soft_light", "Soft Light"), ("hard_light", "Hard Light"),
    ("linear_burn", "Linear Burn"), ("color_burn", "Color Burn"),
    ("linear_dodge", "Linear Dodge (Add)"), ("color_dodge", "Color Dodge"),
    ("darken", "Darken"), ("lighten", "Lighten"), ("luminosity", "Luminosity"),
    ("color", "Color"), ("hue", "Hue"), ("saturation", "Saturation"),
    ("difference", "Difference"), ("exclusion", "Exclusion"),
    ("luma_modulate", "Luma Modulate"), ("texture_multiply", "Texture Multiply"),
    ("texture_screen", "Texture Screen"), ("texture_contrast", "Texture Contrast"),
    ("alpha_modulate", "Alpha / Coverage Modulate"),
    ("height_modulate", "Height / Emboss Modulate"),
)
OBJECT_BLEND_MODE_IDS = frozenset(mode for mode, _ in OBJECT_BLEND_MODES)
MODULATION_MODES = frozenset(mode for mode, _ in OBJECT_BLEND_MODES if "modulate" in mode or "texture" in mode)
BLEND_MODE_HELP = {
    "luma_modulate": "Darken underlying artwork with this object's brightness; retain underlying coverage.",
    "texture_multiply": "Multiply underlying colors by this texture; retain underlying coverage.",
    "texture_screen": "Screen underlying colors with this texture; retain underlying coverage.",
    "texture_contrast": "Imprint brightness as texture contrast; middle gray is neutral. Retain underlying coverage.",
    "alpha_modulate": "Use this object's brightness and opacity to reduce coverage of underlying artwork.",
    "height_modulate": "Use brightness as a height map to shade underlying artwork; flat areas are neutral.",
}


def validate_blend_mode(mode: str) -> str:
    if mode not in OBJECT_BLEND_MODE_IDS:
        raise ValueError(f"Unknown object blend mode: {mode}")
    return mode


def luminance(rgb):
    return rgb[..., 0:1] * .3 + rgb[..., 1:2] * .59 + rgb[..., 2:3] * .11


def blend_rgb(back, front, mode):
    """W3C color blending, plus linear burn/add, on straight RGB in [0, 1]."""
    if mode == "normal":
        return front
    if mode in {"multiply", "texture_multiply"}:
        return back * front
    if mode in {"screen", "texture_screen"}:
        return back + front - back * front
    if mode in {"overlay", "texture_contrast"}:
        return np.where(back <= .5, 2 * back * front, 1 - 2 * (1 - back) * (1 - front))
    if mode == "hard_light":
        return np.where(front <= .5, 2 * back * front, 1 - 2 * (1 - back) * (1 - front))
    if mode == "soft_light":
        d = np.where(back <= .25, ((16 * back - 12) * back + 4) * back, np.sqrt(back))
        return np.where(front <= .5, back - (1 - 2 * front) * back * (1 - back),
                        back + (2 * front - 1) * (d - back))
    if mode == "linear_burn":
        return np.maximum(0, back + front - 1)
    if mode == "linear_dodge":
        return np.minimum(1, back + front)
    if mode == "color_burn":
        return np.where(back >= 1, 1, np.where(front <= 0, 0,
            1 - np.minimum(1, (1 - back) / np.maximum(front, 1e-7))))
    if mode == "color_dodge":
        return np.where(back <= 0, 0, np.where(front >= 1, 1,
            np.minimum(1, back / np.maximum(1 - front, 1e-7))))
    if mode == "darken":
        return np.minimum(back, front)
    if mode == "lighten":
        return np.maximum(back, front)
    if mode == "difference":
        return np.abs(back - front)
    if mode == "exclusion":
        return back + front - 2 * back * front

    def set_lum(rgb, value):
        rgb = rgb + value - luminance(rgb)
        lum = luminance(rgb)
        low = np.minimum(np.minimum(rgb[..., 0:1], rgb[..., 1:2]), rgb[..., 2:3])
        high = np.maximum(np.maximum(rgb[..., 0:1], rgb[..., 1:2]), rgb[..., 2:3])
        rgb = np.where(low < 0, lum + (rgb - lum) * lum / np.maximum(lum - low, 1e-7), rgb)
        return np.where(high > 1, lum + (rgb - lum) * (1 - lum) / np.maximum(high - lum, 1e-7), rgb)

    def sat(rgb):
        return (np.maximum(np.maximum(rgb[..., 0:1], rgb[..., 1:2]), rgb[..., 2:3])
                - np.minimum(np.minimum(rgb[..., 0:1], rgb[..., 1:2]), rgb[..., 2:3]))

    def set_sat(rgb, value):
        low = np.minimum(np.minimum(rgb[..., 0:1], rgb[..., 1:2]), rgb[..., 2:3])
        return (rgb - low) * value / np.maximum(sat(rgb), 1e-7)

    if mode == "luminosity":
        return set_lum(back, luminance(front))
    if mode == "color":
        return set_lum(front, luminance(back))
    if mode == "hue":
        return set_lum(set_sat(front, sat(back)), luminance(back))
    if mode == "saturation":
        return set_lum(set_sat(back, sat(front)), luminance(back))
    raise ValueError(f"Unknown color blend mode: {mode}")


def composite_blend(back, front, mode, *, height_shade=None):
    """Composite float32 premultiplied RGBA; modulation retains backdrop alpha.

    Source alpha controls modulation strength. Transparent source pixels never
    alter the backdrop, and modulation never creates artwork in empty areas.
    """
    sa, da = front[..., 3:4], back[..., 3:4]
    if mode == "normal":
        return front + back * (1 - sa)
    cs = front[..., :3] / np.maximum(sa, 1e-8)
    cb = back[..., :3] / np.maximum(da, 1e-8)
    if mode == "alpha_modulate":
        return back * (1 - sa + sa * luminance(cs))
    if mode in MODULATION_MODES:
        if mode == "luma_modulate":
            blended = cb * luminance(cs)
        elif mode == "height_modulate":
            if height_shade is None:
                raise ValueError("Emboss needs a height-map neighborhood")
            blended = np.clip(cb + height_shade, 0, 1)
        else:
            blended = blend_rgb(cb, luminance(cs) if mode == "texture_contrast" else cs, mode)
        rgb = back[..., :3] * (1 - sa) + blended * sa * da
        alpha = da
    else:
        rgb = front[..., :3] * (1 - da) + back[..., :3] * (1 - sa) + blend_rgb(cb, cs, mode) * sa * da
        alpha = sa + da * (1 - sa)
    return np.concatenate((np.minimum(np.maximum(rgb, 0), alpha), alpha), axis=-1)
