from __future__ import annotations

from pathlib import Path

import pytest

from comic_editor.integrations.blender_process import (
    discover_blender_executables,
    resolve_blender_executable,
)


def _fake_blender(root: Path, version: str) -> Path:
    executable = root / "Blender Foundation" / f"Blender {version}" / "blender.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    return executable.resolve()


def test_blender_override_has_precedence(tmp_path):
    override = tmp_path / "custom" / "blender.exe"
    override.parent.mkdir()
    override.write_bytes(b"")
    _fake_blender(tmp_path, "99.0")

    assert resolve_blender_executable(
        {"BLENDER_EXECUTABLE": str(override)}, [tmp_path]
    ) == override.resolve()


def test_invalid_blender_override_does_not_silently_fall_back(tmp_path):
    _fake_blender(tmp_path, "4.5")
    with pytest.raises(FileNotFoundError, match="BLENDER_EXECUTABLE"):
        resolve_blender_executable(
            {"BLENDER_EXECUTABLE": str(tmp_path / "missing.exe")},
            [tmp_path],
        )


def test_blender_discovery_chooses_newest_semantic_version(tmp_path):
    old = _fake_blender(tmp_path, "4.4")
    newest = _fake_blender(tmp_path, "4.10.2")
    middle = _fake_blender(tmp_path, "4.5.5")

    found = discover_blender_executables([tmp_path], {})

    assert found[:3] == [newest, middle, old]
    assert resolve_blender_executable({}, [tmp_path]) == newest
