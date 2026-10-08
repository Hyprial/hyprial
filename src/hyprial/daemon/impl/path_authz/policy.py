"""Who may make the daemon read which local file, and how it is read.

Rule (Allen, 2026-09-30): a caller may name a file only inside its own
working area, and the hyprial home is never readable this way.

- An authenticated agent session may read under its own
  ``<home>/agents/<name>/workspace`` and its registered cwd.
- The local operator may read anywhere except the hyprial home.
- Inside the hyprial home only the caller's own workspace is readable;
  another agent's home, ``secrets/`` and every daemon file are refused.
  An attachment policy may additionally admit an app actor's own
  ``<home>/apps/<app>/reports`` directory.
  The daemon state dir is refused too; it may live outside the home
  (``HARNESS_STATE_DIR``).

Mechanics (hq, 2026-10-06): resolve with ``realpath`` first and compare whole
path components (``/a/ws2`` is not inside ``/a/ws``); open with
``O_NOFOLLOW``; ``fstat`` the descriptor to confirm a regular file; re-check
the descriptor's own path where the platform reports it; compare the size
before and after the read; and copy the bytes from that one descriptor.
The path is never opened twice, so a symlink swapped in after the decision
cannot redirect the read.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

REASON_OUTSIDE_ALLOWED_ROOTS = "outside-allowed-roots"
REASON_HYPRIAL_HOME = "hyprial-home"
REASON_OTHER_AGENT_HOME = "other-agent-home"
REASON_SYMLINK = "symlink"
REASON_CHANGED = "changed"
REASON_UNVERIFIED = "unverifiable"
REASON_NOT_FOUND = "not-found"
REASON_UNREADABLE = "unreadable"
REASON_NOT_REGULAR = "not-regular"
REASON_EMPTY = "empty"
REASON_TOO_LARGE = "too-large"
REASON_HARDLINK = "hardlinked"

_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_BINARY", 0)
)
_READ_CHUNK = 1024 * 1024


class PathRefused(Exception):
    """The daemon will not read this path for this caller.

    ``path`` is the resolved path when it is safe to echo, and ``None`` when
    the path lies inside the hyprial home (the message names the rule only).
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        path: str | None,
        size_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.path = path
        self.size_bytes = size_bytes


@dataclass(frozen=True, slots=True)
class PathPolicy:
    """Resolved roots for one caller; build it with a constructor below."""

    hyprial_home: Path
    # ``None`` means anywhere outside the hyprial home (the operator).
    allowed_roots: tuple[Path, ...] | None
    # The one subtree of the hyprial home this caller may read.
    own_workspace: Path | None = None
    # Attachment-only exception for an app actor's own reports directory.
    own_app_reports: Path | None = None
    # Daemon-owned roots outside the home (the state dir), refused like it.
    denied_roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorizedFile:
    """Bytes copied from one authorized, regular file."""

    path: Path
    name: str
    data: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def _real(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(os.path.expanduser(os.fspath(path))))


def _within(path: Path, root: Path) -> bool:
    # Component-wise, never a string prefix: /a/ws2 is not inside /a/ws.
    if path == root or path.is_relative_to(root):
        return True
    # realpath keeps the caller's spelling on a case-insensitive volume
    # (APFS: ~/.HYPRIAL is ~/.hyprial), so also compare directory identity:
    # an ancestor that IS the root, by device and inode, means inside.
    # lstat, never stat: roots are realpath'd when the policy is built, so a
    # root that is a symlink NOW was swapped in afterwards, and following it
    # would make its target count as inside the root.
    try:
        root_stat = os.lstat(root)
    except OSError:
        return False
    if stat.S_ISLNK(root_stat.st_mode):
        return False
    for ancestor in (path, *path.parents):
        try:
            if os.path.samestat(os.stat(ancestor), root_stat):
                return True
        except OSError:
            continue
    return False


def _denied(state_dir: str | os.PathLike[str] | None) -> tuple[Path, ...]:
    return (_real(state_dir),) if state_dir is not None else ()


def operator_policy(
    hyprial_home: str | os.PathLike[str],
    *,
    state_dir: str | os.PathLike[str] | None = None,
) -> PathPolicy:
    """The local owner at the terminal: anywhere except the hyprial home."""

    return PathPolicy(
        hyprial_home=_real(hyprial_home),
        allowed_roots=None,
        denied_roots=_denied(state_dir),
    )


def _own_workspace(
    home: Path, workspace: str | os.PathLike[str] | None
) -> Path | None:
    """The caller's workspace, only where the home layout says it lives.

    The workspace is the one subtree of the home a session may read, so it
    must resolve to exactly ``<home>/agents/<name>/workspace``.  A workspace,
    or an ``agents/<name>`` directory, replaced by a symlink (to the home
    itself, to ``secrets/``, to another agent) resolves elsewhere and is
    dropped rather than trusted.
    """

    if workspace is None:
        return None
    given = Path(os.path.expanduser(os.fspath(workspace)))
    name = given.parent.name
    if given.name != "workspace" or not name or name in {".", ".."}:
        return None
    expected = home / "agents" / name / "workspace"
    return expected if _real(given) == expected else None


