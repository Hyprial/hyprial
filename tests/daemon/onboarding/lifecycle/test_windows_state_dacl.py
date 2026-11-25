"""Windows init -> bootstrap privacy of the daemon state directory, without Windows.

The fake models the two Windows facts the real acceptance run (PR #3106) hit:

* CPython 3.12.4+ (gh-118486) ``os.mkdir(path, 0o700)`` on Windows applies its
  own protected DACL ``D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)``.
  ``OW`` is OWNER RIGHTS (``S-1-3-4``), not the current user's SID;
* any other new entry inherits its parent's inheritable ACEs, and a path the
  test did not create (the user profile under ``C:\\Users\\<user>``) carries
  the profile's inherited SYSTEM / Administrators / current-user entries.

An elevated process is modelled by ``creator``: Windows makes
``BUILTIN\\Administrators`` the default owner of what an elevated token creates.
"""

from __future__ import annotations

import os
import stat
import types
from pathlib import Path

import pytest

from hyprial import kernel
from hyprial.daemon.impl.onboarding.lifecycle import admission as admission_module
from hyprial.daemon.impl.onboarding.lifecycle.admission import BootstrapAdmission
from hyprial.kernel.impl.manysys.fs import private_access
from hyprial.shell.impl import daemon_spawn
from hyprial.shell.impl.cli.commands.common import daemon_start
from hyprial.shell.impl.cli.commands.common.services import get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401

_CURRENT = "S-1-5-21-1000-1001"
_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"
_OWNER_RIGHTS = "S-1-3-4"
_AUTHENTICATED_USERS = "S-1-5-11"
_FILE_ALL_ACCESS = 0x1F01FF
_OICI = 0x1 | 0x2
_INHERITED = 0x10


class _Acl:
    def __init__(self, aces: list[tuple[tuple[int, int], int, str]] | None = None) -> None:
        self.aces = list(aces or [])

    def AddAccessAllowedAceEx(self, revision: int, flags: int, mask: int, sid: str) -> None:
        assert revision == 2
        self.aces.append(((0, flags), mask, sid))

    def GetAceCount(self) -> int:
        return len(self.aces)

    def GetAce(self, index: int):
        return self.aces[index]


class _Descriptor:
    def __init__(self, owner: str, dacl: _Acl) -> None:
        self.owner = owner
        self.dacl = dacl

    def GetSecurityDescriptorOwner(self) -> str:
        return self.owner

    def GetSecurityDescriptorDacl(self) -> _Acl:
        return self.dacl


class _WindowsAcls:
    """Per-path security descriptors with CPython 3.12.4+ (gh-118486) mkdir and inheritance."""

    def __init__(self, profile: tuple[str, ...], creator: str = _CURRENT) -> None:
        self.profile = profile
        self.creator = creator
        self.entries: dict[str, tuple[str, _Acl, bool]] = {}

    def _lookup(self, path: str) -> tuple[str, _Acl, bool]:
        found = self.entries.get(path)
        if found is not None:
            return found
        aces = [((0, _OICI | _INHERITED), _FILE_ALL_ACCESS, sid) for sid in self.profile]
        return _CURRENT, _Acl(aces), False

    def created(self, path: str, *, mode: int | None) -> None:
        if mode == 0o700:
            aces = [
                ((0, _OICI), _FILE_ALL_ACCESS, sid)
                for sid in (_SYSTEM, _ADMINISTRATORS, _OWNER_RIGHTS)
            ]
            self.entries[path] = (self.creator, _Acl(aces), True)
            return
        _owner, parent, _protected = self._lookup(os.path.dirname(path))
        aces = [
            ((kind, (flags & _OICI) | _INHERITED), mask, sid)
            for (kind, flags), mask, sid in parent.aces
            if flags & 0x2
        ]
        self.entries[path] = (self.creator, _Acl(aces), False)

    def get(self, path: str, _object: int, _info: int) -> _Descriptor:
        owner, dacl, _protected = self._lookup(path)
        return _Descriptor(owner, dacl)

    def set(self, path: str, _object: int, info: int, _owner, _group, dacl, _sacl) -> None:
        owner, _old, _protected = self._lookup(path)
        self.entries[path] = (owner, dacl, bool(info & 0x80000000))


class _NtOs:
    """``os`` as private_access sees it on Windows; pathlib keeps the host flavour."""

    name = "nt"

    def __getattr__(self, attribute: str):
        return getattr(os, attribute)


