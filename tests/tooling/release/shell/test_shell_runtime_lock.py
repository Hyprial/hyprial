"""shell-6.md §D.4 6c-1: the python-build-standalone lock of the macOS package."""

from __future__ import annotations

import copy
import json
import re

import pytest

from tests.tooling.release.shell.support import LOCK, load

PBS_PREFIX = "https://github.com/astral-sh/python-build-standalone/releases/download/"


def _lock() -> dict:
    return json.loads(LOCK.read_text(encoding="utf-8"))


def test_lock_names_a_verified_pbs_asset_per_target() -> None:
    stage = load("stage_runtime")
    lock = stage.load_lock(LOCK)
    assert set(lock["targets"]) == {"aarch64-apple-darwin", "x86_64-unknown-linux-gnu"}
    for target, entry in lock["targets"].items():
        assert entry["url"].startswith(f"{PBS_PREFIX}{lock['pbsTag']}/")
        asset = entry["url"].rsplit("/", 1)[1].replace("%2B", "+")
        assert asset == f"cpython-{lock['python']}+{lock['pbsTag']}-{target}-install_only_stripped.tar.gz"
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        assert entry["size"] > 0
        assert entry["wheelPlatform"]


def test_macos_entry_is_the_arm64_wheel_tag() -> None:
    entry = _lock()["targets"]["aarch64-apple-darwin"]
    assert entry["wheelPlatform"] == "macosx_13_0_arm64"
    assert entry["pythonPlatform"] == "aarch64-apple-darwin"


def test_no_windows_target() -> None:
    names = " ".join(_lock()["targets"]).lower()
    assert "windows" not in names and "msvc" not in names


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ((), "unexpected"),
        (("targets", "aarch64-apple-darwin"), "unexpected"),
    ],
)
def test_unknown_keys_fail(path: tuple[str, ...], value: str) -> None:
    stage = load("stage_runtime")
    lock = copy.deepcopy(_lock())
    node = lock
    for key in path:
        node = node[key]
    node["extra"] = value
    with pytest.raises(stage.StageError, match="extra"):
        stage.validate_lock(lock)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sha256", "ABC", "sha256"),
        ("url", "https://example.invalid/cpython.tar.gz", "url"),
        ("size", 0, "size"),
        ("wheelPlatform", "", "wheelPlatform"),
    ],
)
def test_malformed_entries_fail(field: str, value: object, message: str) -> None:
    stage = load("stage_runtime")
    lock = copy.deepcopy(_lock())
    lock["targets"]["aarch64-apple-darwin"][field] = value
    with pytest.raises(stage.StageError, match=message):
        stage.validate_lock(lock)


def test_asset_of_another_tag_or_target_fails() -> None:
    stage = load("stage_runtime")
    lock = copy.deepcopy(_lock())
    macos = lock["targets"]["aarch64-apple-darwin"]
    linux = lock["targets"]["x86_64-unknown-linux-gnu"]
    macos["url"] = linux["url"]
    with pytest.raises(stage.StageError, match="url"):
        stage.validate_lock(lock)
