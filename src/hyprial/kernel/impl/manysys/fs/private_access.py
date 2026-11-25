# Decision record (manysys/fs/private_access, W1-MANYSYS 2026-11-21):
# - Migrated from kernel/impl/platform/privacy/private_access.py into
#   manysys/fs.
# - Windows ACE constants:
#   * _ACCESS_ALLOWED_ACE_TYPE (0) is the only granting ACE whose pywin32
#     GetAce tuple is a plain (type, flags, mask, sid) or ((type, flags), mask,
#     sid); compound/object/callback allow ACEs (4, 5, 9, 11) are refused.
#   * _NON_GRANTING_ACE_TYPES (1..3, 6..8, 10, 12..17) never widen access; any
#     other unknown ACE type fails closed.
#   * _INHERIT_CHILDREN (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE = 0x3) is
#     required on protected DACLs so newly created children inherit owner
#     access instead of an empty DACL.
#   * _INHERIT_ONLY_ACE (0x8) applies only to children, never the object itself.
#   * _READ_OR_ALL (0x1 | 0x80000000 | 0x10000000) checks current-user read or
#     full access.
#   * _TRUSTED_AUTHORITY_SIDS (SYSTEM S-1-5-18, Administrators S-1-5-32-544)
#     and CREATOR OWNER (S-1-3-0) are part of the Windows kernel trust boundary.
# - OWNER RIGHTS (S-1-3-4) is a trusted trustee (Windows bootstrap ACL fix,
#   PR #3106 acceptance, 2026-11-23).  CPython 3.12.4+ (gh-118486)
#   ``os.mkdir(path, 0o700)`` on Windows applies the protected DACL
#   ``D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)``, and every child
#   created below it inherits those three ACEs; the product has many such
#   mkdir call sites that are not re-hardened.  Security argument: an OW ACE
#   grants access only to whoever owns the object at access-check time, and
#   the owner is validated first (current user, SYSTEM or Administrators,
#   otherwise ``wrong-owner``).  Changing the owner needs WRITE_OWNER or
#   SeTakeOwnership/SeRestore, which only those same trusted principals hold
#   under an accepted DACL.  OW also suppresses the owner's implicit
#   READ_CONTROL/WRITE_DAC, so it never widens access beyond the owner.  So
#   OW always resolves to an already-checked principal.  It is not the same
#   mechanism as CREATOR OWNER: that SID is a placeholder replaced by the
#   creator's SID when a child inherits the ACE and grants nothing on the
#   object itself, while OW is evaluated against the current owner at every
#   access check.  Both end at a principal this validator has checked.
#   An OW read/full ACE that is not inherit-only counts as current-user read
#   only when the owner IS the current user; with an Administrators/SYSTEM
#   owner and no current-user ACE the path stays ``unsafe-acl`` (a
#   non-elevated process of the same account would be locked out).
#   Users, Authenticated Users, Everyone and every other SID stay untrusted.
# - write_private_file_atomic captures fstat metadata and closes the temporary
#   descriptor BEFORE os.link/os.replace because Windows refuses link/replace
#   on an open file (sharing violation, WinError 32).
# - pywintypes.error is not a subclass of OSError on all builds, so pywin32
#   security inspection/hardening translates non-OSError exceptions to OSError.
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
  those machine authorities.  ``CREATOR OWNER`` and ``OWNER RIGHTS`` ACEs
  are accepted because they resolve to the already-checked owner (the latter
  is what CPython 3.12.4+ (gh-118486) ``os.mkdir(path, 0o700)`` writes on
  Windows).  Any other allow ACE -- including
  inherited ``Users``/``Authenticated Users`` entries -- makes the path
  unprivate, even if its mask is read-only;
* the current user must still have a non-inherit-only read/full allow ACE
  (an ``OWNER RIGHTS`` ACE counts only when the current user is the owner), so
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

from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import stat
from types import ModuleType
import uuid

from .account import current_uid, posix_metadata_enforced
from .durability import fsync_directory
from .file_lock import ProcessFileLock