def _install(monkeypatch: pytest.MonkeyPatch, acls: _WindowsAcls) -> None:
    security = types.SimpleNamespace(
        ACL=_Acl,
        ACL_REVISION=2,
        SE_FILE_OBJECT=1,
        OWNER_SECURITY_INFORMATION=1,
        DACL_SECURITY_INFORMATION=4,
        PROTECTED_DACL_SECURITY_INFORMATION=0x80000000,
        TokenUser=1,
        OpenProcessToken=lambda *_args: types.SimpleNamespace(Close=lambda: None),
        GetTokenInformation=lambda *_args: (_CURRENT,),
        ConvertSidToStringSid=lambda sid: sid,
        GetNamedSecurityInfo=acls.get,
        SetNamedSecurityInfo=acls.set,
    )
    modules = private_access._WindowsSecurity(
        types.SimpleNamespace(GetCurrentProcess=lambda: 1),
        types.SimpleNamespace(GENERIC_ALL=0x10000000, TOKEN_QUERY=0x8),
        security,
    )
    real_mkdir = os.mkdir

    def windows_mkdir(path, mode=0o777, *, dir_fd=None):
        real_mkdir(path, mode, dir_fd=dir_fd)
        acls.created(os.path.abspath(os.fspath(path)), mode=mode)

    monkeypatch.setattr(os, "mkdir", windows_mkdir)
    monkeypatch.setattr(private_access, "os", _NtOs())
    monkeypatch.setattr(private_access, "posix_metadata_enforced", lambda: False)
    monkeypatch.setattr(private_access, "_windows_security", lambda: modules)


class _Admitted(Exception):
    """The privacy fence passed and admission moved on to the owner check."""


def _admit(monkeypatch: pytest.MonkeyPatch, home: Path, state: Path) -> None:
    def _passed() -> None:
        raise _Admitted

    monkeypatch.setattr(admission_module, "require_unoverridden_owner", _passed)
    with pytest.raises(_Admitted):
        BootstrapAdmission.acquire(home, state, state / "daemon.sock")


class _LaunchLocked(Exception):
    """The launcher reached its launch lock after preparing the state directory."""


