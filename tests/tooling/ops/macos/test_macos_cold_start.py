"""Tests for macOS isolated preflight temp root and cold-start socket guard."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ..fakes import build_apply, check, result, smoke
from _dev_upgrade_smoke import preflight as preflight_module


@contextlib.contextmanager
def _short_root(prefix: str = "hsm-t-") -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _make_root_for_socket_bytes(
    base_dir: Path, target_bytes: int, *, use_utf8: bool = False
) -> Path:
    suffix = "/cold-state/daemon.sock"
    base_resolved = base_dir.resolve()
    prefix_bytes = len(str(base_resolved).encode("utf-8")) + 1
    needed = target_bytes - prefix_bytes - len(suffix.encode("utf-8"))
    assert needed > 0, f"base_dir too long for target_bytes={target_bytes}"
    if use_utf8:
        unit = "中"
        unit_bytes = len(unit.encode("utf-8"))
        count = needed // unit_bytes
        rem = needed % unit_bytes
        name = (unit * count) + ("a" * rem)
    else:
        name = "x" * needed
    root = base_dir / name
    root.mkdir(parents=True, exist_ok=True)
    actual_bytes = len(str(root.resolve() / "cold-state" / "daemon.sock").encode("utf-8"))
    assert actual_bytes == target_bytes
    return root


def _fake_popen_auth_blocked(
    log_holder: dict[str, Any], marker: str = "BOOTSTRAP_AUTH_OUTCOME_UNKNOWN"
) -> Any:
    class FakePopen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            log_holder["called"] = True
            log_holder["argv"] = argv
            log_holder["kwargs"] = kwargs
            self.pid = 9999
            self.returncode = 1
            stdout_handle = kwargs.get("stdout")
            if stdout_handle is not None:
                stdout_handle.write(f"{marker}\n".encode("utf-8"))
                stdout_handle.flush()

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

    return FakePopen


def _fake_popen_running(log_holder: dict[str, Any]) -> Any:
    class FakePopen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            log_holder["called"] = True
            log_holder["argv"] = argv
            log_holder["kwargs"] = kwargs
            log_holder["process"] = self
            self.pid = 4321
            self.returncode: int | None = None
            self.waited = False
            self.terminated = False
            stdout_handle = kwargs.get("stdout")
            if stdout_handle is not None:
                stdout_handle.flush()

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            self.waited = True
            self.returncode = 0
            return self.returncode

    return FakePopen


def _fake_popen_failure(log_holder: dict[str, Any]) -> Any:
    class FakePopen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            log_holder["called"] = True
            log_holder["argv"] = argv
            log_holder["kwargs"] = kwargs
            self.pid = 9998
            self.returncode = 1
            stdout_handle = kwargs.get("stdout")
            if stdout_handle is not None:
                stdout_handle.write(
                    (
                        ("ordinary failure output\n" * 100)
                        + "visit http://127.0.0.1:9999/secret-path\n"
                    ).encode("utf-8")
                )
                stdout_handle.flush()

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

    return FakePopen


@pytest.mark.parametrize(
    ("platform_name", "expected_kwargs"),
    [
        ("darwin", {"prefix": "hsm-", "dir": "/tmp"}),
        ("linux", {"prefix": "hyprial-smoke-isolated-"}),
    ],
)
def test_isolated_preflight_mkdtemp_prefix_and_dir_by_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    expected_kwargs: dict[str, str],
) -> None:
    monkeypatch.setattr(sys, "platform", platform_name)
    instance, host = build_apply(tmp_path, mode="preflight")
    mkdtemp_calls: list[dict[str, Any]] = []
    iso_dir = tmp_path / "iso"
    iso_dir.mkdir()
    fake_cli = iso_dir / "bin" / "hyprial"
    fake_cli.parent.mkdir(parents=True, exist_ok=True)
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)

    def fake_mkdtemp(**kwargs: Any) -> str:
        mkdtemp_calls.append(kwargs)
        return str(iso_dir)

    original_dispatch = host.dispatch

    def dispatch(command: list[str]) -> Any:
        if command[0] == str(fake_cli) and command[1:3] == ["version", "--json"]:
            return result(command, 0, {"packageVersion": "0.2.0"})
        return original_dispatch(command)

    host.dispatch = dispatch
    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)

    instance.isolated_preflight()

    assert mkdtemp_calls == [expected_kwargs]
    assert check(instance, "isolated.version").status == smoke.PASS


def test_isolated_preflight_blocks_cleanly_when_root_allocation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, _ = build_apply(tmp_path, mode="preflight")
    monkeypatch.setattr(
        preflight_module.tempfile,
        "mkdtemp",
        lambda **kwargs: (_ for _ in ()).throw(OSError("read-only")),
    )
    monkeypatch.setattr(instance, "prepare", lambda: True)

    exit_code = instance.run()

    assert exit_code == smoke.EXIT_OK
    workdir_check = check(instance, "isolated.workdir")
    assert workdir_check.status == smoke.BLOCKED
    assert workdir_check.data == {
        "dir": "/tmp",
        "error": "OSError: read-only",
    }
    assert (instance.artifacts_dir / "summary.json").exists()


@pytest.mark.parametrize(
    ("platform_name", "limit", "over_bytes"),
    [
        ("darwin", 103, 104),
        ("linux", 107, 108),
    ],
)
def test_cold_start_blocks_when_socket_exceeds_platform_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    limit: int,
    over_bytes: int,
) -> None:
    monkeypatch.setattr(sys, "platform", platform_name)
    instance, _ = build_apply(
        tmp_path, mode="preflight", extra=["--cold-start-timeout", "5"]
    )
    popen_state: dict[str, Any] = {"called": False}
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_auth_blocked(popen_state))
    base = Path("/tmp") if Path("/tmp").is_dir() else tmp_path
    scratch = Path(tempfile.mkdtemp(prefix="hsm-t-", dir=str(base)))
    try:
        root = _make_root_for_socket_bytes(scratch, over_bytes)
        binary = root / "hyprial"

        instance.cold_start(binary, root)

        c = check(instance, "isolated.cold-start")
        assert c.status == smoke.BLOCKED
        assert str(over_bytes) in c.detail
        assert str(limit) in c.detail
        raw_socket = root / "cold-state" / "daemon.sock"
        resolved_socket = (root / "cold-state").resolve() / "daemon.sock"
        raw_bytes = len(str(raw_socket).encode("utf-8"))
        resolved_bytes = len(str(resolved_socket).encode("utf-8"))
        offending_socket = raw_socket if raw_bytes >= resolved_bytes else resolved_socket
        assert c.data == {
            "socketPath": str(offending_socket),
            "socketPathBytes": max(raw_bytes, resolved_bytes),
            "rawSocketPath": str(raw_socket),
            "resolvedSocketPath": str(resolved_socket),
            "rawSocketPathBytes": raw_bytes,
            "resolvedSocketPathBytes": resolved_bytes,
            "socketPathLimit": limit,
        }
        assert not (root / "cold-home").exists()
        assert not (root / "cold-state").exists()
        assert not (root / "cold-user").exists()
        assert not (root / "cold-home" / "node-id").exists()
        assert popen_state["called"] is False
        assert not (root / "cold-start.log").exists()
    finally:
        for child in scratch.rglob("*"):
            if child.is_file():
                child.unlink(missing_ok=True)
        shutil.rmtree(scratch, ignore_errors=True)


def test_cold_start_blocks_on_resolved_symlink_path_exceeding_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, _ = build_apply(
        tmp_path, mode="preflight", extra=["--cold-start-timeout", "5"]
    )
    with _short_root("hsm-s-") as scratch:
        long_real = _make_root_for_socket_bytes(scratch, 110)
        short_link = scratch / "s"
        short_link.symlink_to(long_real)
        raw_bytes = len(str(short_link / "cold-state" / "daemon.sock").encode("utf-8"))
        assert raw_bytes <= 103

        instance.cold_start(short_link / "hyprial", short_link)

        c = check(instance, "isolated.cold-start")
        assert c.status == smoke.BLOCKED
        assert c.data["socketPathBytes"] == 110
        assert c.data["socketPath"] == str((short_link / "cold-state").resolve() / "daemon.sock")
        assert c.data["rawSocketPath"] == str(short_link / "cold-state" / "daemon.sock")
        assert c.data["resolvedSocketPath"] == str((short_link / "cold-state").resolve() / "daemon.sock")
        assert c.data["rawSocketPathBytes"] == raw_bytes
        assert c.data["resolvedSocketPathBytes"] == 110
        assert c.data["socketPathLimit"] == 103
        assert not (short_link / "cold-home").exists()
        assert not (short_link / "cold-state").exists()
        assert not (short_link / "cold-user").exists()
        assert not (short_link / "cold-home" / "node-id").exists()
        assert not (short_link / "cold-start.log").exists()


def test_cold_start_blocks_on_raw_symlink_path_exceeding_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, _ = build_apply(
        tmp_path, mode="preflight", extra=["--cold-start-timeout", "5"]
    )
    with _short_root("hsm-r-") as scratch:
        short_real = scratch / "r"
        short_real.mkdir()
        long_name = "l" * 90
        long_link = scratch / long_name
        long_link.symlink_to(short_real)
        raw_socket = long_link / "cold-state" / "daemon.sock"
        raw_bytes = len(str(raw_socket).encode("utf-8"))
        resolved_bytes = len(
            str((short_real / "cold-state").resolve() / "daemon.sock").encode("utf-8")
        )
        assert raw_bytes > 103
        assert resolved_bytes <= 103

        instance.cold_start(long_link / "hyprial", long_link)

        c = check(instance, "isolated.cold-start")
        assert c.status == smoke.BLOCKED
        assert c.data["socketPathBytes"] == raw_bytes
        assert c.data["socketPath"] == str(raw_socket)
        assert c.data["rawSocketPath"] == str(raw_socket)
        assert c.data["resolvedSocketPath"] == str(
            (short_real / "cold-state").resolve() / "daemon.sock"
        )
        assert c.data["rawSocketPathBytes"] == raw_bytes
        assert c.data["resolvedSocketPathBytes"] == resolved_bytes
        assert c.data["socketPathLimit"] == 103
        assert not (long_link / "cold-home").exists()
        assert not (long_link / "cold-state").exists()
        assert not (long_link / "cold-user").exists()
        assert not (long_link / "cold-home" / "node-id").exists()
        assert not (long_link / "cold-start.log").exists()


def test_cold_start_measures_utf8_byte_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, _ = build_apply(
        tmp_path, mode="preflight", extra=["--cold-start-timeout", "5"]
    )
    with _short_root("hsm-u-") as scratch:
        root = _make_root_for_socket_bytes(scratch, 105, use_utf8=True)
        resolved_sock = str((root / "cold-state").resolve() / "daemon.sock")
        assert len(resolved_sock) <= 103
        assert len(resolved_sock.encode("utf-8")) == 105

        instance.cold_start(root / "hyprial", root)

        c = check(instance, "isolated.cold-start")
        assert c.status == smoke.BLOCKED
        assert c.data["socketPathBytes"] == 105
        assert c.data["socketPathLimit"] == 103
        assert not (root / "cold-home").exists()
        assert not (root / "cold-state").exists()
        assert not (root / "cold-user").exists()
        assert not (root / "cold-home" / "node-id").exists()
        assert not (root / "cold-start.log").exists()


def test_isolated_preflight_keep_workdir_over_limit_leaves_no_cold_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host = build_apply(
        tmp_path,
        mode="preflight",
        extra=["--cold-start-timeout", "5", "--keep-workdir"],
    )
    popen_state: dict[str, Any] = {"called": False}
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_auth_blocked(popen_state))
    with _short_root("hsm-k-") as scratch:
        root = _make_root_for_socket_bytes(scratch, 110)
        fake_cli = root / "bin" / "hyprial"
        fake_cli.parent.mkdir(parents=True, exist_ok=True)
        fake_cli.write_text("#!/bin/sh\nexit 0\n")
        fake_cli.chmod(0o755)
        original_dispatch = host.dispatch

        def dispatch(command: list[str]) -> Any:
            if command[0] == str(fake_cli) and command[1:3] == ["version", "--json"]:
                return result(command, 0, {"packageVersion": "0.2.0"})
            return original_dispatch(command)

        host.dispatch = dispatch
        monkeypatch.setattr(preflight_module, "allocate_isolated_root", lambda: root)

        instance.isolated_preflight()

        assert instance.isolated["kept"] == str(root)
        assert root.exists()
        c = check(instance, "isolated.cold-start")
        assert c.status == smoke.BLOCKED
        assert popen_state["called"] is False
        assert not (root / "cold-home").exists()
        assert not (root / "cold-state").exists()
        assert not (root / "cold-user").exists()
        assert not (root / "cold-home" / "node-id").exists()
        assert not (root / "cold-start.log").exists()


@pytest.mark.parametrize(
    ("platform_name", "exact_limit"),
    [
        ("darwin", 103),
        ("linux", 107),
    ],
)
def test_cold_start_exact_limit_proceeds_to_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    exact_limit: int,
) -> None:
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.delenv("HYPRIAL_NETWORK_ISOLATED", raising=False)
    instance, _ = build_apply(
        tmp_path, mode="preflight", extra=["--cold-start-timeout", "5"]
    )
    popen_state: dict[str, Any] = {"called": False}
    with _short_root("hsm-e-") as scratch:
        root = _make_root_for_socket_bytes(scratch, exact_limit)
        assert len(str(root / "cold-state" / "daemon.sock").encode("utf-8")) <= exact_limit

        fake_popen = _fake_popen_auth_blocked(popen_state)

        def popen(argv: list[str], **kwargs: Any) -> Any:
            node_id = root / "cold-home" / "node-id"
            assert node_id.exists()
            assert node_id.read_text(encoding="utf-8") == "smoke-cold-start\n"
            assert stat.S_IMODE(node_id.stat().st_mode) == 0o600
            assert kwargs["env"]["HYPRIAL_NETWORK_ISOLATED"] == "1"
            assert "HYPRIAL_NETWORK_ISOLATED" not in os.environ
            return fake_popen(argv, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", popen)

        instance.cold_start(root / "hyprial", root)

        assert popen_state["called"] is True
        assert "HYPRIAL_NETWORK_ISOLATED" not in os.environ
        node_id = root / "cold-home" / "node-id"
        assert node_id.read_text(encoding="utf-8") == "smoke-cold-start\n"
        assert stat.S_IMODE(node_id.stat().st_mode) == 0o600
        assert popen_state["kwargs"]["env"]["HYPRIAL_NETWORK_ISOLATED"] == "1"
        c = check(instance, "isolated.cold-start")
        assert c.status == smoke.BLOCKED
        assert "device authorization" in c.detail


@pytest.mark.parametrize("marker", smoke.AUTH_MARKERS)
def test_cold_start_blocks_on_every_auth_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, _ = build_apply(
        tmp_path, mode="preflight", extra=["--cold-start-timeout", "5"]
    )
    popen_state: dict[str, Any] = {"called": False}
    monkeypatch.setattr(
        subprocess, "Popen", _fake_popen_auth_blocked(popen_state, marker=marker)
    )
    with _short_root("hsm-m-") as root:
        instance.cold_start(root / "hyprial", root)

    assert popen_state["called"] is True
    c = check(instance, "isolated.cold-start")
    assert c.status == smoke.BLOCKED
    assert "device authorization" in c.detail


def test_cold_start_passes_when_bootstrap_daemon_becomes_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host = build_apply(
        tmp_path, mode="preflight", extra=["--cold-start-timeout", "5"]
    )
    popen_state: dict[str, Any] = {"called": False}
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_running(popen_state))
    host.daemon_status_override = lambda command: result(
        command,
        0,
        {"running": True, "pid": 4321, "version": "0.2.0", "phase": "bootstrap"},
    )

    def killpg(_pgid: int, _signal: int) -> None:
        popen_state["process"].terminated = True
        popen_state["process"].returncode = 0

    monkeypatch.setattr(preflight_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(preflight_module.os, "killpg", killpg)
    with _short_root("hsm-p-") as root:
        instance.cold_start(root / "hyprial", root)

    c = check(instance, "isolated.cold-start")
    assert c.status == smoke.PASS
    assert "daemon ready pid=4321 version=0.2.0" in c.detail
    assert popen_state["process"].terminated is True
    assert popen_state["process"].waited is True


def test_cold_start_fail_records_bounded_redacted_output_excerpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, _ = build_apply(
        tmp_path, mode="preflight", extra=["--cold-start-timeout", "5"]
    )
    popen_state: dict[str, Any] = {"called": False}
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_failure(popen_state))
    with _short_root("hsm-f-") as root:
        instance.cold_start(root / "hyprial", root)

    c = check(instance, "isolated.cold-start")
    assert c.status == smoke.FAIL
    excerpt = c.data["outputExcerpt"]
    assert len(excerpt) <= 500
    assert "http://127.0.0.1:9999/secret-path" not in excerpt
