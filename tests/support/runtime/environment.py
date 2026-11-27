"""Disposable subprocess homes and a read-only real-user service tripwire.

HOME and XDG paths isolate files, not launchd's UID domain. Product service
management must reject disposable homes; integration tests use --no-service.

Z-8c (2026-11-15): child environments also pin HYPRIAL_SERVICE_AUTOINSTALL=0
and HYPRIAL_DAEMON_AUTOSTART=0 after the HYPRIAL_* clear, so a subprocess CLI
run never installs or manager-starts a real unit even when the invoking
shell exported an opt-in value.

Decision (2026-11-23, PR #3106 real-Windows acceptance): ``real_account_home``
must import on Windows, where ``pwd`` and ``os.getuid`` do not exist. Windows
resolves the account profile with ``OpenProcessToken`` plus
``GetUserProfileDirectoryW``, which is documented to look the token's SID up in
the ``ProfileList`` registry key instead of reading ``USERPROFILE``/``HOME``;
``os.path.expanduser`` and ``Path.home`` read exactly those variables, which the
isolated fixtures rewrite on Windows, so they cannot anchor a tripwire against
touching the real account. A failed lookup raises instead of falling back, so
the tripwire never silently watches the wrong directory. POSIX keeps the
password-database entry unchanged.

Decision (2026-11-23, review A-3/A-4): the earlier ``SHGetKnownFolderPath``
lookup was dropped because ``FOLDERID_Profile`` independence from the process
environment was an unverified assumption. The ``ProfileList`` independence of
``GetUserProfileDirectoryW`` is likewise taken from its documentation and has
not been checked against a decoy ``USERPROFILE`` on a real Windows host
(UNKNOWN until that run). The DLLs are private ``ctypes.WinDLL`` instances with
declared ``argtypes``/``restype``, so correctness does not lean on ctypes'
default conversions and the shared ``ctypes.windll`` cache is never touched.

Decision (2026-11-23, review A-1): Windows reads the account home from
``USERPROFILE`` (``Path.home`` and ``expanduser`` ignore ``HOME`` there) and the
roaming/local application data from ``APPDATA``/``LOCALAPPDATA``.
``isolated_environment`` therefore also points ``USERPROFILE``, ``APPDATA``,
``LOCALAPPDATA``, ``HOMEDRIVE`` and ``HOMEPATH`` inside the disposable root on
Windows, and the root conftest applies the same keys in process. POSIX child
environments keep exactly the previous keys.

Decision (2026-11-23, review A-6): the service-unit tripwire only watches
``Library/LaunchAgents`` and ``.config/systemd/user``. Neither exists under a
Windows profile, so on Windows the tripwire is an empty check and gives no
protection; isolation there rests on the environment rewrite above. It stays
registered so a failed profile lookup still surfaces at configure time.
"""

from __future__ import annotations

import ntpath
import os
from pathlib import Path

import pytest

from hyprial.kernel import secure_private_directory
from hyprial.kernel.impl.primitives.node_identity import mint_node_id, node_id_path


def home_with_node_id(home: Path) -> Path:
    """Create *home* if needed and give it a node id through the product mint.

    For tests that build their own home instead of running ``hyprial init``;
    the host name is never a node-id fallback (2026-11-20).
    """

    secure_private_directory(home.resolve(), create=True)
    if not node_id_path(home).exists():
        mint_node_id(home)
    return home


UNIT_USER_ID = "ou_" + "0123456789abcdef" * 2

WINDOWS_PROFILE_KEYS = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH")


def _windows_profile_environment(home: Path) -> dict[str, str]:
    """Point every Windows account-home variable inside *home*."""

    for directory in (home / "AppData" / "Roaming", home / "AppData" / "Local"):
        secure_private_directory(directory.resolve(), create=True)
    drive, rest = ntpath.splitdrive(str(home))
    return {
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "HOMEDRIVE": drive,
        "HOMEPATH": rest,
    }