def _launch_until_lock(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    def _stop(_descriptor: int) -> None:
        raise _LaunchLocked

    monkeypatch.setattr(get_services(), "_state_dir", lambda: state)
    monkeypatch.setattr(daemon_start, "lock_exclusive", _stop)
    with pytest.raises(_LaunchLocked):
        daemon_start._launch_daemon_process_locked(
            ready_timeout=1.0, listen=None, connect=None
        )


def test_state_created_by_a_plain_private_mkdir_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acls = _WindowsAcls(profile=(_SYSTEM, _ADMINISTRATORS, _CURRENT))
    _install(monkeypatch, acls)
    home = private_access.secure_private_directory(tmp_path / "home", create=True)
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert [sid for _flags, _mask, sid in acls.entries[str(state)][1].aces] == [
        _SYSTEM,
        _ADMINISTRATORS,
        _OWNER_RIGHTS,
    ]

    _admit(monkeypatch, home, state)


def test_a_subdirectory_inheriting_a_plain_private_mkdir_dacl_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acls = _WindowsAcls(profile=(_SYSTEM, _ADMINISTRATORS, _CURRENT))
    _install(monkeypatch, acls)
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    child = logs / "nested"
    child.mkdir()

    assert private_access.private_metadata_violation(child, child.lstat(), mode=None) is None


def test_launcher_hardens_a_new_state_directory_before_bootstrap_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acls = _WindowsAcls(profile=(_SYSTEM, _ADMINISTRATORS, _CURRENT))
    _install(monkeypatch, acls)
    home = private_access.secure_private_directory(tmp_path / "home", create=True)
    state = home / "state"

    _launch_until_lock(monkeypatch, state)

    owner, dacl, protected = acls.entries[str(state)]
    assert protected is True
    assert [sid for _flags, _mask, sid in dacl.aces] == [_CURRENT]
    _admit(monkeypatch, home, state)


def test_launcher_hardens_every_missing_state_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acls = _WindowsAcls(profile=(_SYSTEM, _ADMINISTRATORS, _CURRENT))
    _install(monkeypatch, acls)
    home = private_access.secure_private_directory(tmp_path / "home", create=True)
    state = tmp_path / "outside" / "deep" / "state"

    _launch_until_lock(monkeypatch, state)

    for path in (tmp_path / "outside", tmp_path / "outside" / "deep", state):
        assert acls.entries[str(path)][2] is True
    _admit(monkeypatch, home, state)


def test_existing_state_inheriting_a_hardened_home_is_admitted_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acls = _WindowsAcls(profile=(_SYSTEM, _ADMINISTRATORS, _CURRENT))
    _install(monkeypatch, acls)
    home = private_access.secure_private_directory(tmp_path / "home", create=True)
    state = home / "state"
    state.mkdir()
    before = acls.entries[str(state)]

    _launch_until_lock(monkeypatch, state)

    assert acls.entries[str(state)] is before
    _admit(monkeypatch, home, state)


def test_profile_inherited_home_entries_are_trusted_and_init_hardens_a_precreated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acls = _WindowsAcls(profile=(_SYSTEM, _ADMINISTRATORS, _CURRENT))
    _install(monkeypatch, acls)
    home = tmp_path / "precreated"
    home.mkdir()
    assert acls.entries[str(home)][2] is False
    assert private_access.private_metadata_violation(home, home.lstat(), mode=None) is None

    private_access.secure_private_directory(home, create=True)
    assert acls.entries[str(home)][2] is True
    state = home / "state"
    _launch_until_lock(monkeypatch, state)
    _admit(monkeypatch, home, state)


def test_a_precreated_home_with_a_broad_profile_ace_is_repaired_by_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acls = _WindowsAcls(profile=(_SYSTEM, _ADMINISTRATORS, _CURRENT, _AUTHENTICATED_USERS))
    _install(monkeypatch, acls)
    home = tmp_path / "precreated"
    home.mkdir()
    assert (
        private_access.private_metadata_violation(home, home.lstat(), mode=None)
        == "unsafe-acl"
    )

    private_access.secure_private_directory(home, create=True)
    state = home / "state"
    _launch_until_lock(monkeypatch, state)
    _admit(monkeypatch, home, state)


def test_elevated_launch_is_admitted_but_a_plain_private_mkdir_stays_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acls = _WindowsAcls(
        profile=(_SYSTEM, _ADMINISTRATORS, _CURRENT), creator=_ADMINISTRATORS
    )
    _install(monkeypatch, acls)
    home = private_access.secure_private_directory(tmp_path / "home", create=True)
    state = home / "state"

    _launch_until_lock(monkeypatch, state)

    assert acls.entries[str(state)][0] == _ADMINISTRATORS
    _admit(monkeypatch, home, state)
    attempts = state / "attempts"
    attempts.mkdir(mode=0o700)
    assert acls.entries[str(attempts)][0] == _ADMINISTRATORS
    assert (
        private_access.private_metadata_violation(attempts, attempts.lstat(), mode=None)
        == "unsafe-acl"
    )


def _racing_secure_private_directory(
    monkeypatch: pytest.MonkeyPatch, lost: Path
) -> list[Path]:
    real = kernel.secure_private_directory
    calls: list[Path] = []

    def racing(path: Path, **options: object) -> Path:
        calls.append(Path(path))
        if len(calls) == 1:
            lost.mkdir(mode=0o700)
            raise FileExistsError(str(lost))
        return real(path, **options)

    monkeypatch.setattr(kernel, "secure_private_directory", racing)
    return calls


def test_state_preparation_retries_once_when_a_racing_launcher_created_an_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not private_access.posix_metadata_enforced():
        pytest.skip("POSIX ownership metadata is not enforced on this platform")
    state = tmp_path / "outside" / "state"
    calls = _racing_secure_private_directory(monkeypatch, state.parent)

    daemon_spawn.prepare_private_state_dir(state)

    assert calls == [state, state]
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_state_preparation_accepts_a_state_directory_a_racing_launcher_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    calls = _racing_secure_private_directory(monkeypatch, state)

    daemon_spawn.prepare_private_state_dir(state)

    assert calls == [state]
    assert state.is_dir()


def test_posix_launcher_state_directory_stays_0700(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not private_access.posix_metadata_enforced():
        pytest.skip("POSIX ownership metadata is not enforced on this platform")
    state = tmp_path / "home" / "state"

    _launch_until_lock(monkeypatch, state)

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
