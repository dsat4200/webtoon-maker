"""Bilinear float resampling without a byte or straight-alpha intermediate."""
import numpy as np
from PIL import Image


def resize_rgba(pixels, size):
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError('Float image dimensions must be positive')
    if pixels.shape[:2] == (height, width):
        return np.array(pixels, dtype=np.float32, copy=True)
    result = np.empty((height, width, 4), dtype=np.float32)
    for channel in range(4):
        plane = Image.fromarray(np.asarray(pixels[..., channel], dtype=np.float32))
        result[..., channel] = np.asarray(plane.resize(size, Image.Resampling.BILINEAR))
    return result
