"""Account-private filesystem access on platforms without POSIX metadata.

POSIX exposes the facts this product relies on for 0700 directories and 0600
files directly in ``stat``: ``st_uid`` names the owner and the mode bits name
who may enter.  Windows exposes neither.  CPython synthesises ``st_uid`` 0 and
0666/0777 bits there, while the kernel actually decides access from the
object's security descriptor.  Treating "uid 0 and mode skipped" as private
would silently downgrade every credential and agent-home fence.

This module is the single place that answers "may only this account reach this
path".  The Windows trust boundary is explicit:

* the path must not be a symlink or any other reparse point.  A junction can
  redirect the subtree without changing the POSIX-looking metadata;
* the owner must be the current user or a Windows machine authority
  (``SYSTEM`` / ``BUILTIN\\Administrators``).  Those authorities can take
  ownership or use backup semantics independently of any DACL, so they are
  trusted by the platform, not by this code;
* the DACL must exist and every allow ACE must name only the current user or
  those machine authorities.  A ``CREATOR OWNER`` ACE is accepted because it
  resolves to the already-checked owner.  Any other allow ACE -- including
  inherited ``Users``/``Authenticated Users`` entries -- makes the path
  unprivate, even if its mask is read-only;
* the current user must still have a non-inherit-only read/full allow ACE, so
  a descriptor that locks the application itself out is not reported private.

Newly created paths are hardened with a protected DACL containing one
current-user allow ACE that children inherit, which is the Windows equivalent
of creating with 0700/0600 and a sane umask.  Validation accepts inherited
ACEs when every allow ACE is trusted, so an existing user-profile directory
does not have to be rewritten merely to pass the fence.

If pywin32 is unavailable on Windows the caller receives
``unsupported-private-acl``; it never gets a silent success.  The caller maps
that category to its own typed, sanitized error.
"""

from __future__ import annotations

import importlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from .account import current_uid, posix_metadata_enforced

__all__ = [
    "PrivateAccessUnsupported",
    "harden_private_path",
    "path_is_reparse",
    "private_metadata_violation",
    "private_tree_violation",
]

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
#: ACCESS_ALLOWED_ACE_TYPE, the only granting type whose pywin32 ``GetAce``
#: tuple is a plain ``(type, flags, mask, sid)``.  Compound, object, and
#: callback allow ACEs (4, 5, 9, 11) carry extra fields or conditions; they are
#: refused rather than parsed with the wrong position for their trustee.
_ACCESS_ALLOWED_ACE_TYPE = 0
_UNVERIFIABLE_ALLOW_ACE_TYPES = frozenset({4, 5, 9, 11})
#: Deny, audit, alarm, and mandatory-label ACE types never grant access, so
#: they cannot widen the boundary.  Any other unknown type fails closed.
_NON_GRANTING_ACE_TYPES = frozenset({1, 2, 3, 6, 7, 8, 10, 12, 13, 14, 15, 16, 17})
#: OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE.  A protected DACL is not
#: inherited from above, so without these flags a child created under the
#: hardened path would get an empty DACL -- inaccessible even to its owner.
_INHERIT_CHILDREN = 0x1 | 0x2
#: INHERIT_ONLY_ACE: the ACE applies to children, not to this object.
_INHERIT_ONLY_ACE = 0x8
#: FILE_READ_DATA/FILE_LIST_DIRECTORY, GENERIC_READ, or GENERIC_ALL.
_READ_OR_ALL = 0x1 | 0x80000000 | 0x10000000
#: Platform authorities that can already bypass a DACL.  They are part of the
#: documented Windows trust boundary, never an excuse to grant another user.
_TRUSTED_AUTHORITY_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544"})
_CREATOR_OWNER_SID = "S-1-3-0"


class PrivateAccessUnsupported(RuntimeError):
    """The platform cannot enforce or inspect account-private access."""


@dataclass(frozen=True, slots=True)
class _WindowsSecurity:
    api: ModuleType
    con: ModuleType
    security: ModuleType


