"""Windows DACL trust-boundary decisions, exercised without Windows."""

from __future__ import annotations

import os
import stat
import types
from pathlib import Path

import pytest

from hyprial.kernel.impl.platform.privacy import private_access

_CURRENT = "S-1-5-21-1000"
_OTHER_USER = "S-1-5-21-2000"
_SYSTEM = "S-1-5-18"


class _Acl:
    def __init__(self) -> None:
        self.aces: list[tuple[object, int, object]] = []

    def AddAccessAllowedAce(self, revision: int, mask: int, sid: object) -> None:
        self.AddAccessAllowedAceEx(revision, 0, mask, sid)

    def AddAccessAllowedAceEx(
        self, revision: int, flags: int, mask: int, sid: object
    ) -> None:
        assert revision == 2
        # pywin32 312: GetAce returns ((type, flags), mask, sid).
        self.aces.append(((0, flags), mask, sid))

    def GetAceCount(self) -> int:
        return len(self.aces)

    def GetAce(self, index: int) -> tuple[object, int, object]:
        return self.aces[index]


class _Token:
    def Close(self) -> None:
        pass


class _SecurityDescriptor:
    """The object pywin32 returns from GetNamedSecurityInfo.

    pywin32 312 returns a PySECURITY_DESCRIPTOR, not the four-tuple the unit
    fake used to imitate; the production code must read the owner and DACL
    through these accessors.
    """

    def __init__(self, owner: str, dacl: _Acl | None) -> None:
        self.owner = owner
        self.dacl = dacl

    def GetSecurityDescriptorOwner(self) -> str:
        return self.owner

    def GetSecurityDescriptorDacl(self) -> _Acl | None:
        return self.dacl


class _Descriptor:
    def __init__(
        self,
        path: Path,
        *,
        owner: str = _CURRENT,
        trustees: tuple[str, ...] = (_CURRENT,),
        mask: int = 0x1,
    ) -> None:
        self.path = str(path)
        self.owner = owner
        self.dacl: _Acl | None = _Acl()
        assert self.dacl is not None
        for trustee in trustees:
            self.dacl.AddAccessAllowedAce(2, mask, trustee)
        self.protected = False

    def get(self, path: str, _object: int, _info: int):
        assert path == self.path
        return _SecurityDescriptor(self.owner, self.dacl)

    def set(self, path: str, _object: int, info: int, _owner, _group, dacl, _sacl) -> None:
        assert path == self.path
        self.dacl = dacl
        self.protected = bool(info & 0x80000000)


def _modules(
    descriptor: _Descriptor | None, *, token_error: bool = False
) -> private_access._WindowsSecurity | None:
    if descriptor is None:
        return None

    def _open_token(*_args):
        if token_error:
            raise RuntimeError("access denied")
        return _Token()

    security = types.SimpleNamespace(
        ACL=_Acl,
        ACL_REVISION=2,
        SE_FILE_OBJECT=1,
        OWNER_SECURITY_INFORMATION=1,
        DACL_SECURITY_INFORMATION=4,
        PROTECTED_DACL_SECURITY_INFORMATION=0x80000000,
        TokenUser=1,
        OpenProcessToken=_open_token,
        GetTokenInformation=lambda *_args: (_CURRENT,),
        ConvertSidToStringSid=lambda sid: sid,
        GetNamedSecurityInfo=descriptor.get,
        SetNamedSecurityInfo=descriptor.set,
    )
    api = types.SimpleNamespace(GetCurrentProcess=lambda: 1)
    con = types.SimpleNamespace(GENERIC_ALL=0x10000000, TOKEN_QUERY=0x8)
    return private_access._WindowsSecurity(api, con, security)


def _install_windows(
    monkeypatch: pytest.MonkeyPatch,
    descriptor: _Descriptor | None,
    *,
    token_error: bool = False,
) -> None:
    monkeypatch.setattr(private_access.os, "name", "nt")
    monkeypatch.setattr(private_access, "posix_metadata_enforced", lambda: False)
    monkeypatch.setattr(
        private_access,
        "_windows_security",
        lambda: _modules(descriptor, token_error=token_error),
    )


def _metadata(*, reparse: bool = False) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        st_file_attributes=0x400 if reparse else 0,
        st_mode=stat.S_IFREG | 0o666,
        st_uid=0,
    )


def test_current_user_only_dacl_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    descriptor = _Descriptor(path)
    _install_windows(monkeypatch, descriptor)

    assert private_access.private_metadata_violation(path, _metadata(), mode=0o600) is None


