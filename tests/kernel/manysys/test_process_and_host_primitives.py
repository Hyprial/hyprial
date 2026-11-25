# Decision record (test_process_and_host_primitives, W2B-1 2026-11-21):
# - Covers the 7 W2B-1 cross-platform primitives added to hyprial.kernel.manysys
#   with real host subprocess/filesystem execution and deterministic Linux,
#   Darwin, and Windows mock branches (including Windows parent-PID reuse
#   rejection and handle-fenced TerminateProcess without calling os.kill).
"""Unit and host tests for W2B-1 ``manysys`` process and host primitives."""

from __future__ import annotations

from ctypes import wintypes
import os
from pathlib import Path
import platform
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hyprial.kernel import (
    GUEST_WORKER_STATE,
    ProcessLivenessState,
    executable_filename,
    file_modes_are_significant,
    host_machine_identifier,
    open_descriptor_count,
    posix_runtime_bus_dir,
    process_birth_identity,
    probe_process_group_or_pid,
    process_cpu_sample,
    process_parent_pid,
    scoped_process_birth_marker,
    terminate_fenced_process,
)
import hyprial.kernel.impl.manysys.fs.account as account_module
import hyprial.kernel.impl.manysys.process.owned_process as owned_process_module
import hyprial.kernel.impl.manysys.process.windows_process as windows_process


def _fill_filetime(target: wintypes.FILETIME, ticks: int) -> None:
    target.dwHighDateTime = (ticks >> 32) & 0xFFFFFFFF
    target.dwLowDateTime = ticks & 0xFFFFFFFF


class _FakeWinFunc:
    def __init__(self, fn: object) -> None:
        self._fn = fn
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        return self._fn(*args)  # type: ignore[operator]


class _FakeWindowsKernel32:
    def __init__(
        self,
        processes: dict[int, dict[str, int]],
        *,
        snapshot_rows: list[tuple[int, int]] | None = None,
    ) -> None:
        self._processes = processes
        self._snapshot_rows = snapshot_rows or []
        self._handles: dict[int, int] = {}
        self._next_handle = 100
        self._snap_index = 0
        self.terminated: list[tuple[int, int]] = []
        self.closed_handles: list[int] = []
        self.OpenProcess = _FakeWinFunc(self._open_process)
        self.WaitForSingleObject = _FakeWinFunc(self._wait_for_single_object)
        self.GetProcessTimes = _FakeWinFunc(self._get_process_times)
        self.TerminateProcess = _FakeWinFunc(self._terminate_process)
        self.CloseHandle = _FakeWinFunc(self._close_handle)
        self.CreateToolhelp32Snapshot = _FakeWinFunc(self._create_snapshot)
        self.Process32FirstW = _FakeWinFunc(self._write_snapshot_row)
        self.Process32NextW = _FakeWinFunc(self._write_snapshot_row)

    def _open_process(self, _access: int, _inherit: bool, pid: int) -> int:
        if pid not in self._processes:
            return 0
        handle = self._next_handle
        self._next_handle += 1
        self._handles[handle] = pid
        return handle

    def _wait_for_single_object(self, handle: int, _timeout: int) -> int:
        pid = self._handles[handle]
        return self._processes[pid].get("wait_state", 258)

    def _get_process_times(
        self,
        handle: int,
        created_ptr: object,
        exited_ptr: object,
        kernel_ptr: object,
        user_ptr: object,
    ) -> int:
        pid = self._handles[handle]
        row = self._processes[pid]
        _fill_filetime(created_ptr._obj, row["created"])  # type: ignore[attr-defined]
        _fill_filetime(exited_ptr._obj, 0)  # type: ignore[attr-defined]
        _fill_filetime(kernel_ptr._obj, row.get("kernel", 0))  # type: ignore[attr-defined]
        _fill_filetime(user_ptr._obj, row.get("user", 0))  # type: ignore[attr-defined]
        return 1

    def _terminate_process(self, handle: int, exit_code: int) -> int:
        pid = self._handles[handle]
        self.terminated.append((pid, exit_code))
        self._processes[pid]["wait_state"] = 0
        return 1

    def _close_handle(self, handle: int) -> int:
        self.closed_handles.append(handle)
        self._handles.pop(handle, None)
        return 1

    def _create_snapshot(self, _flags: int, _pid: int) -> int:
        self._snap_index = 0
        return 999

    def _write_snapshot_row(self, _snapshot: int, entry_ptr: object) -> int:
        if self._snap_index >= len(self._snapshot_rows):
            return 0
        pid, ppid = self._snapshot_rows[self._snap_index]
        self._snap_index += 1
        entry_ptr._obj.th32ProcessID = pid  # type: ignore[attr-defined]
        entry_ptr._obj.th32ParentProcessID = ppid  # type: ignore[attr-defined]
        return 1


