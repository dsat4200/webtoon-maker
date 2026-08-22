from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "blender_extension" / "webtoon_comic_views"


def _blender_45() -> str | None:
    configured = os.environ.get("BLENDER_45_EXECUTABLE", "").strip()
    candidates = [
        configured,
        shutil.which("blender") or "",
        r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def test_extension_manifest_declares_blender_45_windows_and_io_permissions():
    manifest = tomllib.loads(
        (EXTENSION / "blender_manifest.toml").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "1.0.0"
    assert manifest["id"] == "webtoon_comic_views"
    assert manifest["version"] == "0.5.1"
    assert manifest["blender_version_min"] == "4.5.0"
    assert manifest["platforms"] == ["windows-x64"]
    assert "network" in manifest["permissions"]
    assert "files" in manifest["permissions"]


def test_blender_background_state_bridge_and_publication_probe(tmp_path):
    executable = _blender_45()
    if executable is None:
        pytest.skip("Blender 4.5 LTS is not installed")
    environment = dict(os.environ)
    environment["WEBTOON_EXTENSION_ROOT"] = str(EXTENSION)
    environment["WEBTOON_COMIC_VIEW_FRAME_ROOT"] = str(tmp_path / "frames")
    result = subprocess.run(
        [
            executable, "--factory-startup", "--background", "--python",
            str(Path(__file__).with_name("_blender_comic_views_probe.py")),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    output = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, output
    assert "WEBTOON_COMIC_VIEWS_PROBE_OK" in output


def test_blender_extension_package_validates():
    executable = _blender_45()
    if executable is None:
        pytest.skip("Blender 4.5 LTS is not installed")
    result = subprocess.run(
        [
            executable, "--factory-startup", "--command", "extension",
            "validate", str(EXTENSION),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_extension_installs_and_enables_from_restricted_registration_context(
    tmp_path,
):
    executable = _blender_45()
    if executable is None:
        pytest.skip("Blender 4.5 LTS is not installed")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    build = subprocess.run(
        [
            executable, "--factory-startup", "--command", "extension",
            "build", "--source-dir", str(EXTENSION),
            "--output-dir", str(package_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert build.returncode == 0, build.stdout + "\n" + build.stderr
    package = package_dir / "webtoon_comic_views-0.5.1.zip"
    assert package.is_file()

    environment = dict(os.environ)
    for name in ("config", "scripts", "datafiles"):
        directory = tmp_path / name
        directory.mkdir()
        environment[f"BLENDER_USER_{name.upper()}"] = str(directory)
    install = subprocess.run(
        [
            executable, "--factory-startup", "--command", "extension",
            "install-file", "-r", "user_default", "-e", str(package),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert install.returncode == 0, install.stdout + "\n" + install.stderr

    launch = subprocess.run(
        [
            executable, "--background", "--python-expr",
            "import bpy; print('WCV_ENABLED', "
            "[k for k in bpy.context.preferences.addons.keys() "
            "if 'webtoon_comic_views' in k])",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = launch.stdout + "\n" + launch.stderr
    assert launch.returncode == 0, output
    assert "bl_ext.user_default.webtoon_comic_views" in output


@pytest.mark.blender_live
def test_opt_in_live_gpu_integration_is_explicitly_enabled():
    if os.environ.get("WEBTOON_BLENDER_LIVE_TEST") != "1":
        pytest.skip("set WEBTOON_BLENDER_LIVE_TEST=1 for the visible GPU test")
    executable = _blender_45()
    if executable is None:
        pytest.skip("Blender 4.5 LTS is not installed")
    script = Path(__file__).with_name("_blender_live_gpu_probe.py")
    environment = dict(os.environ)
    environment["WEBTOON_EXTENSION_ROOT"] = str(EXTENSION)
    result = subprocess.run(
        [executable, "--factory-startup", "--python", str(script)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "WEBTOON_LIVE_GPU_PROBE_OK" in result.stdout
