"""Binary location and version verification for the tailcat sidecar."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.network.tailcat import (
    BINARY_ENV,
    SIDECAR_BINARY_NAME,
    TAILCAT_COMMIT,
    TailcatSidecarError,
    locate_tailcat_sidecar,
    sidecar_binary_path,
    verify_tailcat_sidecar,
)
from hyprial.daemon.impl.network.tailcat import binary as binary_module
from hyprial.daemon.impl.network.tailcat.binary import wheel_binary_path


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _version_script(tmp_path: Path, *, commit: str = TAILCAT_COMMIT) -> Path:
    record = json.dumps({"v": 6, "sidecar": "0.1.0", "tailcat": commit})
    return _script(tmp_path / "hyprial-tailcat", f"echo '{record}'\n")


def test_sidecar_binary_path_is_the_conventional_bin_dir(tmp_path: Path) -> None:
    assert sidecar_binary_path(tmp_path) == (
        tmp_path / "bin" / SIDECAR_BINARY_NAME
    )


def test_console_children_are_hidden_only_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A console child of the console-less daemon must not open a window."""

    monkeypatch.setattr(binary_module.sys, "platform", "win32")
    monkeypatch.setattr(
        binary_module.subprocess, "CREATE_NO_WINDOW", 0x0800_0000, raising=False
    )
    assert binary_module.no_console_window_kwargs() == {
        "creationflags": 0x0800_0000
    }

    monkeypatch.setattr(binary_module.sys, "platform", "linux")
    assert binary_module.no_console_window_kwargs() == {}


def test_locate_prefers_the_explicit_environment_path(tmp_path: Path) -> None:
    explicit = _script(tmp_path / "elsewhere", "exit 0\n")
    found = locate_tailcat_sidecar(tmp_path, {BINARY_ENV: str(explicit)})
    assert found == explicit


def test_locate_does_not_bypass_an_explicit_but_broken_pin(tmp_path: Path) -> None:
    """An explicit ``HYPRIAL_TAILCAT_BINARY`` is a pin, not a hint."""

    (tmp_path / "bin").mkdir(parents=True)
    _script(tmp_path / "bin" / SIDECAR_BINARY_NAME, "exit 0\n")
    broken = tmp_path / "missing-sidecar"
    with pytest.raises(TailcatSidecarError) as raised:
        locate_tailcat_sidecar(tmp_path, {BINARY_ENV: str(broken)})
    assert raised.value.code == "SIDECAR_MISSING"
    assert str(broken) in str(raised.value)


def test_wheel_binary_path_sits_under_the_package_root(tmp_path: Path) -> None:
    assert wheel_binary_path(tmp_path) == (
        tmp_path / "_bin" / SIDECAR_BINARY_NAME
    )


@pytest.mark.parametrize(
    ("platform_name", "expected_name", "mode"),
    [
        ("win32", "hyprial-tailcat.exe", 0o644),
        ("linux", "hyprial-tailcat", 0o755),
    ],
)
@pytest.mark.parametrize("source", ["wheel", "home"])
def test_locate_uses_the_platform_binary_name_for_both_install_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    expected_name: str,
    mode: int,
    source: str,
) -> None:
    monkeypatch.setattr(sys, "platform", platform_name)
    package_root = tmp_path / "package"
    home = tmp_path / "home"
    wheel_path = wheel_binary_path(package_root)
    home_path = sidecar_binary_path(home)
    expected = (
        package_root / "_bin" / expected_name
        if source == "wheel"
        else home / "bin" / expected_name
    )
    candidate = wheel_path if source == "wheel" else home_path

    assert candidate == expected
    candidate.parent.mkdir(parents=True)
    candidate.write_text("sidecar", encoding="utf-8")
    candidate.chmod(mode)
    monkeypatch.setattr(
        binary_module,
        "wheel_binary_path",
        lambda: wheel_path if source == "wheel" else tmp_path / "missing-wheel",
    )

    assert locate_tailcat_sidecar(home, {}) == candidate


