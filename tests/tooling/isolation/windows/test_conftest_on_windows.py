# Decision (2026-11-23, PR #3106 real-Windows acceptance): the root conftest
# chain must configure on Windows, where ``pwd`` does not exist, ``os.getuid``
# is absent, ``/dev/fd`` is missing and ``/tmp`` is not a real directory.
# There is no Windows runner, so each module under test sees a Windows-shaped
# ``os`` proxy instead of a patched global ``os.name``: pathlib dispatches on
# the global and would refuse to build paths on the Linux host.
# Decision (2026-11-23, review A-3/A-5): the fake Win32 surface answers with a
# constant profile, so these tests prove only that ``real_account_home`` never
# reads USERPROFILE/HOME itself and declares every signature; whether the real
# GetUserProfileDirectoryW ignores a decoy USERPROFILE is UNKNOWN until a real
# Windows run. The fake CloseHandle checks the token value it receives.
"""Simulated-Windows regression for the root conftest configuration chain."""

from __future__ import annotations

import ctypes
import os
import sys
import types
from pathlib import Path

import pytest

import tests.conftest as root_conftest
from tests.support.runtime import _tmp, environment
from tests.support.windows_suite import collection
from tests.tooling.isolation.windows._windows_os import WindowsOs

PROFILE = "C:\\Users\\real-account"
TOKEN = 0x1234
_EXPORTS = {
    "kernel32": ("GetCurrentProcess", "CloseHandle"),
    "advapi32": ("OpenProcessToken",),
    "userenv": ("GetUserProfileDirectoryW",),
}


class _Function:
    def __init__(self, impl) -> None:
        self.impl = impl
        self.argtypes = None
        self.restype = "undeclared"

    def __call__(self, *args):
        return self.impl(*args)


class _WinApi:
    """Fake Win32 surface; it cannot prove the real API ignores the environment."""

    def __init__(self) -> None:
        self.dlls: dict[str, types.SimpleNamespace] = {}
        self.closed: list[int] = []
        self.last_error = 0
        self.open_ok = True
        self.profile_error = 0

    def load(self, name: str, use_last_error: bool = False) -> types.SimpleNamespace:
        assert use_last_error
        dll = types.SimpleNamespace(**{export: _Function(getattr(self, export)) for export in _EXPORTS[name]})
        self.dlls[name] = dll
        return dll

    def GetCurrentProcess(self) -> int:
        return -1

    def CloseHandle(self, handle) -> int:
        self.closed.append(handle.value)
        return 1

    def OpenProcessToken(self, process, access, token) -> int:
        assert (process, access) == (-1, 0x0008)
        if not self.open_ok:
            self.last_error = 5
            return 0
        token._obj.value = TOKEN
        return 1

    def GetUserProfileDirectoryW(self, token, buffer, size) -> int:
        assert token.value == TOKEN
        if self.profile_error:
            self.last_error = self.profile_error
            return 0
        if buffer is None or size._obj.value < len(PROFILE) + 1:
            size._obj.value = len(PROFILE) + 1
            self.last_error = 122
            return 0
        buffer.value = PROFILE
        return 1


class _NoWindll:
    def __getattr__(self, attr: str):
        raise AssertionError(f"touched the shared ctypes.windll.{attr}")


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> _WinApi:
    api = _WinApi()
    monkeypatch.setattr(environment, "os", WindowsOs())
    monkeypatch.setitem(sys.modules, "pwd", None)
    monkeypatch.setattr(ctypes, "WinDLL", api.load, raising=False)
    monkeypatch.setattr(ctypes, "windll", _NoWindll(), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: api.last_error, raising=False)
    monkeypatch.setenv("USERPROFILE", "C:\\redirected\\by-a-test")
    monkeypatch.setenv("HOME", "C:\\redirected\\by-a-test")
    return api


def test_real_account_home_asks_the_process_token_profile_not_the_environment(windows) -> None:
    assert environment.real_account_home() == Path(PROFILE)
    assert sorted(windows.dlls) == ["advapi32", "kernel32", "userenv"]
    assert windows.closed == [TOKEN]


def test_real_account_home_declares_every_win32_signature(windows) -> None:
    environment.real_account_home()
    for name, dll in windows.dlls.items():
        for export in _EXPORTS[name]:
            function = getattr(dll, export)
            assert function.argtypes is not None, export
            assert function.restype != "undeclared", export


def test_real_account_home_fails_loudly_when_the_token_cannot_be_opened(windows) -> None:
    windows.open_ok = False
    with pytest.raises(OSError, match="OpenProcessToken failed: Win32 error 5"):
        environment.real_account_home()
    assert windows.closed == []


def test_real_account_home_fails_loudly_and_closes_the_token_when_the_profile_lookup_fails(windows) -> None:
    windows.profile_error = 1312
    with pytest.raises(OSError, match="GetUserProfileDirectoryW failed: Win32 error 1312"):
        environment.real_account_home()
    assert windows.closed == [TOKEN]


def test_root_conftest_configure_registers_the_tripwire_on_windows(
    windows, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: dict[str, object] = {}
    manager = types.SimpleNamespace(
        register=lambda plugin, name: registered.setdefault(name, plugin),
        get_plugin=registered.get,
    )
    monkeypatch.delenv("HYPRIAL_CI_PROFILE", raising=False)
    root_conftest.pytest_configure(types.SimpleNamespace(pluginmanager=manager))
    assert isinstance(registered[collection.PLUGIN_NAME], collection.PosixOnlyPlugin)
    tripwire = registered["service-unit-tripwire"]
    assert isinstance(tripwire, environment.ServiceUnitTripwire)
    assert tripwire.home == Path(PROFILE)


@pytest.mark.posix_only("pwd")
def test_real_account_home_keeps_the_posix_password_entry() -> None:
    import pwd

    assert environment.real_account_home() == Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_fd_hygiene_snapshot_is_empty_where_dev_fd_does_not_exist(monkeypatch) -> None:
    monkeypatch.setattr(root_conftest, "os", WindowsOs())
    monkeypatch.setattr(os, "listdir", lambda path: pytest.fail(f"listed {path}"))
    assert root_conftest._fd_set() == set()


def test_fd_hygiene_snapshot_still_lists_dev_fd_on_posix() -> None:
    with open(__file__, "rb") as handle:
        assert handle.fileno() in root_conftest._fd_set()


def test_tmp_root_defaults_to_the_platform_temp_directory_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(_tmp, "os", WindowsOs())
    monkeypatch.delenv("HYPRIAL_TEST_TMP_ROOT", raising=False)
    monkeypatch.setattr(_tmp.tempfile, "gettempdir", lambda: "C:\\Users\\real-account\\AppData\\Local\\Temp")
    assert _tmp.hyprial_test_tmp_root() == "C:\\Users\\real-account\\AppData\\Local\\Temp"