def session_policy(
    hyprial_home: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str] | None,
    cwd: str | os.PathLike[str] | None,
    state_dir: str | os.PathLike[str] | None = None,
) -> PathPolicy:
    """An authenticated agent session: its workspace and its registered cwd.

    A cwd of ``/``, the user's home directory, or any directory that
    contains the hyprial home is not used as a root -- it would allow
    everything.  The workspace still applies.
    """

    home = _real(hyprial_home)
    denied = _denied(state_dir)
    roots: list[Path] = []
    own = _own_workspace(home, workspace)
    if own is not None:
        roots.append(own)
    if cwd is not None and os.path.isabs(os.path.expanduser(os.fspath(cwd))):
        resolved = _real(cwd)
        too_broad = (
            resolved == Path(resolved.anchor)
            or resolved == _real(Path.home())
            or _within(home, resolved)
            or any(_within(root, resolved) for root in denied)
        )
        if not too_broad:
            roots.append(resolved)
    return PathPolicy(
        hyprial_home=home,
        allowed_roots=tuple(roots),
        own_workspace=own,
        denied_roots=denied,
    )


def _own_app_reports(
    home: Path,
    *,
    actor_name: str | None,
    cwd: str | os.PathLike[str] | None,
) -> Path | None:
    """Return the reports root named by the session's registered facts.

    The registered cwd identifies the app only when it resolves to the exact
    app layout path, and the session's local actor name must equal that app
    name.  Neither fact comes from the caller's requested attachment path,
    but neither is authenticated either: any local socket caller can register
    a session with this name and cwd (binding apps to daemon-owned facts is
    an open design question).  The root itself must be the real
    ``<home>/apps/<app>/reports`` directory, as ``_own_workspace`` requires of
    the workspace: replaced by a symlink it is dropped, because containment
    compares directory identity and would otherwise follow the link.
    """

    if actor_name is None or cwd is None:
        return None
    given = Path(os.path.expanduser(os.fspath(cwd)))
    if not given.is_absolute() or given.name != "agent-home":
        return None
    app = given.parent.name
    if not app or app in {".", ".."} or actor_name != app:
        return None
    expected = home / "apps" / app / "agent-home"
    if _real(given) != expected:
        return None
    reports = home / "apps" / app / "reports"
    if reports.is_symlink() or _real(reports) != reports or not reports.is_dir():
        return None
    return reports


def session_attachment_policy(
    hyprial_home: str | os.PathLike[str],
    *,
    actor_name: str | None,
    workspace: str | os.PathLike[str] | None,
    cwd: str | os.PathLike[str] | None,
    state_dir: str | os.PathLike[str] | None = None,
) -> PathPolicy:
    """A session policy with the narrow app-reports attachment exception."""

    base = session_policy(
        hyprial_home,
        workspace=workspace,
        cwd=cwd,
        state_dir=state_dir,
    )
    reports = _own_app_reports(base.hyprial_home, actor_name=actor_name, cwd=cwd)
    if reports is None:
        return base
    assert base.allowed_roots is not None
    return PathPolicy(
        hyprial_home=base.hyprial_home,
        allowed_roots=(*base.allowed_roots, reports),
        own_workspace=base.own_workspace,
        own_app_reports=reports,
        denied_roots=base.denied_roots,
    )


def _decide(path: Path, policy: PathPolicy) -> None:
    home = policy.hyprial_home
    own_home_roots = tuple(
        root
        for root in (policy.own_workspace, policy.own_app_reports)
        if root is not None
    )
    if _within(path, home) and not any(
        _within(path, root) for root in own_home_roots
    ):
        if _within(path, home / "agents"):
            raise PathRefused(
                REASON_OTHER_AGENT_HOME,
                "files under an agent home are readable only from that "
                "agent's own workspace",
                path=None,
            )
        raise PathRefused(
            REASON_HYPRIAL_HOME,
            "files under the hyprial home cannot be attached or read this way",
            path=None,
        )
    if any(_within(path, root) for root in policy.denied_roots):
        raise PathRefused(
            REASON_HYPRIAL_HOME,
            "daemon state files cannot be attached or read this way",
            path=None,
        )
    roots = policy.allowed_roots
    if roots is not None and not any(_within(path, root) for root in roots):
        raise PathRefused(
            REASON_OUTSIDE_ALLOWED_ROOTS,
            f"{path} is outside the caller's workspace and working directory",
            path=str(path),
        )


