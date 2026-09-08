from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "blender_extension" / "webtoon_comic_views"


def _blender_executables() -> list[str]:
    candidates = [
        os.environ.get("BLENDER_EXECUTABLE", "").strip(),
        os.environ.get("BLENDER_52_EXECUTABLE", "").strip(),
        os.environ.get("BLENDER_45_EXECUTABLE", "").strip(),
        r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
        r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
        shutil.which("blender") or "",
    ]
    found = {}
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            path = Path(candidate).resolve()
            found.setdefault(os.path.normcase(str(path)), str(path))
    return list(found.values())


@pytest.fixture(params=_blender_executables() or [None], ids=lambda path: (
    Path(path).parent.name if path else "not-installed"
))
def blender_executable(request) -> str:
    if request.param is None:
        pytest.skip("Blender 5.2 or 4.5 is not installed")
    return request.param


@pytest.fixture
def blender_environment(tmp_path) -> dict[str, str]:
    """Keep every subprocess away from the user's installed add-ons and files."""
    environment = dict(os.environ)
    for name in ("config", "scripts", "datafiles", "extensions"):
        directory = tmp_path / name
        directory.mkdir()
        environment[f"BLENDER_USER_{name.upper()}"] = str(directory)
    environment["WEBTOON_EXTENSION_ROOT"] = str(EXTENSION)
    environment["WEBTOON_COMIC_VIEW_FRAME_ROOT"] = str(tmp_path / "frames")
    return environment


def test_extension_manifest_declares_supported_blender_windows_and_io_permissions():
    manifest = tomllib.loads(
        (EXTENSION / "blender_manifest.toml").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == "1.0.0"
    assert manifest["id"] == "webtoon_comic_views"
    assert manifest["version"] == "0.6.1"
    assert manifest["blender_version_min"] == "4.5.0"
    assert manifest["platforms"] == ["windows-x64"]
    assert "network" in manifest["permissions"]
    assert "files" in manifest["permissions"]


@pytest.mark.parametrize("probe,marker", [
    ("_blender_comic_views_probe.py", "WEBTOON_COMIC_VIEWS_PROBE_OK"),
    ("_blender_action_slots_probe.py", "WEBTOON_ACTION_SLOTS_PROBE_OK"),
    ("_blender_bake_bookkeeping_probe.py", "WEBTOON_BAKE_BOOKKEEPING_PROBE_OK"),
    ("_blender_thumbnails_probe.py", "WEBTOON_THUMBNAILS_PROBE_OK"),
])
def test_blender_background_state_bridge_and_publication_probe(
    blender_executable, blender_environment, probe, marker,
):
    result = subprocess.run(
        [
            blender_executable, "--factory-startup", "--background",
            "--python-exit-code", "1", "--python",
            str(Path(__file__).with_name(probe)),
        ],
        cwd=ROOT,
        env=blender_environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    output = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, output
    assert marker in output
    for line in output.splitlines():
        if line.startswith("WEBTOON_BAKE_TIMING "):
            print(f"{Path(blender_executable).parent.name}: {line}")


def test_blender_extension_package_validates(blender_executable, blender_environment):
    result = subprocess.run(
        [
            blender_executable, "--factory-startup", "--command", "extension",
            "validate", str(EXTENSION),
        ],
        cwd=ROOT,
        env=blender_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_extension_installs_and_enables_from_restricted_registration_context(
    tmp_path, blender_executable, blender_environment,
):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    build = subprocess.run(
        [
            blender_executable, "--factory-startup", "--command", "extension",
            "build", "--source-dir", str(EXTENSION),
            "--output-dir", str(package_dir),
        ],
        cwd=ROOT,
        env=blender_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert build.returncode == 0, build.stdout + "\n" + build.stderr
    manifest = tomllib.loads(
        (EXTENSION / "blender_manifest.toml").read_text(encoding="utf-8")
    )
    package = package_dir / f"webtoon_comic_views-{manifest['version']}.zip"
    assert package.is_file()

    install = subprocess.run(
        [
            blender_executable, "--factory-startup", "--command", "extension",
            "install-file", "-r", "user_default", "-e", str(package),
        ],
        cwd=ROOT,
        env=blender_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert install.returncode == 0, install.stdout + "\n" + install.stderr

    launch = subprocess.run(
        [
            blender_executable, "--background", "--python-expr",
            "import bpy, importlib; print('WCV_ENABLED', "
            "[k for k in bpy.context.preferences.addons.keys() "
            "if 'webtoon_comic_views' in k]); "
            "addon = importlib.import_module('bl_ext.user_default.webtoon_comic_views'); "
            "print('WCV_ADDON_FILE', addon.__file__)",
        ],
        cwd=ROOT,
        env=blender_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = launch.stdout + "\n" + launch.stderr
    assert launch.returncode == 0, output
    assert "bl_ext.user_default.webtoon_comic_views" in output
    assert str(tmp_path / "extensions" / "user_default" / "webtoon_comic_views") in output


@pytest.mark.blender_live
@pytest.mark.parametrize("probe,marker", [
    ("_blender_live_gpu_probe.py", "WEBTOON_LIVE_GPU_PROBE_OK"),
    ("_blender_thumbnails_probe.py", "WEBTOON_THUMBNAILS_PROBE_OK"),
])
def test_opt_in_live_gpu_integration_is_explicitly_enabled(
    blender_executable, blender_environment, probe, marker,
):
    if os.environ.get("WEBTOON_BLENDER_LIVE_TEST") != "1":
        pytest.skip("set WEBTOON_BLENDER_LIVE_TEST=1 for the visible GPU test")
    script = Path(__file__).with_name(probe)
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    command = [
        blender_executable, "--factory-startup", "--python-exit-code", "1",
        "--python", str(script),
    ]
    if probe == "_blender_thumbnails_probe.py":
        # Exercise real icons after the window exists and always close this
        # isolated Blender process, including when a probe assertion fails.
        command[-2:] = [
            "--python-expr",
            "import bpy, runpy, sys, traceback\n"
            "sys.stdout.reconfigure(line_buffering=True)\n"
            "def run_thumbnail_probe():\n"
            "    try:\n"
            f"        runpy.run_path({str(script)!r})\n"
            "    except Exception:\n"
            "        traceback.print_exc()\n"
            "    finally:\n"
            "        bpy.ops.wm.quit_blender()\n"
            "bpy.app.timers.register(run_thumbnail_probe, first_interval=1.0)\n",
        ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=blender_environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        startupinfo=startupinfo,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert marker in result.stdout