def test_locate_prefers_the_wheel_copy_over_home_bin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wheel install outranks a stale ``$HYPRIAL_HOME/bin`` copy."""

    (tmp_path / "bin").mkdir(parents=True)
    installed = _script(tmp_path / "bin" / SIDECAR_BINARY_NAME, "exit 0\n")
    shipped = _script(tmp_path / "wheel-copy", "exit 0\n")
    monkeypatch.setattr(
        binary_module, "wheel_binary_path", lambda *args: shipped
    )
    assert locate_tailcat_sidecar(tmp_path, {}) == shipped
    assert shipped != installed


def test_locate_still_honours_an_explicit_pin_over_the_wheel_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = _script(tmp_path / "elsewhere", "exit 0\n")
    shipped = _script(tmp_path / "wheel-copy", "exit 0\n")
    monkeypatch.setattr(
        binary_module, "wheel_binary_path", lambda *args: shipped
    )
    assert locate_tailcat_sidecar(tmp_path, {BINARY_ENV: str(explicit)}) == explicit


def test_locate_falls_back_to_home_bin(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir(parents=True)
    installed = _script(tmp_path / "bin" / SIDECAR_BINARY_NAME, "exit 0\n")
    assert locate_tailcat_sidecar(tmp_path, {}) == installed


@pytest.mark.parametrize("environ", [{}, {BINARY_ENV: ""}])
def test_locate_missing_is_loud(tmp_path: Path, environ: dict[str, str]) -> None:
    with pytest.raises(TailcatSidecarError) as raised:
        locate_tailcat_sidecar(tmp_path, environ)
    assert raised.value.code == "SIDECAR_MISSING"


def test_locate_refuses_a_non_executable_candidate(tmp_path: Path) -> None:
    plain = tmp_path / "hyprial-tailcat"
    plain.write_text("not executable", encoding="utf-8")
    with pytest.raises(TailcatSidecarError) as raised:
        locate_tailcat_sidecar(tmp_path, {BINARY_ENV: str(plain)})
    assert raised.value.code == "SIDECAR_MISSING"


def test_verify_returns_the_version_record(tmp_path: Path) -> None:
    binary = _version_script(tmp_path)
    record = verify_tailcat_sidecar(tmp_path, {BINARY_ENV: str(binary)})
    assert record == {"v": 6, "sidecar": "0.1.0", "tailcat": TAILCAT_COMMIT}


def test_verify_rejects_a_different_tailcat_commit(tmp_path: Path) -> None:
    binary = _version_script(tmp_path, commit="0" * 40)
    with pytest.raises(TailcatSidecarError) as raised:
        verify_tailcat_sidecar(tmp_path, {BINARY_ENV: str(binary)})
    assert raised.value.code == "SIDECAR_INVALID"
    assert TAILCAT_COMMIT in str(raised.value)


def test_verify_rejects_non_json_output(tmp_path: Path) -> None:
    binary = _script(tmp_path / "hyprial-tailcat", "echo 'not json'\n")
    with pytest.raises(TailcatSidecarError) as raised:
        verify_tailcat_sidecar(tmp_path, {BINARY_ENV: str(binary)})
    assert raised.value.code == "SIDECAR_INVALID"


def test_verify_rejects_a_nonzero_version_exit(tmp_path: Path) -> None:
    binary = _script(tmp_path / "hyprial-tailcat", "exit 1\n")
    with pytest.raises(TailcatSidecarError) as raised:
        verify_tailcat_sidecar(tmp_path, {BINARY_ENV: str(binary)})
    assert raised.value.code == "SIDECAR_INVALID"


@pytest.mark.parametrize('version', [2, 5, 6.0])
def test_verify_rejects_a_protocol_version_mismatch(tmp_path: Path, version) -> None:
    record = json.dumps({"v": version, "sidecar": "0.1.0", "tailcat": TAILCAT_COMMIT})
    binary = _script(tmp_path / "hyprial-tailcat", f"echo '{record}'\n")
    with pytest.raises(TailcatSidecarError) as raised:
        verify_tailcat_sidecar(tmp_path, {BINARY_ENV: str(binary)})
    assert raised.value.code == "SIDECAR_INVALID"
