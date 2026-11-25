"""Tests for macOS native-actor loaded inspection via lsof."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from ..fakes import STARTED_PID, FakeHost, build_apply, check, result, smoke


class LsofHost(FakeHost):
    """Fake host that records lsof invocations and returns configured output."""

    def __init__(self, tmp_path: Path, home: Path) -> None:
        super().__init__(tmp_path, home)
        self.platform = "launchd"
        self.unit = "com.hyprial.hyprial.daemon.0123456789ab"
        self.lsof_result: Any = None
        self.lsof_calls: list[list[str]] = []

    def dispatch(self, command: list[str]) -> Any:
        if command[0] == "lsof":
            self.lsof_calls.append(command)
            if self.lsof_result is not None:
                return self.lsof_result(command)
            return result(command, 0, "")
        return super().dispatch(command)


def _setup_native_apply(
    tmp_path: Path, *, tool_subpath: str = "tool"
) -> tuple[Any, LsofHost, Path]:
    instance, host = build_apply(
        tmp_path,
        LsofHost,
        extra=["--require-native-actor"],
    )
    tool_dir = tmp_path / tool_subpath
    so_path = (
        tool_dir
        / "lib"
        / "python3.12"
        / "site-packages"
        / "hyprial"
        / "_core"
        / "actors.abi3.so"
    )
    so_path.parent.mkdir(parents=True, exist_ok=True)
    so_path.write_bytes(b"\xcf\xfa\xed\xfe")
    instance.tool_dir = tool_dir
    instance.new_info = {
        "version": "0.2.0",
        "nativeFiles": ["hyprial/_core/actors.abi3.so"],
    }
    return instance, host, so_path


def test_darwin_lsof_exact_match_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host, so_path = _setup_native_apply(tmp_path)
    host.lsof_result = lambda cmd: result(
        cmd,
        0,
        f"p{STARTED_PID}\nfcwd\nn{tmp_path}\nftxt\nn{so_path}\n",
    )

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert host.lsof_calls == [["lsof", "-nP", "-p", str(STARTED_PID), "-Fn"]]
    loaded_check = check(instance, "post.native-actor.loaded")
    assert loaded_check.status == smoke.PASS
    assert loaded_check.data.get("loaded") == [str(so_path)]
    assert str(so_path) in loaded_check.detail


def test_darwin_lsof_symlink_realpath_match_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    real_root = tmp_path / "private_var"
    link_root = tmp_path / "var_link"
    real_root.mkdir()
    link_root.symlink_to(real_root)
    instance, host, so_path = _setup_native_apply(
        tmp_path, tool_subpath="var_link/tool"
    )
    real_so = so_path.resolve()
    assert str(so_path) != str(real_so)
    host.lsof_result = lambda cmd: result(
        cmd,
        0,
        f"p{STARTED_PID}\nftxt\nn{real_so}\n",
    )

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    loaded_check = check(instance, "post.native-actor.loaded")
    assert loaded_check.status == smoke.PASS
    assert loaded_check.data.get("loaded") == [str(so_path)]


def test_darwin_lsof_partial_filename_spoof_fails_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host, so_path = _setup_native_apply(tmp_path)
    spoof_suffix = f"{so_path}.bak"
    spoof_prefix = str(so_path.parent / f"fake_{so_path.name}")
    host.lsof_result = lambda cmd: result(
        cmd,
        0,
        f"p{STARTED_PID}\nftxt\nn{spoof_suffix}\nftxt\nn{spoof_prefix}\n",
    )

    rc = instance.run_apply()

    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert host.installs == ["0.2.0", "0.1.0"]
    loaded_check = check(instance, "post.native-actor.loaded")
    assert loaded_check.status == smoke.FAIL
    assert "not mapped" in loaded_check.detail


def test_darwin_lsof_rc0_without_match_fails_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host, _ = _setup_native_apply(tmp_path)
    host.lsof_result = lambda cmd: result(
        cmd,
        0,
        f"p{STARTED_PID}\nfcwd\nn/tmp\nftxt\nn/usr/lib/libSystem.B.dylib\n",
    )

    rc = instance.run_apply()

    assert rc == smoke.EXIT_FAILED
    assert instance.result == "ROLLED_BACK"
    assert host.installs == ["0.2.0", "0.1.0"]
    assert check(instance, "post.native-actor.loaded").status == smoke.FAIL


@pytest.mark.parametrize(
    ("stdout", "expected_status"),
    [
        ("n{installed}\n", smoke.BLOCKED),
        ("p999\nn{installed}\n", smoke.BLOCKED),
        ("n{installed}\np456\nn/other.so\n", smoke.FAIL),
        ("p456\nn/other.so\np999\nn{installed}\n", smoke.FAIL),
    ],
)
def test_darwin_lsof_only_accepts_target_process_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected_status: str,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host, so_path = _setup_native_apply(tmp_path)
    host.lsof_result = lambda cmd: result(
        cmd,
        0,
        stdout.format(installed=so_path),
    )

    instance.run_apply()

    assert check(instance, "post.native-actor.loaded").status == expected_status


def test_darwin_lsof_missing_binary_is_blocked_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host, _ = _setup_native_apply(tmp_path)
    host.lsof_result = lambda cmd: result(
        cmd, 127, "", "FileNotFoundError: [Errno 2] No such file or directory: 'lsof'"
    )

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert instance.result == "PASS_BLOCKED"
    assert host.installs == ["0.2.0"]
    loaded_check = check(instance, "post.native-actor.loaded")
    assert loaded_check.status == smoke.BLOCKED
    assert "rc=127" in loaded_check.detail


def test_darwin_lsof_timeout_is_blocked_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host, _ = _setup_native_apply(tmp_path)
    host.lsof_result = lambda cmd: smoke.CommandResult(
        cmd, 124, "", "timed out", timed_out=True
    )

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert instance.result == "PASS_BLOCKED"
    assert host.installs == ["0.2.0"]
    loaded_check = check(instance, "post.native-actor.loaded")
    assert loaded_check.status == smoke.BLOCKED
    assert "timed out" in loaded_check.detail


def test_darwin_lsof_timeout_with_partial_match_is_blocked_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host, so_path = _setup_native_apply(tmp_path)
    host.lsof_result = lambda cmd: smoke.CommandResult(
        cmd, 124, f"p{STARTED_PID}\nftxt\nn{so_path}\n", "timed out", timed_out=True
    )

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert instance.result == "PASS_BLOCKED"
    assert host.installs == ["0.2.0"]
    loaded_check = check(instance, "post.native-actor.loaded")
    assert loaded_check.status == smoke.BLOCKED
    assert "timed out" in loaded_check.detail


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr"),
    [
        (1, "", "lsof: permission denied"),
        (0, "", ""),
        (0, f"p{STARTED_PID}\n", ""),
        (0, "ftxt\nn/tmp/something.so\n", ""),
        (0, "p999999\nftxt\nn/tmp/something.so\n", ""),
    ],
)
def test_darwin_lsof_unusable_output_is_blocked_without_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rc: int,
    stdout: str,
    stderr: str,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host, _ = _setup_native_apply(tmp_path)
    host.lsof_result = lambda cmd: result(cmd, rc, stdout, stderr)

    exit_code = instance.run_apply()

    assert exit_code == smoke.EXIT_OK
    assert instance.result == "PASS_BLOCKED"
    assert host.installs == ["0.2.0"]
    assert check(instance, "post.native-actor.loaded").status == smoke.BLOCKED


def test_darwin_lsof_rc1_with_matching_n_line_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    instance, host, so_path = _setup_native_apply(tmp_path)
    host.lsof_result = lambda cmd: result(
        cmd,
        1,
        f"p{STARTED_PID}\nftxt\nn{so_path}\n",
        "lsof: WARNING: can't stat() devfs file system",
    )

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert instance.result == smoke.PASS
    assert host.installs == ["0.2.0"]
    loaded_check = check(instance, "post.native-actor.loaded")
    assert loaded_check.status == smoke.PASS
    assert loaded_check.data.get("loaded") == [str(so_path)]


def test_unknown_platform_records_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "freebsd14")
    instance, host, _ = _setup_native_apply(tmp_path)

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert host.lsof_calls == []
    loaded_check = check(instance, "post.native-actor.loaded")
    assert loaded_check.status == smoke.NOT_RUN
    assert "Linux and macOS only" in loaded_check.detail


def test_linux_proc_maps_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    instance, host, so_path = _setup_native_apply(tmp_path)
    original_read_text = Path.read_text
    target_maps = Path(f"/proc/{STARTED_PID}/maps")

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target_maps:
            return f"7f0000-7f1000 r-xp 00000000 08:01 1 {so_path}\n"
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    rc = instance.run_apply()

    assert rc == smoke.EXIT_OK
    assert host.lsof_calls == []
    loaded_check = check(instance, "post.native-actor.loaded")
    assert loaded_check.status == smoke.PASS
    assert str(so_path) in loaded_check.detail