def _descriptor_path(fd: int) -> Path | None:
    """Where the open descriptor really points.

    ``None`` only on a platform that cannot report it (the inode comparison
    is then the last check).  On Darwin and Linux a failure to read it is a
    refusal: aliases such as ``/.vol/<dev>/<ino>`` and a parent directory
    swapped after the decision are caught only here.
    """

    try:
        if sys.platform == "darwin":
            import fcntl

            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
            return _real(raw.rstrip(b"\0").decode())
        if sys.platform.startswith("linux"):
            return _real(os.readlink(f"/proc/self/fd/{fd}"))
    except (OSError, AttributeError, UnicodeDecodeError, ValueError) as error:
        raise PathRefused(
            REASON_UNVERIFIED,
            "the opened file's real location could not be verified",
            path=None,
        ) from error
    return None


def _open(path: Path) -> int:
    try:
        return os.open(path, _OPEN_FLAGS)
    except FileNotFoundError as error:
        raise PathRefused(
            REASON_NOT_FOUND, f"{path} does not exist", path=str(path)
        ) from error
    except OSError as error:
        if os.path.islink(path):
            # O_NOFOLLOW refused a final component that became a symlink
            # after realpath resolved it: the path changed under us.
            raise PathRefused(
                REASON_SYMLINK,
                f"{path} was replaced by a symlink while being read",
                path=str(path),
            ) from error
        raise PathRefused(
            REASON_UNREADABLE, f"{path} cannot be opened: {error}", path=str(path)
        ) from error


def _read_exact(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size + 1  # one extra byte reveals growth during the read
    while remaining > 0:
        chunk = os.read(fd, min(_READ_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_authorized_file(
    raw_path: str | os.PathLike[str],
    policy: PathPolicy,
    *,
    max_bytes: int,
) -> AuthorizedFile:
    """Authorize ``raw_path`` for ``policy`` and copy its bytes once.

    Raises :class:`PathRefused` with a ``REASON_*`` code.  Nothing is read
    from a path the policy refuses, so a refusal never reveals whether a file
    inside the hyprial home exists.
    """

    if not os.path.isabs(os.path.expanduser(os.fspath(raw_path))):
        raise PathRefused(
            REASON_OUTSIDE_ALLOWED_ROOTS,
            f"{os.fspath(raw_path)!r} must be an absolute path",
            path=os.fspath(raw_path),
        )
    resolved = _real(raw_path)
    _decide(resolved, policy)
    try:
        before_open = os.stat(resolved)
    except FileNotFoundError as error:
        raise PathRefused(
            REASON_NOT_FOUND, f"{resolved} does not exist", path=str(resolved)
        ) from error
    except OSError as error:
        raise PathRefused(
            REASON_UNREADABLE,
            f"{resolved} cannot be inspected: {error}",
            path=str(resolved),
        ) from error
    if not stat.S_ISREG(before_open.st_mode):
        raise PathRefused(
            REASON_NOT_REGULAR, f"{resolved} is not a regular file", path=str(resolved)
        )
    fd = _open(resolved)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PathRefused(
                REASON_NOT_REGULAR,
                f"{resolved} is not a regular file",
                path=str(resolved),
            )
        if policy.allowed_roots is not None and opened.st_nlink > 1:
            # A hard link has its own path, so realpath, containment and the
            # descriptor re-judge all see the allowed root even when the same
            # inode also lives in secrets/ or another agent's home.  Sessions
            # may not read a multiply-linked file; the operator is unchanged.
            # The refusal never echoes the path (the link may name a secret).
            # Limit: a link count cannot see a link made to a secret that was
            # later rotated by atomic replace (the old inode keeps one link),
            # so rotation must treat the old secret as revoked.
            raise PathRefused(
                REASON_HARDLINK, "a multiply-linked file was refused", path=None
            )
        actual = _descriptor_path(fd)
        if actual is not None:
            # An intermediate directory swapped after realpath would land
            # the open somewhere else: judge where it really landed.
            _decide(actual, policy)
        if (opened.st_dev, opened.st_ino) != (before_open.st_dev, before_open.st_ino):
            raise PathRefused(
                REASON_CHANGED,
                f"{resolved} changed while being read",
                path=str(resolved),
            )
        size = opened.st_size
        if size == 0:
            raise PathRefused(REASON_EMPTY, f"{resolved} is empty", path=str(resolved))
        if size > max_bytes:
            raise PathRefused(
                REASON_TOO_LARGE,
                f"{resolved} is {size} bytes; the limit is {max_bytes}",
                path=str(resolved),
                size_bytes=size,
            )
        try:
            data = _read_exact(fd, size)
            after = os.fstat(fd)
        except OSError as error:
            raise PathRefused(
                REASON_UNREADABLE,
                f"{resolved} cannot be read: {error}",
                path=str(resolved),
            ) from error
        if (
            len(data) != size
            or after.st_size != size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise PathRefused(
                REASON_CHANGED,
                f"{resolved} changed while being read",
                path=str(resolved),
            )
    finally:
        os.close(fd)
    return AuthorizedFile(path=resolved, name=resolved.name, data=data)