__all__ = [
    "PrivateAccessUnsupported",
    "append_private_file",
    "harden_private_path",
    "inspect_private_file",
    "open_private_file",
    "path_is_reparse",
    "private_access_supported",
    "private_config_file_violation",
    "private_metadata_violation",
    "private_tree_violation",
    "read_private_file",
    "secure_private_directory",
    "write_private_file_atomic",
]

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_ACCESS_ALLOWED_ACE_TYPE = 0
_UNVERIFIABLE_ALLOW_ACE_TYPES = frozenset({4, 5, 9, 11})
_NON_GRANTING_ACE_TYPES = frozenset({1, 2, 3, 6, 7, 8, 10, 12, 13, 14, 15, 16, 17})
_INHERIT_CHILDREN = 0x1 | 0x2
_INHERIT_ONLY_ACE = 0x8
_READ_OR_ALL = 0x1 | 0x80000000 | 0x10000000
_TRUSTED_AUTHORITY_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544"})
_CREATOR_OWNER_SID = "S-1-3-0"
_OWNER_RIGHTS_SID = "S-1-3-4"


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


def private_config_file_violation(
    path: Path, metadata: os.stat_result
) -> str | None:
    """Return why a request-http config file is not safe to trust, else None.

    The two platforms enforce the same boundary by different means.  POSIX
    keeps this file's historical contract: owner and exact mode are not
    constrained, but no group or other principal may write it.  Windows has
    no mode bits that describe access, so the account-private security
    descriptor fence -- reparse state, owner, and every allow ACE -- decides.
    """

    if posix_metadata_enforced():
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return "unsafe-mode"
        return None
    return private_metadata_violation(path, metadata, mode=None)


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
        except NotImplementedError as error:  # pragma: no cover
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
    if protected is None:  # pragma: no cover
        raise PrivateAccessUnsupported(
            "this pywin32 has no protected-DACL security information"
        )
    try:
        current = _current_user_sid(windows)
        dacl = windows.security.ACL()
        add_allowed = getattr(dacl, "AddAccessAllowedAceEx", None)
        if add_allowed is None:  # pragma: no cover
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
    except Exception as error:
        raise OSError(
            f"cannot apply a private Windows DACL to {path.name}"
        ) from error
    _verify_after_hardening(path, mode)


def private_access_supported() -> bool:
    """Whether this runtime can enforce account-private filesystem access."""

    return posix_metadata_enforced() or _windows_security() is not None


def _validate_ancestry(path: Path) -> None:
    for parent in path.absolute().parents:
        try:
            metadata = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or path_is_reparse(metadata):
            raise PrivateAccessUnsupported("unsafe-ancestry")
        break


def secure_private_directory(path: Path, *, create: bool = False, mode: int | None = 0o700) -> Path:
    """Return a validated private directory, optionally creating missing ancestors."""

    path = Path(path).absolute()
    _validate_ancestry(path)
    if create:
        harden_mode = 0o700 if mode is None else mode
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=harden_mode)
            harden_private_path(directory, mode=harden_mode)
        harden_private_path(path, mode=harden_mode)
        _validate_ancestry(path)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise PrivateAccessUnsupported("unsafe-directory")
    violation = private_metadata_violation(path, metadata, mode=mode)
    if violation is not None:
        raise PrivateAccessUnsupported(violation)
    return path


def inspect_private_file(path: Path, *, mode: int = 0o600) -> os.stat_result:
    """Validate one private regular file without opening its contents."""

    _validate_ancestry(path)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise PrivateAccessUnsupported("unsafe-file")
    violation = private_metadata_violation(path, metadata, mode=mode)
    if violation is not None:
        raise PrivateAccessUnsupported(violation)
    return metadata


@contextmanager
def open_private_file(path: Path, *, mode: int = 0o600):
    """Yield a verified private descriptor and its metadata without reading bytes."""

    previous = inspect_private_file(path, mode=mode)
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (previous.st_dev, previous.st_ino)
        ):
            raise PrivateAccessUnsupported("unsafe-file")
        violation = private_metadata_violation(path, metadata, mode=mode)
        if violation is not None:
            raise PrivateAccessUnsupported(violation)
        yield descriptor, metadata
    finally:
        os.close(descriptor)


