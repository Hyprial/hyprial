# Decision record (test_spawn_detached_daemon, W1-MANYSYS 2026-11-21):
# - Verifies spawn_detached_daemon across POSIX session isolation and Windows
#   up-front Job breakaway probing (_windows_current_job_allows_breakaway):
#   (a) parent not in a Job -> single Popen without CREATE_BREAKAWAY_FROM_JOB,
#   (b) parent in a Job permitting breakaway -> CREATE_BREAKAWAY_FROM_JOB added,
#   (c) parent in a Job forbidding breakaway -> single Popen without breakaway,
#   (d) plain PermissionError (no winerror), target executable/directory ACL
#       denial (even with winerror == 5), and FileNotFoundError -> never retried
#       and propagated verbatim; nested outer-Job WinError 5 on an accessible
#       target still falls back cleanly.
"""Tests for ``spawn_detached_daemon`` across POSIX and Windows."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.kernel import spawn_detached_daemon
from hyprial.kernel.impl.manysys.process import spawn as spawn_mod

_DETACHED_FLAGS = 0x00000200 | 0x00000008 | 0x08000000
_BREAKAWAY_DETACHED_FLAGS = _DETACHED_FLAGS | 0x01000000


def test_posix_spawn_detached_daemon_sets_new_session_and_close_fds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[Any, dict[str, Any]]] = []
    fake_proc = SimpleNamespace(pid=1234)
    monkeypatch.setattr(spawn_mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        spawn_mod,
        "subprocess",
        SimpleNamespace(
            Popen=lambda args, **kwargs: recorded.append((args, kwargs)) or fake_proc
        ),
    )

    result = spawn_detached_daemon(["hyprial", "daemon", "run"])
    assert result is fake_proc
    assert recorded == [
        (
            ["hyprial", "daemon", "run"],
            {"close_fds": True, "start_new_session": True},
        )
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX getsid check")
def test_posix_spawn_detached_daemon_creates_real_session_leader() -> None:
    child = spawn_detached_daemon(
        [sys.executable, "-c", "import os, sys; sys.stdout.write(str(os.getsid(0)))"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        out, _ = child.communicate(timeout=5.0)
        assert child.returncode == 0
        assert int(out.decode("utf-8").strip()) == child.pid
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2.0)


@pytest.mark.parametrize(
    ("in_job_flag", "limit_flags", "expected"),
    [
        (0, 0, (False, False)),
        (1, 0x00002000, (True, False)),
        (1, 0x00002000 | 0x00000800, (True, True)),
        (1, 0x00001000, (True, True)),
    ],
)
def test_windows_current_job_allows_breakaway_inspects_kernel32_job_limits(
    monkeypatch: pytest.MonkeyPatch,
    in_job_flag: int,
    limit_flags: int,
    expected: tuple[bool, bool],
) -> None:
    def fake_is_process_in_job(_proc: int, _job: object, out_ptr: object) -> int:
        out_ptr._obj.value = in_job_flag  # type: ignore[attr-defined]
        return 1

    def fake_query_job(
        job_handle: object,
        info_class: int,
        info_ptr: object,
        info_size: int,
        _ret_len: object,
    ) -> int:
        assert job_handle is None
        assert info_class == 9
        assert info_size == ctypes.sizeof(spawn_mod._JobObjectExtendedLimitInformation)
        info_ptr._obj.BasicLimitInformation.LimitFlags = limit_flags  # type: ignore[attr-defined]
        return 1

    fake_kernel32 = SimpleNamespace(
        GetCurrentProcess=lambda: -1,
        IsProcessInJob=fake_is_process_in_job,
        QueryInformationJobObject=fake_query_job,
    )
    monkeypatch.setattr(spawn_mod, "_load_windows_kernel32", lambda: fake_kernel32)

    assert spawn_mod._windows_current_job_allows_breakaway() == expected


def test_windows_spawn_not_in_job_spawns_once_without_breakaway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    fake_proc = SimpleNamespace(pid=2001)
    monkeypatch.setattr(spawn_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        spawn_mod, "_windows_current_job_allows_breakaway", lambda: (False, False)
    )
    monkeypatch.setattr(
        spawn_mod,
        "subprocess",
        SimpleNamespace(Popen=lambda _args, **kwargs: calls.append(kwargs) or fake_proc),
    )

    result = spawn_detached_daemon(
        ["hyprial.exe", "daemon", "run"],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert result is fake_proc
    assert calls == [
        {
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
            "creationflags": _DETACHED_FLAGS,
        }
    ]


def test_windows_spawn_in_breakaway_allowed_job_attaches_breakaway_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    fake_proc = SimpleNamespace(pid=2002)
    monkeypatch.setattr(spawn_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        spawn_mod, "_windows_current_job_allows_breakaway", lambda: (True, True)
    )
    monkeypatch.setattr(
        spawn_mod,
        "subprocess",
        SimpleNamespace(Popen=lambda _args, **kwargs: calls.append(kwargs) or fake_proc),
    )

    result = spawn_detached_daemon(
        ["hyprial.exe", "daemon", "run"],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert result is fake_proc
    assert calls == [
        {
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
            "creationflags": _BREAKAWAY_DETACHED_FLAGS,
        }
    ]


def test_windows_spawn_in_breakaway_forbidden_job_spawns_once_without_breakaway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    fake_proc = SimpleNamespace(pid=2003)
    monkeypatch.setattr(spawn_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        spawn_mod, "_windows_current_job_allows_breakaway", lambda: (True, False)
    )
    monkeypatch.setattr(
        spawn_mod,
        "subprocess",
        SimpleNamespace(
            Popen=lambda _args, **kwargs: calls.append(kwargs["creationflags"])
            or fake_proc
        ),
    )

    result = spawn_detached_daemon(["hyprial.exe", "daemon", "run"])
    assert result is fake_proc
    assert calls == [_DETACHED_FLAGS]


def test_windows_spawn_nested_job_winerror_5_falls_back_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    fake_proc = SimpleNamespace(pid=2004)

    def fake_popen(_args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["creationflags"])
        if len(calls) == 1:
            err = OSError("Nested ancestor job denies breakaway")
            err.winerror = 5  # type: ignore[attr-defined]
            raise err
        return fake_proc

    monkeypatch.setattr(spawn_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        spawn_mod, "_windows_current_job_allows_breakaway", lambda: (True, True)
    )
    monkeypatch.setattr(spawn_mod, "subprocess", SimpleNamespace(Popen=fake_popen))

    result = spawn_detached_daemon(["hyprial.exe", "daemon", "run"])
    assert result is fake_proc
    assert calls == [_BREAKAWAY_DETACHED_FLAGS, _DETACHED_FLAGS]


def test_windows_spawn_never_retries_plain_permission_error_or_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawn_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        spawn_mod, "_windows_current_job_allows_breakaway", lambda: (True, True)
    )

    perm_calls: list[int] = []

    def raise_plain_permission(_args: Any, **kwargs: Any) -> Any:
        perm_calls.append(kwargs["creationflags"])
        raise PermissionError("Access is denied without winerror")

    monkeypatch.setattr(
        spawn_mod, "subprocess", SimpleNamespace(Popen=raise_plain_permission)
    )
    with pytest.raises(PermissionError, match="without winerror"):
        spawn_detached_daemon(["denied.exe"])
    assert perm_calls == [_BREAKAWAY_DETACHED_FLAGS]

    missing_calls: list[int] = []

    def raise_missing(_args: Any, **kwargs: Any) -> Any:
        missing_calls.append(kwargs["creationflags"])
        err = FileNotFoundError("executable missing")
        err.winerror = 2  # type: ignore[attr-defined]
        raise err

    monkeypatch.setattr(spawn_mod, "subprocess", SimpleNamespace(Popen=raise_missing))
    with pytest.raises(FileNotFoundError):
        spawn_detached_daemon(["missing.exe"])
    assert missing_calls == [_BREAKAWAY_DETACHED_FLAGS]


def test_windows_spawn_never_retries_winerror_5_caused_by_target_acl_or_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    blocked_dir = tmp_path / "not_an_executable"
    blocked_dir.mkdir()
    non_exec_file = tmp_path / "non_executable.exe"
    non_exec_file.write_bytes(b"MZ")
    non_exec_file.chmod(0o400)

    monkeypatch.setattr(spawn_mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        spawn_mod, "_windows_current_job_allows_breakaway", lambda: (True, True)
    )

    for bad_target in (blocked_dir, non_exec_file):
        calls: list[int] = []

        def fake_popen(_args: Any, **kwargs: Any) -> Any:
            calls.append(kwargs["creationflags"])
            err = PermissionError("Target path lacks execute permission")
            err.winerror = 5  # type: ignore[attr-defined]
            raise err

        monkeypatch.setattr(spawn_mod, "subprocess", SimpleNamespace(Popen=fake_popen))
        with pytest.raises(PermissionError):
            spawn_detached_daemon([str(bad_target)])
        assert calls == [_BREAKAWAY_DETACHED_FLAGS]