def test_another_users_allow_ace_is_not_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    descriptor = _Descriptor(path, trustees=(_CURRENT, _OTHER_USER))
    _install_windows(monkeypatch, descriptor)

    assert (
        private_access.private_metadata_violation(path, _metadata(), mode=0o600)
        == "unsafe-acl"
    )


def test_dacl_without_current_user_access_is_not_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    descriptor = _Descriptor(path, trustees=(_SYSTEM,))
    _install_windows(monkeypatch, descriptor)

    assert (
        private_access.private_metadata_violation(path, _metadata(), mode=0o600)
        == "unsafe-acl"
    )


def test_reparse_point_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    _install_windows(monkeypatch, _Descriptor(path))

    assert (
        private_access.private_metadata_violation(
            path, _metadata(reparse=True), mode=0o600
        )
        == "unsafe-reparse"
    )


def test_object_allow_ace_fails_closed_instead_of_reading_a_guid_as_a_sid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    descriptor = _Descriptor(path)
    assert descriptor.dacl is not None
    descriptor.dacl.aces.append(((5, 0), 0x1, "object-guid"))
    _install_windows(monkeypatch, descriptor)

    assert (
        private_access.private_metadata_violation(path, _metadata(), mode=0o600)
        == "unsafe-acl"
    )


def test_unknown_ace_type_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    descriptor = _Descriptor(path)
    assert descriptor.dacl is not None
    descriptor.dacl.aces.append(((99, 0), 0x1, _CURRENT))
    _install_windows(monkeypatch, descriptor)

    assert (
        private_access.private_metadata_violation(path, _metadata(), mode=0o600)
        == "unsafe-acl"
    )


def test_token_lookup_failure_is_an_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    _install_windows(monkeypatch, _Descriptor(path), token_error=True)

    with pytest.raises(OSError):
        private_access.private_metadata_violation(path, _metadata(), mode=0o600)


def test_foreign_owner_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    _install_windows(
        monkeypatch, _Descriptor(path, owner=_OTHER_USER, trustees=(_OTHER_USER,))
    )

    assert (
        private_access.private_metadata_violation(path, _metadata(), mode=0o600)
        == "wrong-owner"
    )


def test_missing_pywin32_is_an_explicit_unsupported_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    path.write_text("value", encoding="utf-8")
    _install_windows(monkeypatch, None)

    assert (
        private_access.private_metadata_violation(path, _metadata(), mode=0o600)
        == "unsupported-private-acl"
    )
    with pytest.raises(private_access.PrivateAccessUnsupported):
        private_access.harden_private_path(path, mode=0o600)


def test_hardening_applies_a_protected_current_user_dacl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    path.write_text("value", encoding="utf-8")
    path.chmod(0o666)
    descriptor = _Descriptor(path, trustees=(_CURRENT, _OTHER_USER))
    _install_windows(monkeypatch, descriptor)

    private_access.harden_private_path(path, mode=0o600)

    assert descriptor.protected is True
    assert descriptor.dacl is not None
    assert descriptor.dacl.GetAceCount() == 1
    (_kind, flags), _mask, sid = descriptor.dacl.GetAce(0)
    assert sid == _CURRENT
    # Children of a protected DACL inherit only from these flags; without
    # them a newly created child gets an empty DACL and is unusable.
    assert flags & 0x1 and flags & 0x2
    assert private_access.private_metadata_violation(path, _metadata(), mode=0o600) is None


def test_posix_without_getuid_is_unsupported_not_silently_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "getuid", raising=False)

    assert (
        private_access.private_metadata_violation(
            tmp_path, _metadata(), mode=0o600
        )
        == "unsupported-private-acl"
    )


def test_ace_fields_accepts_both_pywin32_shapes() -> None:
    sid = object()

    # pywin32 312 nests (type, flags); older builds returned a flat tuple.
    assert private_access._ace_fields(((0, 3), 0x1, sid)) == (0, 3, 0x1, sid)
    assert private_access._ace_fields((0, 3, 0x1, sid)) == (0, 3, 0x1, sid)


def test_unrecognised_ace_shape_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret"
    descriptor = _Descriptor(path)
    assert descriptor.dacl is not None
    descriptor.dacl.aces.append((0, 1))  # type: ignore[arg-type]
    _install_windows(monkeypatch, descriptor)

    with pytest.raises(OSError):
        private_access.private_metadata_violation(path, _metadata(), mode=0o600)