def test_real_host_parent_pid_cpu_sample_and_scoped_marker() -> None:
    assert process_parent_pid(0) is None
    assert process_parent_pid(-5) is None
    assert process_parent_pid(True) is None  # type: ignore[arg-type]
    assert process_parent_pid(os.getpid()) == os.getppid()

    sample = process_cpu_sample(os.getpid())
    assert sample is not None
    birth, cpu_seconds = sample
    assert birth
    assert cpu_seconds >= 0.0
    assert process_cpu_sample(0) is None

    scoped = scoped_process_birth_marker(os.getpid())
    assert scoped is not None
    if sys.platform.startswith("linux"):
        assert scoped.startswith("proc-starttime:")

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process_parent_pid(child.pid) == os.getpid()
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_scoped_birth_marker_supports_custom_proc_root_and_ps_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "name", "posix")
    proc_dir = tmp_path / "4242"
    proc_dir.mkdir()
    fields = ["S", "1", "4242", "4242"] + ["0"] * 15 + ["987654321", "0"]
    (proc_dir / "stat").write_text(
        "4242 (worker) " + " ".join(fields) + "\n",
        encoding="utf-8",
    )
    assert (
        scoped_process_birth_marker(4242, proc_root=tmp_path)
        == "proc-starttime:987654321"
    )
    assert (
        scoped_process_birth_marker(
            9999,
            proc_root=tmp_path,
            ps_reader=lambda pid: f"ps-lstart:synthetic-{pid}",
        )
        == "ps-lstart:synthetic-9999"
    )


def test_posix_probe_and_terminate_fenced_process_on_real_child() -> None:
    if os.name == "nt":
        pytest.skip("POSIX process-group test")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        liveness = probe_process_group_or_pid(child.pid)
        assert liveness.state is ProcessLivenessState.ALIVE
        real_marker = scoped_process_birth_marker(child.pid)
        assert real_marker is not None
        wrong_marker = "proc-starttime:1"
        assert (
            terminate_fenced_process(
                child.pid, birth_marker=wrong_marker, process_group=True
            )
            is False
        )
        assert child.poll() is None

        plain_marker = process_birth_identity(child.pid)
        assert plain_marker is not None
        assert (
            terminate_fenced_process(
                child.pid, birth_marker=plain_marker, process_group=True
            )
            is True
        )
        child.wait(timeout=5)
        dead = probe_process_group_or_pid(child.pid)
        assert dead.state is ProcessLivenessState.DEAD
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_windows_parent_pid_verifies_parent_creation_time_against_pid_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "name", "nt")

    fake_valid = _FakeWindowsKernel32(
        {
            200: {"created": 5_000, "wait_state": 258},
            100: {"created": 4_000, "wait_state": 258},
        }
    )
    monkeypatch.setattr(windows_process, "_bind_kernel32", lambda: fake_valid)
    monkeypatch.setattr(windows_process, "_ntquery_parent_pid", lambda _handle: 100)
    assert process_parent_pid(200) == 100

    fake_reused = _FakeWindowsKernel32(
        {
            200: {"created": 5_000, "wait_state": 258},
            100: {"created": 5_001, "wait_state": 258},
        }
    )
    monkeypatch.setattr(windows_process, "_bind_kernel32", lambda: fake_reused)
    assert process_parent_pid(200) is None

    fake_exited_parent = _FakeWindowsKernel32(
        {
            200: {"created": 5_000, "wait_state": 258},
            100: {"created": 4_000, "wait_state": 0},
        }
    )
    monkeypatch.setattr(windows_process, "_bind_kernel32", lambda: fake_exited_parent)
    assert process_parent_pid(200) is None

    fake_toolhelp = _FakeWindowsKernel32(
        {
            200: {"created": 5_000, "wait_state": 258},
            150: {"created": 3_000, "wait_state": 258},
        },
        snapshot_rows=[(10, 4), (200, 150)],
    )
    monkeypatch.setattr(windows_process, "_bind_kernel32", lambda: fake_toolhelp)
    monkeypatch.setattr(windows_process, "_ntquery_parent_pid", lambda _handle: None)
    assert process_parent_pid(200) == 150


