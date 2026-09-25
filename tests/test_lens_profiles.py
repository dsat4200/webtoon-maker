from __future__ import annotations

import math

import pytest

from comic_editor.core.lens_profiles import (
    BUILTIN_DATABASE, LensProfileError, correction_for_params, load_lens_catalog,
)


EXAMPLE_XML = """<lensdatabase version="2">
  <mount><name>Body</name><compat>Lens</compat></mount>
  <camera><maker>Example</maker><model>Camera</model><mount>Body</mount><cropfactor>2</cropfactor></camera>
  <lens><maker>Example</maker><model>Zoom</model><mount>Lens</mount><cropfactor>1</cropfactor>
    <calibration>
      <distortion model="ptlens" focal="20" a="0.01" b="0.02" c="-0.03"/>
      <distortion model="ptlens" focal="40" a="0.03" b="0.04" c="-0.05"/>
    </calibration>
  </lens>
</lensdatabase>"""


def test_bundled_profiles_are_real_calibrations_and_have_license():
    catalog = load_lens_catalog()
    assert len(catalog.cameras) == 4
    assert len(catalog.lenses) == 9
    lens = next(item for item in catalog.lenses if item.model == "Canon EF-S 18-55mm f/3.5-5.6 IS II")
    at_18 = catalog.correction(lens.id, 18)
    assert at_18.model == "ptlens"
    assert at_18.coefficients == pytest.approx((0.02504, -0.06883, 0.01502))
    assert at_18.radius_scale == pytest.approx(1)
    assert BUILTIN_DATABASE.with_name("ATTRIBUTION.md").is_file()
    assert BUILTIN_DATABASE.with_name("COPYING.CC_BY-SA_3.0").is_file()


def test_camera_mount_filters_lenses_including_compatible_mounts():
    catalog = load_lens_catalog()
    full_frame = next(item for item in catalog.cameras if item.model == "Canon EOS 5D Mark III")
    cropped = next(item for item in catalog.cameras if item.model == "Canon EOS 80D")
    full_names = {item.model for item in catalog.lenses_for_camera(full_frame.id)}
    crop_names = {item.model for item in catalog.lenses_for_camera(cropped.id)}
    assert "Canon EF 24-70mm f/2.8L USM" in full_names
    assert "Canon EF-S 18-55mm f/3.5-5.6 IS II" not in full_names
    assert full_names < crop_names
    assert all("Canon" in name for name in crop_names)
    assert catalog.lenses_for_camera() == catalog.lenses
    assert catalog.lenses_for_camera("missing") == ()


def test_focal_interpolation_and_nearest_endpoint():
    catalog = load_lens_catalog(xml_text=EXAMPLE_XML)
    lens = catalog.lenses[0]
    assert catalog.correction(lens.id, 30).coefficients == pytest.approx((0.02, 0.03, -0.04))
    assert catalog.correction(lens.id, 10).coefficients == pytest.approx((0.01, 0.02, -0.03))
    assert catalog.correction(lens.id, 70).coefficients == pytest.approx((0.03, 0.04, -0.05))


def test_selected_camera_crop_and_image_aspect_change_radial_units():
    catalog = load_lens_catalog(xml_text=EXAMPLE_XML)
    lens, camera = catalog.lenses[0], catalog.cameras[0]
    correction = catalog.correction(lens.id, 30, camera.id)
    assert correction.radius_scale == pytest.approx(0.5)
    assert correction.calibration_crop_factor == 1
    assert correction.camera_crop_factor == 2
    square = catalog.correction(lens.id, 30, camera.id, image_aspect=1)
    assert square.radius_scale == pytest.approx(0.5 * math.sqrt(3.25 / 2))
    portrait = catalog.correction(lens.id, 30, camera.id, image_aspect=2 / 3)
    assert portrait.radius_scale == pytest.approx(correction.radius_scale)


def test_imported_profile_is_portable_and_retains_stable_ids(tmp_path):
    path = tmp_path / "profile.xml"
    path.write_text(EXAMPLE_XML, encoding="utf-8")
    file_catalog = load_lens_catalog(path)
    embedded_catalog = load_lens_catalog(xml_text=EXAMPLE_XML)
    assert file_catalog == embedded_catalog
    params = {
        "lens_profile": file_catalog.lenses[0].id,
        "camera_profile": file_catalog.cameras[0].id,
        "focal_length": 30,
        "profile_xml": path.read_text(encoding="utf-8"),
    }
    path.unlink()
    assert correction_for_params(params).coefficients == pytest.approx((0.02, 0.03, -0.04))


@pytest.mark.parametrize("method, attributes, coefficients", [
    ("poly3", 'k1="0.04"', (0.04, 0, 0)),
    ("poly5", 'k1="0.04" k2="-0.01"', (0.04, -0.01, 0)),
    ("ptlens", 'a="0.04" b="-0.01" c="0.03"', (0.04, -0.01, 0.03)),
])
def test_imports_all_supported_polynomial_models(method, attributes, coefficients):
    xml = f'''<lensdatabase><lens><maker>Example</maker><model>Prime</model>
    <mount>Lens</mount><cropfactor>1.5</cropfactor><calibration>
    <distortion model="{method}" focal="50" {attributes}/>
    </calibration></lens></lensdatabase>'''
    catalog = load_lens_catalog(xml_text=xml)
    result = catalog.correction(catalog.lenses[0].id, 50)
    assert result.model == method
    assert result.coefficients == pytest.approx(coefficients)


def test_different_sensor_calibration_groups_are_never_mixed():
    xml = EXAMPLE_XML.replace('</calibration>', '''</calibration>
    <calibration cropfactor="2" aspect-ratio="4:3">
      <distortion model="poly3" focal="20" k1="0.05"/>
      <distortion model="poly3" focal="40" k1="0.15"/>
    </calibration>''')
    catalog = load_lens_catalog(xml_text=xml)
    result = catalog.correction(catalog.lenses[0].id, 30, catalog.cameras[0].id, 4 / 3)
    assert result.model == "poly3"
    assert result.coefficients == pytest.approx((0.1, 0, 0))
    assert result.radius_scale == pytest.approx(1)


@pytest.mark.parametrize("xml", [
    '<invalid/>', '<lensdatabase>',
    EXAMPLE_XML.replace('a="0.01"', 'a="NaN"'),
    EXAMPLE_XML.replace('<cropfactor>2</cropfactor>', '<cropfactor>0</cropfactor>'),
    '<!DOCTYPE lensdatabase [<!ENTITY test "data">]><lensdatabase/>',
    EXAMPLE_XML.replace('model="ptlens"', 'model="unsupported"'),
])
def test_invalid_profiles_report_a_useful_error(xml):
    with pytest.raises(LensProfileError):
        load_lens_catalog(xml_text=xml)


def test_stale_or_invalid_modifier_settings_render_as_identity():
    catalog = load_lens_catalog(xml_text=EXAMPLE_XML)
    valid = {"lens_profile": catalog.lenses[0].id, "profile_xml": EXAMPLE_XML}
    assert correction_for_params({}) is None
    assert correction_for_params({"lens_profile": "missing"}) is None
    assert correction_for_params({**valid, "profile_xml": "invalid"}) is None
    assert correction_for_params({**valid, "focal_length": float("nan")}) is None
    assert correction_for_params({**valid, "camera_profile": "missing"}) is None