def _windows_security() -> _WindowsSecurity | None:
    """Return pywin32's security modules, or ``None`` when it is not installed."""

    try:
        return _WindowsSecurity(
            importlib.import_module("win32api"),
            importlib.import_module("win32con"),
            importlib.import_module("win32security"),
        )
    except ImportError:
        return None


def path_is_reparse(metadata: os.stat_result) -> bool:
    """Whether a Windows directory entry is a reparse point (junction, link...)."""

    return bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def private_metadata_violation(
    path: Path,
    metadata: os.stat_result,
    *,
    mode: int | None,
) -> str | None:
    """Return a stable category when ``path`` is not account-private.

    ``mode`` keeps the POSIX contract identical to the caller's existing
    check.  Windows has no meaningful mode, so the security descriptor and
    reparse state decide instead.  ``None`` means only privacy, not an exact
    mode, is required.
    """

    if posix_metadata_enforced():
        if metadata.st_uid != current_uid():
            return "wrong-owner"
        if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
            return "unsafe-mode"
        return None
    if os.name != "nt":
        return "unsupported-private-acl"
    if path_is_reparse(metadata):
        return "unsafe-reparse"
    windows = _windows_security()
    if windows is None:
        return "unsupported-private-acl"
    return _windows_violation(windows, path)


def private_tree_violation(path: Path, metadata: os.stat_result) -> str | None:
    """Owner-only (POSIX) or account-private (Windows) check for tree entries.

    Tree copies keep whatever exact mode the source used; the invariant is that
    no principal other than the account can reach the entry.  On POSIX that is
    ownership plus no group/other bits; Windows uses the same descriptor fence
    as :func:`private_metadata_violation`.
    """

    if posix_metadata_enforced():
        if metadata.st_uid != current_uid():
            return "wrong-owner"
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            return "unsafe-mode"
        return None
    if os.name != "nt":
        return "unsupported-private-acl"
    return private_metadata_violation(path, metadata, mode=None)


def harden_private_path(path: Path, *, mode: int) -> None:
    """Make a path this process just created private, then verify the result.

    POSIX applies ``mode`` explicitly so umask cannot loosen it.  Windows
    replaces the DACL with a protected, current-user-only ACE.  Raises
    :class:`PrivateAccessUnsupported` when no supported primitive exists and
    ``OSError`` when the OS rejects the operation.
    """

    if posix_metadata_enforced():
        try:
            os.chmod(path, mode, follow_symlinks=False)
        except NotImplementedError as error:  # pragma: no cover - exotic POSIX
            raise PrivateAccessUnsupported(
                "this platform cannot chmod a directory entry without following links"
            ) from error
        _verify_after_hardening(path, mode)
        return
    if os.name != "nt":
        raise PrivateAccessUnsupported(
            "this platform has neither POSIX ownership nor a Windows DACL"
        )
    windows = _windows_security()
    if windows is None:
        raise PrivateAccessUnsupported(
            "pywin32's win32security module is not available"
        )
    protected = getattr(windows.security, "PROTECTED_DACL_SECURITY_INFORMATION", None)
    if protected is None:  # pragma: no cover - old pywin32
        raise PrivateAccessUnsupported(
            "this pywin32 has no protected-DACL security information"
        )
    try:
        current = _current_user_sid(windows)
        dacl = windows.security.ACL()
        add_allowed = getattr(dacl, "AddAccessAllowedAceEx", None)
        if add_allowed is None:  # pragma: no cover - very old pywin32
            raise PrivateAccessUnsupported(
                "this pywin32 cannot create an inheritable DACL entry"
            )
        add_allowed(
            windows.security.ACL_REVISION,
            _INHERIT_CHILDREN,
            windows.con.GENERIC_ALL,
            current,
        )
        windows.security.SetNamedSecurityInfo(
            str(path),
            windows.security.SE_FILE_OBJECT,
            windows.security.DACL_SECURITY_INFORMATION | protected,
            None,
            None,
            dacl,
            None,
        )
    except PrivateAccessUnsupported:
        raise
    except OSError:
        raise
    except Exception as error:  # pywintypes.error is not an OSError
        raise OSError(
            f"cannot apply a private Windows DACL to {path.name}"
        ) from error
    _verify_after_hardening(path, mode)


