"""Owned pixel conversions without redundant full-frame temporaries."""
import numpy as np


def image_has_high_precision(image):
    """Recognize wide channels even in packed 32-bit or single-band storage."""
    from PySide6.QtGui import QImage
    return (image.depth() > 32 or image.format() in (
        QImage.Format_Grayscale16, QImage.Format_RGB30, QImage.Format_BGR30,
        QImage.Format_A2RGB30_Premultiplied, QImage.Format_A2BGR30_Premultiplied,
    ))


def normalized_bytes(pixels):
    result = np.array(pixels, dtype=np.float32, copy=True, order='C')
    np.divide(result, np.float32(255.), out=result)
    return result


def truncated_bytes(pixels):
    # Preserve the input's arithmetic dtype and the reference truncation rule.
    scaled = np.multiply(pixels, 255.)
    np.clip(scaled, 0., 255., out=scaled)
    return np.ascontiguousarray(scaled.astype(np.uint8))