def read_private_file(
    path: Path, *, mode: int = 0o600, max_bytes: int | None = None
) -> tuple[bytes, os.stat_result]:
    """Read one private regular file with a no-follow open and optional size cap."""

    with open_private_file(path, mode=mode) as (descriptor, metadata):
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
        if max_bytes is not None and len(data) > max_bytes:
            raise PrivateAccessUnsupported("file-too-large")
        return data, metadata


def write_private_file_atomic(
    path: Path, data: bytes, *, mode: int = 0o600, exclusive: bool = False
) -> os.stat_result:
    """Write bytes fully and publish atomically, optionally refusing replacement."""

    path = Path(path).absolute()
    secure_private_directory(path.parent, create=True)
    _validate_ancestry(path)
    if exclusive and path.exists():
        raise FileExistsError(path)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        mode,
    )
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
        harden_private_path(temporary, mode=mode)
        metadata = os.fstat(descriptor)
        os.close(descriptor)
        descriptor = -1
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        fsync_directory(path.parent)
        return metadata
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_at(descriptor: int, length: int, offset: int) -> bytes:
    """Read without moving the shared file position.

    ``os.pread`` does not exist on Windows; ``seek`` + ``read`` under the
    append lock gives the same "still ends with a newline?" answer without
    adding a race outside the existing lock.
    """

    pread = getattr(os, "pread", None)
    if pread is not None:
        return pread(descriptor, length, offset)
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, offset, os.SEEK_SET)
        return os.read(descriptor, length)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


def append_private_file(path: Path, data: bytes, *, mode: int = 0o600) -> os.stat_result:
    """Append one complete row and roll this call back if its write or flush fails."""

    path = Path(path).absolute()
    secure_private_directory(path.parent, create=True)
    lock = ProcessFileLock(path.parent / f".{path.name}.lock")
    lock.acquire(timeout=15.0)
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
            mode,
        )
        harden_private_path(path, mode=mode)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PrivateAccessUnsupported("unsafe-file")
        violation = private_metadata_violation(path, metadata, mode=mode)
        if violation is not None:
            raise PrivateAccessUnsupported(violation)
        size = metadata.st_size
        encoded = data
        if size and _read_at(descriptor, 1, size - 1) != b"\n":
            encoded = b"\n" + data
        try:
            view = memoryview(encoded)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
            return os.fstat(descriptor)
        except OSError:
            os.ftruncate(descriptor, size)
            raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        lock.release()


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


def _windows_violation(windows: _WindowsSecurity, path: Path) -> str | None:
    try:
        descriptor = windows.security.GetNamedSecurityInfo(
            str(path),
            windows.security.SE_FILE_OBJECT,
            windows.security.OWNER_SECURITY_INFORMATION
            | windows.security.DACL_SECURITY_INFORMATION,
        )
        owner = descriptor.GetSecurityDescriptorOwner()
        dacl = descriptor.GetSecurityDescriptorDacl()
    except Exception as error:
        raise OSError(
            f"cannot inspect the Windows security descriptor for {path.name}"
        ) from error
    if owner is None:
        return "unsafe-acl"
    current = _sid_string(windows, _current_user_sid(windows))
    trusted = {current, *_TRUSTED_AUTHORITY_SIDS}
    owner_sid = _sid_string(windows, owner)
    if owner_sid not in trusted:
        return "wrong-owner"
    if dacl is None:
        return "unsafe-acl"
    current_can_read = False
    allowed = trusted | {_CREATOR_OWNER_SID, _OWNER_RIGHTS_SID}
    reads_as_current = {current}
    if owner_sid == current:
        reads_as_current.add(_OWNER_RIGHTS_SID)
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
                trustee in reads_as_current
                and not ace_flags & _INHERIT_ONLY_ACE
                and access_mask & _READ_OR_ALL
            ):
                current_can_read = True
    except Exception as error:
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
    except Exception as error:
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
    except Exception as error:
        raise OSError("cannot convert a Windows security identifier") from error