def _verify_after_hardening(path: Path, mode: int) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise
    violation = private_metadata_violation(path, metadata, mode=mode)
    if violation is not None:
        raise PrivateAccessUnsupported(
            f"private access was not enforced for {path.name}: {violation}"
        )


def _ace_fields(ace) -> tuple[int, int, int, object]:
    """Return ``(type, flags, mask, sid)`` from a pywin32 ACE.

    pywin32 312 returns ``((type, flags), mask, sid)``; older builds returned
    the flat ``(type, flags, mask, sid)``. An unrecognised shape raises, which
    the caller turns into a fail-closed error.
    """

    if len(ace) == 3 and isinstance(ace[0], tuple):
        (ace_type, ace_flags), access_mask, sid = ace
        return ace_type, ace_flags, access_mask, sid
    ace_type, ace_flags, access_mask, sid = ace[:4]
    return ace_type, ace_flags, access_mask, sid


def _windows_violation(windows: _WindowsSecurity, path: Path) -> str:
    try:
        descriptor = windows.security.GetNamedSecurityInfo(
            str(path),
            windows.security.SE_FILE_OBJECT,
            windows.security.OWNER_SECURITY_INFORMATION
            | windows.security.DACL_SECURITY_INFORMATION,
        )
        owner = descriptor.GetSecurityDescriptorOwner()
        dacl = descriptor.GetSecurityDescriptorDacl()
    except Exception as error:  # pywintypes.error is not an OSError
        raise OSError(
            f"cannot inspect the Windows security descriptor for {path.name}"
        ) from error
    if owner is None:
        return "unsafe-acl"
    current = _sid_string(windows, _current_user_sid(windows))
    trusted = {current, *_TRUSTED_AUTHORITY_SIDS}
    if _sid_string(windows, owner) not in trusted:
        return "wrong-owner"
    if dacl is None:
        return "unsafe-acl"
    current_can_read = False
    allowed = trusted | {_CREATOR_OWNER_SID}
    try:
        for index in range(dacl.GetAceCount()):
            ace_type, ace_flags, access_mask, sid = _ace_fields(
                dacl.GetAce(index)
            )
            if ace_type in _UNVERIFIABLE_ALLOW_ACE_TYPES:
                return "unsafe-acl"
            if ace_type in _NON_GRANTING_ACE_TYPES:
                continue
            if ace_type != _ACCESS_ALLOWED_ACE_TYPE:
                return "unsafe-acl"
            trustee = _sid_string(windows, sid)
            if trustee not in allowed:
                return "unsafe-acl"
            if (
                trustee == current
                and not ace_flags & _INHERIT_ONLY_ACE
                and access_mask & _READ_OR_ALL
            ):
                current_can_read = True
    except Exception as error:  # pywintypes.error is not an OSError
        raise OSError(
            f"cannot inspect the Windows DACL for {path.name}"
        ) from error
    if not current_can_read:
        return "unsafe-acl"
    return None


def _current_user_sid(windows: _WindowsSecurity):
    token = None
    try:
        token = windows.security.OpenProcessToken(
            windows.api.GetCurrentProcess(), windows.con.TOKEN_QUERY
        )
        return windows.security.GetTokenInformation(token, windows.security.TokenUser)[0]
    except Exception as error:  # pywintypes.error is not an OSError
        raise OSError("cannot read the current Windows user SID") from error
    finally:
        if token is not None:
            try:
                token.Close()
            except Exception:
                pass


def _sid_string(windows: _WindowsSecurity, sid) -> str:
    try:
        return str(windows.security.ConvertSidToStringSid(sid))
    except Exception as error:  # pywintypes.error is not an OSError
        raise OSError("cannot convert a Windows security identifier") from error