def test_windows_cpu_sample_and_handle_fenced_termination_never_call_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        os,
        "kill",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unfenced os.kill")),
    )

    fake = _FakeWindowsKernel32(
        {
            300: {
                "created": 123_456_789,
                "kernel": 15_000_000,
                "user": 25_000_000,
                "wait_state": 258,
            }
        }
    )
    monkeypatch.setattr(windows_process, "_bind_kernel32", lambda: fake)

    sample = process_cpu_sample(300)
    assert sample == ("win-filetime:123456789", 4.0)
    assert scoped_process_birth_marker(300) == "win-filetime:123456789"

    assert terminate_fenced_process(300, birth_marker=None) is False
    assert (
        terminate_fenced_process(300, birth_marker="win-filetime:999999999")
        is False
    )
    assert (
        terminate_fenced_process(300, birth_marker="win32:999999999")
        is False
    )
    assert (
        terminate_fenced_process(
            300, birth_marker="win32:123456789;win-filetime:999999999"
        )
        is False
    )
    assert fake.terminated == []

    assert (
        terminate_fenced_process(
            300,
            birth_marker="ps-lstart:legacy;win-filetime:123456789",
            process_group=True,
        )
        is True
    )
    assert fake.terminated == [(300, 1)]

    fake._processes[300]["wait_state"] = 258
    assert (
        terminate_fenced_process(
            300,
            birth_marker="win32:123456789",
            process_group=True,
        )
        is True
    )
    assert fake.terminated == [(300, 1), (300, 1)]


def test_windows_fencing_ignores_unknown_marker_schemes() -> None:
    assert windows_process._windows_marker_matches(
        "unknown-scheme:abc;win-filetime:123", 123
    ) is True
    assert windows_process._windows_marker_matches(
        "win-filetime:123;unknown-scheme:abc", 123
    ) is True
    assert windows_process._windows_marker_matches(
        "win-filetime:123;unknown-scheme:", 123
    ) is False
    assert windows_process._windows_marker_matches(
        "win-filetime:", 123
    ) is False
    assert windows_process._windows_marker_matches(
        "win-filetime:123;malformed", 123
    ) is False
    assert windows_process._windows_marker_matches(
        "win-filetime:123;win32:456", 123
    ) is False


