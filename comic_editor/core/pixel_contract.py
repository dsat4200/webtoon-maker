"""Persisted document color and precision policy, independent of the renderer."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PixelContract:
    version: int = 1
    working_space: str = 'srgb'
    precision: str = 'uint8'
    alpha: str = 'premultiplied'
    display: str = 'sRGB'
    view: str = 'Standard'
    export_space: str = 'srgb'
    ocio_config: str = ''

    def __post_init__(self):
        if type(self.version) is not int or self.version not in (1, 2):
            raise ValueError('Unsupported pixel contract version')
        if any(not isinstance(value, str) for value in (
                self.working_space, self.precision, self.alpha, self.display,
                self.view, self.export_space, self.ocio_config)):
            raise ValueError('Pixel contract settings must be strings')
        if self.alpha != 'premultiplied':
            raise ValueError('Unsupported pixel contract')
        if self.precision not in ('uint8', 'float16', 'float32'):
            raise ValueError('Unsupported working pixel precision')
        if self.version == 1 and (self.precision != 'uint8' or self.working_space != 'srgb'
                or self.export_space != 'srgb' or self.display != 'sRGB'
                or self.view != 'Standard' or self.ocio_config):
            raise ValueError('Legacy artwork requires the original sRGB byte contract')
        if self.version == 2 and self.precision == 'uint8':
            raise ValueError('The floating-point contract requires float precision')
        if not self.working_space or not self.export_space or not self.display or not self.view:
            raise ValueError('Color spaces, display, and view must be specified')
        if not self.ocio_config and (self.working_space not in ('srgb', 'linear_srgb')
                                    or self.export_space not in ('srgb', 'linear_srgb')
                                    or self.display != 'sRGB' or self.view != 'Standard'):
            raise ValueError('Custom color spaces require an OpenColorIO configuration')
        object.__setattr__(self, '_signature', tuple(asdict(self).values()))

    @property
    def floating(self):
        return self.version >= 2

    @property
    def image_format(self):
        from PySide6.QtGui import QImage
        return {'uint8': QImage.Format_ARGB32_Premultiplied,
                'float16': QImage.Format_RGBA16FPx4_Premultiplied,
                'float32': QImage.Format_RGBA32FPx4_Premultiplied}[self.precision]

    @property
    def signature(self):
        return self._signature

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, payload=None):
        if payload is None:
            return LEGACY_PIXELS
        if not isinstance(payload, dict):
            raise ValueError('Pixel contract must be an object')
        try:
            return cls(**payload)
        except TypeError as error:
            raise ValueError('Unsupported pixel contract fields') from error


LEGACY_PIXELS = PixelContract()
FLOAT_PIXELS = PixelContract(version=2, precision='float32')
