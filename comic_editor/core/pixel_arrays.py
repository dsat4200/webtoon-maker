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


def native_rgba_pixels(image):
    """Read source channels before integer alpha conversion can round them.

    Qt's integer-to-float format conversion may premultiply and unpremultiply
    through integer storage even for a straight destination. At very low alpha
    this destroys otherwise valid source RGB. Recognized channel layouts are
    read directly; packed/other layouts retain Qt's native wide conversion.
    The returned float array is detached and the flag describes association.
    """
    from PySide6.QtGui import QImage

    if image.isNull():
        return np.empty((0, 0, 4), np.float32), False
    format = image.format()
    premultiplied = format.name.endswith('_Premultiplied')
    if format == QImage.Format_Grayscale16:
        rows = np.frombuffer(image.constBits(), np.uint16).reshape(
            image.height(), image.bytesPerLine() // 2)
        values = np.ones((image.height(), image.width(), 4), np.float32)
        gray = rows[:, :image.width()].astype(np.float32) / np.float32(65535.)
        values[..., :3] = gray[..., None]
        return values, False
    layouts = {
        QImage.Format_RGBA8888: (np.uint8, 255.),
        QImage.Format_RGBA8888_Premultiplied: (np.uint8, 255.),
        QImage.Format_RGBX8888: (np.uint8, 255.),
        QImage.Format_RGBA64: (np.uint16, 65535.),
        QImage.Format_RGBA64_Premultiplied: (np.uint16, 65535.),
        QImage.Format_RGBX64: (np.uint16, 65535.),
        QImage.Format_RGBA16FPx4: (np.float16, None),
        QImage.Format_RGBA16FPx4_Premultiplied: (np.float16, None),
        QImage.Format_RGBX16FPx4: (np.float16, None),
        QImage.Format_RGBA32FPx4: (np.float32, None),
        QImage.Format_RGBA32FPx4_Premultiplied: (np.float32, None),
        QImage.Format_RGBX32FPx4: (np.float32, None),
    }
    if format in layouts:
        dtype, maximum = layouts[format]
        rows = np.frombuffer(image.constBits(), dtype).reshape(
            image.height(), image.bytesPerLine() // np.dtype(dtype).itemsize)
        values = np.array(rows[:, :image.width()*4].reshape(
            image.height(), image.width(), 4), dtype=np.float32, copy=True)
        if maximum is not None:
            np.divide(values, np.float32(maximum), out=values)
        if format.name.startswith('Format_RGBX'):
            values[..., 3] = 1.
        return values, premultiplied
    if format in (QImage.Format_ARGB32, QImage.Format_ARGB32_Premultiplied,
                  QImage.Format_RGB32):
        rows = np.frombuffer(image.constBits(), np.uint32).reshape(
            image.height(), image.bytesPerLine() // 4)[:, :image.width()]
        values = np.empty((image.height(), image.width(), 4), np.float32)
        for channel, shift in enumerate((16, 8, 0, 24)):
            values[..., channel] = (rows >> shift) & 255
        np.divide(values, np.float32(255.), out=values)
        return values, premultiplied
    # Preserve Qt's normalization of packed wide channels and grayscale. These
    # opaque formats cannot lose color through small coverage multiplication.
    if image_has_high_precision(image):
        source = image.convertToFormat(QImage.Format_RGBA32FPx4_Premultiplied)
        return native_rgba_pixels(source)[0], True
    source = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied
                                   if premultiplied else QImage.Format_RGBA8888)
    return native_rgba_pixels(source)


def truncated_bytes(pixels):
    # Preserve the input's arithmetic dtype and the reference truncation rule.
    scaled = np.multiply(pixels, 255.)
    np.clip(scaled, 0., 255., out=scaled)
    return np.ascontiguousarray(scaled.astype(np.uint8))
