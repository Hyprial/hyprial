"""Platform account facts: Windows has no ``os.getuid`` and synthesises modes."""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentHomeError
from hyprial.kernel.impl.platform.privacy import account, private_access

from .lifecycle.support import (
    _isolated_environment as _isolated_environment,
    _registry,
)

_CURRENT_SID = "S-1-5-21-1000"


class _Acl:
    def __init__(self) -> None:
        self.aces: list[tuple[object, int, object]] = []

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


class _SecurityDescriptor:
    def __init__(self, owner: str, dacl: _Acl | None) -> None:
        self.owner = owner
        self.dacl = dacl

    def GetSecurityDescriptorOwner(self) -> str:
        return self.owner

    def GetSecurityDescriptorDacl(self) -> _Acl | None:
        return self.dacl


class _Token:
    def Close(self) -> None:
        pass


def _private_dacl() -> _Acl:
    dacl = _Acl()
    dacl.AddAccessAllowedAceEx(2, 0x1 | 0x2, 0x10000000, _CURRENT_SID)
    return dacl


def _simulated_windows_security() -> private_access._WindowsSecurity:
    security = types.SimpleNamespace(
        ACL=_Acl,
        ACL_REVISION=2,
        SE_FILE_OBJECT=1,
        OWNER_SECURITY_INFORMATION=1,
        DACL_SECURITY_INFORMATION=4,
        PROTECTED_DACL_SECURITY_INFORMATION=0x80000000,
        TokenUser=1,
        OpenProcessToken=lambda *_args: _Token(),
        GetTokenInformation=lambda *_args: (_CURRENT_SID,),
        ConvertSidToStringSid=lambda sid: sid,
        GetNamedSecurityInfo=lambda *_args: _SecurityDescriptor(
            _CURRENT_SID, _private_dacl()
        ),
        SetNamedSecurityInfo=lambda *_args: None,
    )
    api = types.SimpleNamespace(GetCurrentProcess=lambda: 1)
    con = types.SimpleNamespace(TOKEN_QUERY=0x8, GENERIC_ALL=0x10000000)
    return private_access._WindowsSecurity(api, con, security)


def test_create_refuses_when_platform_privacy_cannot_be_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing POSIX owner lookup is an explicit refusal, never a skip.

    Removing the attribute leaves a POSIX platform whose ownership fences
    cannot be inspected.  Windows is served by the DACL boundary instead, but
    a platform with neither capability must not report a private home as if
    the check had passed.
    """

    if os.name == "nt":
        pytest.skip("Windows uses the DACL boundary instead of os.getuid")
    monkeypatch.delattr(os, "getuid", raising=False)
    assert account.current_uid() == 0
    assert account.posix_metadata_enforced() is False

    registry = _registry(tmp_path)
    with pytest.raises(AgentHomeError) as error:
        registry.create("worker")
    assert error.value.category == "unsupported-private-acl"
    assert registry.get("worker") is None


def test_create_uses_the_windows_acl_boundary_without_posix_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real registry create path must work from a DACL, not ``os.getuid``.

    This simulates the Windows branch on a POSIX host until the native probe
    can run: the provisioning, receipt and directory fences all go through the
    same private-access contract the Windows host will exercise.
    """

    if os.name == "nt":
        pytest.skip("this exercises the simulated Windows branch on a POSIX host")
    monkeypatch.setattr(private_access, "posix_metadata_enforced", lambda: False)
    monkeypatch.setattr(private_access, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        private_access, "_windows_security", _simulated_windows_security
    )

    registry = _registry(tmp_path)
    created = registry.create("worker")

    assert created.actor == "worker"
    assert registry.get("worker") is not None


def test_posix_owner_fence_still_rejects_a_foreign_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows guard must not turn the POSIX ownership fence off."""

    if not hasattr(os, "getuid"):
        pytest.skip("the POSIX ownership fence needs os.getuid")
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert account.current_uid() == real_uid + 1
    assert account.posix_metadata_enforced() is True

    registry = _registry(tmp_path)
    with pytest.raises(AgentHomeError) as error:
        registry.create("worker")
    assert error.value.category == "wrong-owner"
    assert registry.get("worker") is None


def test_posix_mode_fence_still_rejects_a_non_private_directory(
    tmp_path: Path,
) -> None:
    """Skipping mode equality is Windows-only, not a general relaxation."""

    if os.name == "nt":
        pytest.skip("Windows synthesises permission bits instead of enforcing them")
    registry = _registry(tmp_path)
    provisioner = registry._home
    assert provisioner is not None
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)

    with pytest.raises(AgentHomeError) as error:
        provisioner.safe_directory(loose, "worker", "test-mode", mode=0o700)
    assert error.value.category == "unsafe-mode"