def test_scoped_birth_marker_darwin_confirmation_and_legacy_scheme_fencing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify exact 2c706e2850 Darwin/Linux marker emission and legacy scheme fencing."""

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(
        owned_process_module,
        "darwin_process_start_time",
        lambda _pid: (1755490000, 123),
    )
    assert (
        scoped_process_birth_marker(
            4242, ps_reader=lambda _pid: "ps-lstart:Mon Aug 18 04:06:40 2025"
        )
        == "darwin-starttime:1755490000:123;darwin-startsec:1755490000;ps-lstart:Mon Aug 18 04:06:40 2025"
    )
    assert (
        scoped_process_birth_marker(4242, ps_reader=lambda _pid: None)
        == "darwin-starttime:1755490000:123;darwin-startsec:1755490000"
    )

    unstable_reads = iter([(1755490000, 1), (1755490000, 2), (1755490000, 3), (1755490000, 4)])
    monkeypatch.setattr(
        owned_process_module,
        "darwin_process_start_time",
        lambda _pid: next(unstable_reads),
    )
    assert (
        scoped_process_birth_marker(
            4242, ps_reader=lambda _pid: "ps-lstart:Mon Aug 18 04:06:40 2025"
        )
        is None
    )

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, int(sig))))
    monkeypatch.setattr(owned_process_module, "process_birth_identity", lambda _pid: None)
    monkeypatch.setattr(
        owned_process_module,
        "darwin_process_start_time",
        lambda _pid: (1755490000, 123),
    )
    monkeypatch.setattr(
        owned_process_module,
        "_ps_utc_process_identity",
        lambda _pid: "ps-lstart:Mon Aug 18 04:06:40 2025",
    )
    assert terminate_fenced_process(4242, birth_marker="darwin:1755490000:123") is True
    assert terminate_fenced_process(4242, birth_marker="darwin:1755490000") is True
    assert terminate_fenced_process(4242, birth_marker="darwin:1755490000:999") is False
    assert len(killed) == 2

    monkeypatch.setattr(sys, "platform", "linux")
    proc_dir = tmp_path / "5151"
    proc_dir.mkdir()
    fields = ["S", "1", "5151", "5151"] + ["0"] * 15 + ["7654321", "0"]
    (proc_dir / "stat").write_text(
        "5151 (worker) " + " ".join(fields) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        owned_process_module,
        "scoped_process_birth_marker",
        lambda pid: scoped_process_birth_marker(pid, proc_root=tmp_path),
    )
    assert terminate_fenced_process(5151, birth_marker="linux:7654321") is True
    assert terminate_fenced_process(5151, birth_marker="proc:7654321") is True
    assert terminate_fenced_process(5151, birth_marker="linux:1111111") is False
    assert len(killed) == 4


def test_executable_filename_fd_count_bus_dir_and_guest_worker_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert GUEST_WORKER_STATE == "/run/hyprial-worker"
    if sys.platform != "win32":
        fd_count = open_descriptor_count()
        assert fd_count is None or fd_count > 0
        assert posix_runtime_bus_dir(1000) == Path("/run/user/1000")
        assert file_modes_are_significant() is True

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "name", "nt")
    assert executable_filename("hyprial-tailcat") == "hyprial-tailcat.exe"
    assert open_descriptor_count() is None
    assert posix_runtime_bus_dir(1000) is None
    assert file_modes_are_significant() is False

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "name", "posix")
    assert executable_filename("hyprial-tailcat") == "hyprial-tailcat"
    assert owned_process_module._parse_ps_cputime("2-01:02:03.5") == 176523.5


def test_host_machine_identifier_across_linux_darwin_windows_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, real_value = host_machine_identifier()
    assert source in {
        "linux-machine-id",
        "darwin-platform-uuid",
        "windows-machine-guid",
        "fallback-host-node",
    }
    assert real_value

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        account_module.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            ["ioreg"], 0, '  "IOPlatformUUID" = "UUID-MAC-1"\n', ""
        ),
    )
    assert host_machine_identifier() == ("darwin-platform-uuid", "UUID-MAC-1")

    class _FakeRegKey:
        def __enter__(self) -> _FakeRegKey:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    fake_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE=1,
        OpenKey=lambda *_a: _FakeRegKey(),
        QueryValueEx=lambda _key, _name: ("WIN-GUID-42", 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert host_machine_identifier() == ("windows-machine-guid", "WIN-GUID-42")

    monkeypatch.setattr(platform, "system", lambda: "Plan9")
    fb_source, fb_value = host_machine_identifier()
    assert fb_source == "fallback-host-node"
    assert "\0" in fb_value
