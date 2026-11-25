# Decision (2026-11-23, review A-1/A-2): Windows reads the account home from
# USERPROFILE and ignores HOME, and it cannot delete a file that is still open.
# These tests simulate Windows with an ``os`` proxy and drive the root
# conftest's isolated-home context directly, so they assert the environment
# keys and the release-before-delete order without a Windows runner. Real
# Windows behavior of either property stays UNKNOWN until a real run.
"""Simulated-Windows regression for the isolated home's profile keys and teardown order."""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

import tests.conftest as root_conftest
from tests.support.runtime import environment
from tests.tooling.isolation.windows._windows_os import WindowsOs

DECOY = "C:\\Users\\real-account"


def _inside(path: str, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def test_isolated_environment_points_every_windows_profile_key_inside_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(environment, "os", WindowsOs())
    for key in environment.WINDOWS_PROFILE_KEYS:
        monkeypatch.setenv(key, DECOY)
    env = environment.isolated_environment(tmp_path)
    for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        assert _inside(env[key], tmp_path), (key, env[key])
        assert Path(env[key]).is_dir(), key
    assert env["HOMEDRIVE"] + env["HOMEPATH"] == env["USERPROFILE"] == env["HOME"]
    environment.assert_root_local_environment(tmp_path, env)


def test_isolated_environment_leaves_windows_profile_keys_alone_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in environment.WINDOWS_PROFILE_KEYS:
        monkeypatch.setenv(key, DECOY)
    env = environment.isolated_environment(tmp_path)
    assert {key: env[key] for key in environment.WINDOWS_PROFILE_KEYS} == dict.fromkeys(
        environment.WINDOWS_PROFILE_KEYS, DECOY,
    )


def _run_isolated_home(monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, bool]]) -> dict[str, str]:
    seen: dict[str, str] = {}
    roots: list[Path] = []
    monkeypatch.setattr(
        root_conftest, "set_default_registry",
        lambda registry: events.append(("registry", registry is None and roots[0].exists())),
    )
    monkeypatch.setattr(
        root_conftest, "gc",
        types.SimpleNamespace(collect=lambda *args: events.append(("collect", roots[0].exists()))),
    )
    inner = pytest.MonkeyPatch()
    try:
        with root_conftest.isolated_hyprial_home(inner):
            roots.append(Path(os.environ["HYPRIAL_HOME"]).parent)
            seen = {key: os.environ.get(key, "") for key in (*environment.WINDOWS_PROFILE_KEYS, "HOME")}
        assert not roots[0].exists()
    finally:
        inner.undo()
    seen["root"] = str(roots[0])
    return seen


def test_isolated_home_rewrites_windows_profile_keys_in_process_and_releases_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(environment, "os", WindowsOs())
    monkeypatch.setattr(root_conftest, "os", WindowsOs())
    for key in environment.WINDOWS_PROFILE_KEYS:
        monkeypatch.setenv(key, DECOY)
    events: list[tuple[str, bool]] = []
    seen = _run_isolated_home(monkeypatch, events)
    root = Path(seen["root"])
    for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        assert _inside(seen[key], root), (key, seen[key])
    assert seen["HOMEDRIVE"] + seen["HOMEPATH"] == seen["USERPROFILE"] == seen["HOME"]
    assert events == [("registry", True), ("collect", True)]
    assert all(os.environ[key] == DECOY for key in environment.WINDOWS_PROFILE_KEYS)


def test_isolated_home_keeps_posix_teardown_and_profile_keys_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in environment.WINDOWS_PROFILE_KEYS:
        monkeypatch.setenv(key, DECOY)
    events: list[tuple[str, bool]] = []
    seen = _run_isolated_home(monkeypatch, events)
    assert events == []
    assert all(seen[key] == DECOY for key in environment.WINDOWS_PROFILE_KEYS)
