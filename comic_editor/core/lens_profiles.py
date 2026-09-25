"""Offline Lensfun profile selection for the Lens Correction modifier.

The bundled data is a small, attributed selection from the Lensfun database.
Imported XML can be embedded in modifier parameters so projects remain portable.
Only geometric distortion is applied here; this is not a camera RAW developer.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping
import xml.etree.ElementTree as ET


DATA_DIRECTORY = Path(__file__).with_name("data") / "lensfun"
BUILTIN_DATABASE = DATA_DIRECTORY / "profiles.xml"
SUPPORTED_MODELS = frozenset({"poly3", "poly5", "ptlens"})
MAX_XML_BYTES = 16 * 1024 * 1024


class LensProfileError(ValueError):
    """A profile file is malformed or contains no supported distortion data."""


@dataclass(frozen=True)
class CameraProfile:
    id: str
    name: str
    maker: str
    model: str
    mount: str
    crop_factor: float


@dataclass(frozen=True)
class LensMeasurement:
    model: str
    focal_length: float
    coefficients: tuple[float, float, float]
    crop_factor: float
    aspect_ratio: float


@dataclass(frozen=True)
class LensProfile:
    id: str
    name: str
    maker: str
    model: str
    mounts: tuple[str, ...]
    crop_factor: float
    aspect_ratio: float
    calibrations: tuple[LensMeasurement, ...]

    @property
    def focal_range(self) -> tuple[float, float]:
        values = [item.focal_length for item in self.calibrations]
        return min(values), max(values)


@dataclass(frozen=True)
class DistortionCalibration:
    """A ready-to-evaluate output-to-input radial map.

    Normalize distance from the image center by half its shorter dimension,
    then multiply by ``radius_scale`` before evaluating the model. Multiply
    the original pixel displacement by the resulting radial scale factor.

    ``poly3`` coefficients are (k1, 0, 0): 1 - k1 + k1*r**2.
    ``poly5`` coefficients are (k1, k2, 0): 1 + k1*r**2 + k2*r**4.
    ``ptlens`` coefficients are (a, b, c): 1-a-b-c + c*r + b*r**2 + a*r**3.
    These map corrected output coordinates to distorted source coordinates.
    """
    model: str
    coefficients: tuple[float, float, float]
    radius_scale: float
    focal_length: float
    calibration_crop_factor: float
    camera_crop_factor: float


@dataclass(frozen=True)
class LensCatalog:
    cameras: tuple[CameraProfile, ...]
    lenses: tuple[LensProfile, ...]
    mount_compatibility: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def camera(self, profile_id: str) -> CameraProfile | None:
        return next((item for item in self.cameras if item.id == profile_id), None)

    def lens(self, profile_id: str) -> LensProfile | None:
        return next((item for item in self.lenses if item.id == profile_id), None)

    def lenses_for_camera(self, camera_id: str = "") -> tuple[LensProfile, ...]:
        """Filter by physical mount; an empty camera permits every profile."""
        if not camera_id:
            return self.lenses
        camera = self.camera(camera_id)
        if camera is None:
            return ()
        compatible = {camera.mount}
        pending = [camera.mount]
        mounts = dict(self.mount_compatibility)
        while pending:
            for name in mounts.get(pending.pop(), ()):
                if name not in compatible:
                    compatible.add(name)
                    pending.append(name)
        return tuple(lens for lens in self.lenses if compatible.intersection(lens.mounts))

    def correction(
        self,
        lens_id: str,
        focal_length: float,
        camera_id: str = "",
        image_aspect: float = 1.5,
    ) -> DistortionCalibration | None:
        lens = self.lens(lens_id)
        if lens is None:
            return None
        camera = self.camera(camera_id)
        if camera_id and (camera is None or lens not in self.lenses_for_camera(camera_id)):
            return None
        focal = _positive(focal_length, "Focal length")
        aspect = _positive(image_aspect, "Image aspect ratio")
        aspect = max(aspect, 1.0 / aspect)
        camera_crop = camera.crop_factor if camera is not None else lens.crop_factor

        # A lens may provide several independent sensor calibrations. Do not
        # interpolate across sensor sizes, aspect ratios, or polynomial models.
        nearest = min(lens.calibrations, key=lambda item: (
            abs(math.log(item.crop_factor / camera_crop)),
            abs(item.aspect_ratio - aspect),
            abs(item.focal_length - focal),
        ))
        measurements = sorted((item for item in lens.calibrations if (
            item.crop_factor == nearest.crop_factor
            and item.aspect_ratio == nearest.aspect_ratio
            and item.model == nearest.model
        )), key=lambda item: item.focal_length)
        low = high = measurements[0]
        for item in measurements:
            if item.focal_length <= focal:
                low = item
            if item.focal_length >= focal:
                high = item
                break
        else:
            high = measurements[-1]
        if focal <= measurements[0].focal_length:
            low = high = measurements[0]
        if high.focal_length == low.focal_length:
            coefficients = low.coefficients
        else:
            fraction = (focal - low.focal_length) / (high.focal_length - low.focal_length)
            coefficients = tuple(a + (b - a) * fraction
                                 for a, b in zip(low.coefficients, high.coefficients))

        # Lensfun's radius unit is half the calibration sensor's short side.
        # Crop factor is diagonal-based, so aspect ratios also matter.
        radius_scale = (nearest.crop_factor / camera_crop
                        * math.sqrt(1.0 + nearest.aspect_ratio ** 2)
                        / math.sqrt(1.0 + aspect ** 2))
        return DistortionCalibration(
            nearest.model, coefficients, radius_scale, focal,
            nearest.crop_factor, camera_crop,
        )


def _positive(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LensProfileError(f"{label} must be a positive number.") from exc
    if not math.isfinite(number) or number <= 0:
        raise LensProfileError(f"{label} must be a positive number.")
    return number


def _aspect(value: str | None) -> float:
    text = str(value or "3:2")
    try:
        if ":" in text:
            left, right = text.split(":", 1)
            number = _positive(left, "Aspect ratio") / _positive(right, "Aspect ratio")
        else:
            number = _positive(text, "Aspect ratio")
    except (ValueError, ZeroDivisionError) as exc:
        raise LensProfileError("Invalid calibration aspect ratio.") from exc
    return max(number, 1.0 / number)


def _text(element: ET.Element, tag: str) -> str:
    # The unlocalized record is what EXIF matching and persistent IDs use.
    candidates = element.findall(tag)
    preferred = next((child for child in candidates if not child.attrib.get("lang")), None)
    if preferred is None:
        preferred = next(iter(candidates), None)
    return "" if preferred is None else str(preferred.text or "").strip()


def _name(maker: str, model: str) -> str:
    brand = maker.split()[0] if maker else ""
    return model if model.casefold().startswith(brand.casefold()) else f"{maker} {model}".strip()


def _identifier(kind: str, *parts: Any) -> str:
    content = "\0".join(str(part) for part in parts)
    return f"{kind}:{hashlib.sha256(content.encode('utf-8')).hexdigest()[:20]}"


@lru_cache(maxsize=12)
def _parse_catalog(xml_text: str) -> LensCatalog:
    if len(xml_text.encode("utf-8")) > MAX_XML_BYTES:
        raise LensProfileError("The lens profile XML is too large (maximum 16 MB).")
    if "<!ENTITY" in xml_text.upper():
        raise LensProfileError("Entity declarations are not supported in lens profile XML.")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise LensProfileError(f"Invalid Lensfun XML: {exc}") from exc
    if root.tag != "lensdatabase":
        raise LensProfileError("Choose a Lensfun XML file with a lensdatabase root.")

    mounts: dict[str, tuple[str, ...]] = {}
    for element in root.findall("mount"):
        name = _text(element, "name")
        if name:
            mounts[name] = tuple(str(item.text or "").strip() for item in element.findall("compat"))
    cameras: dict[str, CameraProfile] = {}
    for element in root.findall("camera"):
        maker, model = _text(element, "maker"), _text(element, "model")
        mount = _text(element, "mount")
        if not model or not mount:
            continue
        crop = _positive(_text(element, "cropfactor") or 1, "Camera crop factor")
        key = _identifier("camera", maker, model, mount, crop)
        cameras[key] = CameraProfile(key, _name(maker, model), maker, model, mount, crop)

    lenses: dict[str, LensProfile] = {}
    for element in root.findall("lens"):
        maker, model = _text(element, "maker"), _text(element, "model")
        if not model:
            continue
        crop = _positive(_text(element, "cropfactor") or 1, "Lens crop factor")
        aspect = _aspect(_text(element, "aspect-ratio") or None)
        lens_mounts = tuple(sorted({str(item.text or "").strip() for item in element.findall("mount")}))
        measurements = []
        for group in element.findall("calibration"):
            group_crop = _positive(group.get("cropfactor", crop), "Calibration crop factor")
            group_aspect = _aspect(group.get("aspect-ratio", str(aspect)))
            for entry in group.findall("distortion"):
                method = str(entry.get("model", "")).lower()
                if method not in SUPPORTED_MODELS:
                    continue
                focal = _positive(entry.get("focal"), "Calibration focal length")
                keys = {"poly3": ("k1",), "poly5": ("k1", "k2"), "ptlens": ("a", "b", "c")}[method]
                try:
                    values = [float(entry.get(key, 0)) for key in keys]
                except (ValueError, TypeError) as exc:
                    raise LensProfileError("Invalid lens distortion coefficient.") from exc
                if not all(math.isfinite(value) for value in values):
                    raise LensProfileError("Lens distortion coefficients must be finite.")
                values.extend([0.0] * (3 - len(values)))
                measurements.append(LensMeasurement(method, focal, tuple(values), group_crop, group_aspect))
        if not measurements:
            continue
        key = _identifier("lens", maker, model, "/".join(lens_mounts), crop, aspect)
        # Include the calibration sensor size where one lens has multiple entries.
        name = _name(maker, model)
        lenses[key] = LensProfile(key, name, maker, model, lens_mounts, crop, aspect, tuple(measurements))
    if not lenses:
        raise LensProfileError("No poly3, poly5, or PTLens distortion profiles were found in this file.")
    return LensCatalog(
        tuple(sorted(cameras.values(), key=lambda item: item.name.casefold())),
        tuple(sorted(lenses.values(), key=lambda item: (item.name.casefold(), item.crop_factor))),
        tuple(sorted(mounts.items())),
    )


@lru_cache(maxsize=1)
def _builtin_catalog() -> LensCatalog:
    return _parse_catalog(BUILTIN_DATABASE.read_text(encoding="utf-8"))


def load_lens_catalog(
    xml_path: str | Path | None = None, *, xml_text: str | None = None,
) -> LensCatalog:
    """Load bundled or imported Lensfun data without network or native libraries."""
    if xml_text:
        return _parse_catalog(xml_text)
    if xml_path:
        path = Path(xml_path)
        if path.stat().st_size > MAX_XML_BYTES:
            raise LensProfileError("The lens profile XML is too large (maximum 16 MB).")
        return _parse_catalog(path.read_text(encoding="utf-8-sig"))
    return _builtin_catalog()


def correction_for_params(
    params: Mapping[str, Any], image_aspect: float = 1.5,
) -> DistortionCalibration | None:
    """Resolve persisted modifier settings; stale/malformed profiles are identity."""
    lens_id = str(params.get("lens_profile", ""))
    if not lens_id:
        return None
    try:
        catalog = load_lens_catalog(
            params.get("profile_path") or None,
            xml_text=params.get("profile_xml") or None,
        )
        return catalog.correction(
            lens_id, params.get("focal_length", 50.0),
            str(params.get("camera_profile", "")), image_aspect,
        )
    except (LensProfileError, OSError, TypeError):
        return None