def isolated_environment(root: Path) -> dict[str, str]:
    """Keep subprocess state and user configuration inside one disposable root."""

    root = root.resolve()
    home = root / "operating-home"
    paths = {
        "HOME": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_DATA_HOME": home / ".local/share",
        "XDG_STATE_HOME": home / ".local/state",
        "XDG_RUNTIME_DIR": root / "runtime",
    }
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith(("HYPRIAL_", "HARNESS_", "DBUS_")):
            env.pop(key)
    for key, path in paths.items():
        secure_private_directory(path.resolve(), create=True)
        env[key] = str(path)
    if os.name == "nt":
        env.update(_windows_profile_environment(home))
    env.update({
        "HYPRIAL_HOME": str(root / "home"),
        "HARNESS_STATE_DIR": str(root / "state"),
        "HYPRIAL_NETWORK_ISOLATED": "1",
        "HYPRIAL_OWNER": "hyprial-unit-owner",
        "HYPRIAL_USER_ID": UNIT_USER_ID,
        "HYPRIAL_SERVICE_AUTOINSTALL": "0",
        "HYPRIAL_DAEMON_AUTOSTART": "0",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    })
    return env


def assert_root_local_environment(root: Path, env: dict[str, str]) -> None:
    """Check final subprocess paths after scenario-specific overrides."""

    for key in (
        "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
        "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "HYPRIAL_HOME", "HARNESS_STATE_DIR",
    ):
        assert Path(env[key]).resolve().is_relative_to(root.resolve()), (key, env[key])
    if os.name == "nt":
        for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            assert Path(env[key]).resolve().is_relative_to(root.resolve()), (key, env[key])


def remove_isolated_service_files(root: Path, env: dict[str, str]) -> None:
    """Remove only exact test-local unit files; never call a user service manager."""

    root = root.resolve()
    home = Path(env["HOME"]).resolve()
    assert home.is_relative_to(root) and home != root
    for suffix in (
        "Library/LaunchAgents/com.hyprial.hyprial.daemon.plist",
        ".config/systemd/user/hyprial.service",
    ):
        path = home / suffix
        assert path.resolve().is_relative_to(home)
        path.unlink(missing_ok=True)


def service_unit_snapshot(home: Path) -> dict[str, int]:
    """List only names and lstat mtimes; never open a unit's contents."""

    result = {}
    for suffix in ("Library/LaunchAgents", ".config/systemd/user"):
        directory = home / suffix
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if "hyprial" in entry.name:
                        result[f"{suffix}/{entry.name}"] = entry.stat(follow_symlinks=False).st_mtime_ns
        except FileNotFoundError:
            pass
    return result


class ServiceUnitTripwire:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.before: dict[str, int] = {}
        self.changed: list[str] = []

    def pytest_sessionstart(self, session) -> None:
        self.before = service_unit_snapshot(self.home)

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        after = service_unit_snapshot(self.home)
        self.changed = sorted(
            name for name in self.before.keys() | after.keys()
            if self.before.get(name) != after.get(name)
        )
        if self.changed:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self.changed:
            terminalreporter.write_sep("!", "REAL USER SERVICE UNIT CHANGED")
            for name in self.changed:
                terminalreporter.write_line(name)


_TOKEN_QUERY = 0x0008
_ERROR_INSUFFICIENT_BUFFER = 122


def _windows_profile_home() -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    userenv.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise OSError(f"OpenProcessToken failed: Win32 error {ctypes.get_last_error()}")
    try:
        size = wintypes.DWORD(0)
        if not userenv.GetUserProfileDirectoryW(token, None, ctypes.byref(size)):
            error = ctypes.get_last_error()
            if error != _ERROR_INSUFFICIENT_BUFFER or size.value == 0:
                raise OSError(f"GetUserProfileDirectoryW failed: Win32 error {error}")
        buffer = ctypes.create_unicode_buffer(size.value)
        if not userenv.GetUserProfileDirectoryW(token, buffer, ctypes.byref(size)):
            raise OSError(f"GetUserProfileDirectoryW failed: Win32 error {ctypes.get_last_error()}")
        if not buffer.value:
            raise OSError("GetUserProfileDirectoryW returned an empty profile path")
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(token)


def real_account_home() -> Path:
    """The process token's real account home, immune to HOME/USERPROFILE rewrites."""

    if os.name == "nt":
        return _windows_profile_home()
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)
