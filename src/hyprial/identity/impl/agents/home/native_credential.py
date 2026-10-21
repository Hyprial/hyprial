"""Install harness-native credentials into an agent's secrets tree.

An agent runs in an isolated HOME, so it does not inherit the operator's
``~/.claude``/``~/.codex``/pi configuration.  Nothing in the product used to
write the canonical native tree (``<agent_home>/secrets/native/<harness>/``)
that the runtime reads, which is why claude agents start with
``CLAUDE_CREDENTIAL_MISSING``; this module is that writer.

Shape rules come from the read side and are enforced here, not assumed:

- the runtime reads a **regular file**, never a symlink, with mode **0600**
  (``daemon/impl/harnesses/claude/runtime.py`` rejects anything else);
- the runtime projects this tree into the isolated HOME
  (``identity/impl/agents/runtime/context.py``), so one write is enough;
- filenames are constrained to the per-harness allowlist below, so this API
  can never be used to plant an arbitrary file inside the secrets tree.

Path safety (review of 95c1b8b45, B2/B3).  The agent can write inside its own
home, so ``secrets`` or ``secrets/native`` may have been replaced by a symlink
into another agent's home.  Every access therefore walks from the agent home
with ``O_DIRECTORY | O_NOFOLLOW`` one component at a time, requires each level
to be a directory owned by this uid with mode 0700 (the same rule as the
runtime's ``_ensure_private_directory``), creates a missing level with
``mkdir`` + ``fchmod 0700`` so the umask cannot loosen it, and performs every
create, rename, read and unlink relative to the final directory descriptor.
Nothing is resolved through a path string after the walk.

:func:`install_native_credential_set` is the seeding entry point: it takes every
file the harness reads (pi has two) and checks the whole set -- names,
emptiness, existing targets -- before it writes the first file.  It then stages
every document as ``<name>.incoming`` (registered for cleanup before its first
byte is written), moves each file it replaces aside to ``<name>.previous``,
and only then renames the staged files into place.  Any I/O error removes what
this run installed and staged and renames the ``.previous`` copies back, so the
directory holds either the whole new set or the whole old one; the error says
so, and names any old file it could not put back (kept as ``.previous``).  A
process crash mid-run is the one case not rolled back: stale ``.incoming`` and
``.previous`` files (possibly plaintext) are removed by the next seed and by
logout.  The agent home itself must also be a 0700 directory of this uid (the
provisioner creates it so; the runtime's own check starts below it).  A target whose bytes already equal the incoming
document is reported ``unchanged`` and not rewritten, which makes a repeat seed
idempotent.  :func:`remove_native_credentials` is the revoke side.  No message
raised here ever contains document content.

Platform support.  Descriptor-relative no-follow I/O is a POSIX capability.  The
module still imports where it is absent -- the daemon imports this surface while
starting -- but the writers refuse with ``UNSUPPORTED`` rather than falling back
to a weaker path-based write.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from hyprial.kernel import current_uid, ipc_errors, posix_metadata_enforced

#: Credential file names the runtime reads per harness.  A caller may install
#: only these names, and only under their own harness directory.  ``pi`` keeps
#: a second file (``models-store.json``) because an empty pair is exactly the
#: 2026-10-13 autodl failure: the credential alone is not enough.
NATIVE_CREDENTIAL_FILES: dict[str, frozenset[str]] = {
    "claude": frozenset({".credentials.json"}),
    "codex": frozenset({"auth.json"}),
    "pi": frozenset({"auth.json", "models-store.json"}),
}

_STAGING_SUFFIX = ".incoming"
_PREVIOUS_SUFFIX = ".previous"


def _directory_flags() -> int:
    # O_DIRECTORY/O_NOFOLLOW are POSIX-only; computing them unguarded makes this
    # module unimportable on Windows, where the daemon imports it at start-up.
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _private_descriptor_io() -> bool:
    """Whether every descriptor-relative primitive this writer relies on exists."""

    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_NONBLOCK")
        and hasattr(os, "fchmod")
        and hasattr(os, "getuid")
        and all(
            function in getattr(os, "supports_dir_fd", frozenset())
            for function in (os.open, os.stat, os.unlink, os.rename, os.mkdir)
        )
    )


_DIRECTORY_FLAGS = _directory_flags()
_PRIVATE_DESCRIPTOR_IO = _private_descriptor_io()


class NativeCredentialError(Exception):
    """Refusal to install a native credential; ``code`` is the CLI-facing code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _require_private_descriptor_io() -> None:
    if _PRIVATE_DESCRIPTOR_IO:
        return
    raise NativeCredentialError(
        ipc_errors.UNSUPPORTED,
        "native credential writes need descriptor-relative no-follow I/O "
        "that this platform does not provide",
    )


def native_secrets_root(agent_home: Path, harness: str) -> Path:
    """The canonical directory the runtime reads for ``harness``."""

    return Path(agent_home) / "secrets" / "native" / harness


def supported_harnesses() -> tuple[str, ...]:
    return tuple(sorted(NATIVE_CREDENTIAL_FILES))


def install_native_credential(
    agent_home: Path,
    harness: str,
    filename: str,
    content: bytes,
    *,
    force: bool = False,
) -> dict[str, object]:
    """Write one allowlisted file; the single-file form of the set writer."""

    receipt = _install(agent_home, harness, [(filename, content)], force=force, whole_set=False)
    files = receipt["files"]
    assert isinstance(files, list)
    return {"provider": harness, **files[0]}


def install_native_credential_set(
    agent_home: Path,
    harness: str,
    documents: Sequence[tuple[str, bytes]],
    *,
    force: bool = False,
) -> dict[str, object]:
    """Write every credential file ``harness`` reads, or none of them."""

    return _install(agent_home, harness, documents, force=force, whole_set=True)


def remove_native_credentials(agent_home: Path, harness: str) -> dict[str, object]:
    """Delete the credential files ``harness`` reads; report which existed."""

    allowed = _allowed(harness)
    _require_private_descriptor_io()
    root = native_secrets_root(agent_home, harness)
    directory = _open_private_chain(Path(agent_home), harness, create=False)
    removed: list[str] = []
    absent: list[str] = []
    if directory is None:
        absent = [str(root / name) for name in sorted(allowed)]
        return {"provider": harness, "removed": removed, "absent": absent}
    try:
        for name in sorted(allowed):
            _unlink_quietly(name + _STAGING_SUFFIX, directory)
            _unlink_quietly(name + _PREVIOUS_SUFFIX, directory)
            if _lstat_at(name, directory) is None:
                absent.append(str(root / name))
            else:
                os.unlink(name, dir_fd=directory)
                removed.append(str(root / name))
    except OSError as error:
        raise NativeCredentialError(
            ipc_errors.DAEMON_ERROR,
            f"cannot remove credentials under {root}: {error.strerror}",
        ) from error
    finally:
        os.close(directory)
    return {"provider": harness, "removed": removed, "absent": absent}


def _allowed(harness: str) -> frozenset[str]:
    allowed = NATIVE_CREDENTIAL_FILES.get(harness)
    if allowed is None:
        raise NativeCredentialError(
            ipc_errors.PROVIDER_UNSUPPORTED,
            f"provider {harness!r} is not supported; supported: "
            + ", ".join(supported_harnesses()),
        )
    return allowed


def _install(
    agent_home: Path,
    harness: str,
    documents: Sequence[tuple[str, bytes]],
    *,
    force: bool,
    whole_set: bool,
) -> dict[str, object]:
    allowed = _allowed(harness)
    _require_private_descriptor_io()
    names = [name for name, _content in documents]
    if whole_set:
        if len(set(names)) != len(names) or set(names) != allowed:
            raise NativeCredentialError(
                ipc_errors.INVALID_ARGUMENT,
                f"{harness!r} is seeded with exactly {sorted(allowed)}; got {names}",
            )
    elif len(names) != 1 or names[0] not in allowed:
        raise NativeCredentialError(
            ipc_errors.INVALID_ARGUMENT,
            f"{harness!r} reads only {sorted(allowed)}; refusing to write {names}",
        )
    for name, content in documents:
        if not content:
            raise NativeCredentialError(
                ipc_errors.INVALID_ARGUMENT, f"refusing to install an empty {name}"
            )

    root = native_secrets_root(agent_home, harness)
    directory = _open_private_chain(Path(agent_home), harness, create=False)
    try:
        plan: list[tuple[str, bytes, bool, bool]] = []
        for name, content in documents:
            existed, unchanged = _inspect_target(name, content, directory, root)
            if existed and not unchanged and not force:
                raise NativeCredentialError(
                    ipc_errors.PROVIDER_CREDENTIAL_EXISTS,
                    f"{root / name} already exists; re-run with --force to replace it",
                )
            plan.append((name, content, existed, unchanged))
        if directory is None:
            directory = _open_private_chain(Path(agent_home), harness, create=True)
        assert directory is not None
        _write_plan(plan, directory, root)
    finally:
        if directory is not None:
            os.close(directory)
    receipts = [
        {
            "path": str(root / name),
            "bytes": len(content),
            "replaced": existed and not unchanged,
            "unchanged": unchanged,
        }
        for name, content, existed, unchanged in plan
    ]
    return {"provider": harness, "files": receipts}


def _inspect_target(
    name: str, content: bytes, directory: int | None, root: Path
) -> tuple[bool, bool]:
    if directory is None:
        return False, False
    metadata = _lstat_at(name, directory)
    if metadata is None:
        return False, False
    if not stat.S_ISREG(metadata.st_mode):
        raise NativeCredentialError(
            ipc_errors.PROVIDER_UNSAFE_PATH, f"{root / name} is not a regular file"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size != len(content):
        return True, False
    return True, _read_at(name, directory) == content


def _write_plan(
    plan: list[tuple[str, bytes, bool, bool]], directory: int, root: Path
) -> None:
    pending = [
        (name, content, existed)
        for name, content, existed, unchanged in plan
        if not unchanged
    ]
    staged: list[str] = []
    aside: list[str] = []
    installed: list[str] = []
    try:
        for name, _content, _existed in pending:
            _unlink_quietly(name + _STAGING_SUFFIX, directory)
            _unlink_quietly(name + _PREVIOUS_SUFFIX, directory)
        for name, content, _existed in pending:
            staged.append(name + _STAGING_SUFFIX)
            _create_private_at(name + _STAGING_SUFFIX, content, directory)
        for name, _content, existed in pending:
            if existed:
                os.rename(name, name + _PREVIOUS_SUFFIX, src_dir_fd=directory, dst_dir_fd=directory)
                aside.append(name)
        for name, _content, _existed in pending:
            os.rename(name + _STAGING_SUFFIX, name, src_dir_fd=directory, dst_dir_fd=directory)
            staged.remove(name + _STAGING_SUFFIX)
            installed.append(name)
    except OSError as error:
        kept = _roll_back(directory, staged=staged, aside=aside, installed=installed)
        state = (
            "the previous files are back in place"
            if not kept
            else "could not restore "
            + ", ".join(kept)
            + f"; the previous copies are kept as <name>{_PREVIOUS_SUFFIX}"
        )
        raise NativeCredentialError(
            ipc_errors.DAEMON_ERROR,
            f"writing credentials under {root} failed ({error.strerror}); "
            f"nothing from this run was kept and {state}",
        ) from error
    for name in aside:
        _unlink_quietly(name + _PREVIOUS_SUFFIX, directory)
    for name, _content, _existed in pending:
        metadata = _lstat_at(name, directory)
        if (
            metadata is None
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise NativeCredentialError(
                ipc_errors.PROVIDER_UNSAFE_PATH,
                f"{root / name} is not a 0600 regular file after writing",
            )


def _roll_back(
    directory: int, *, staged: list[str], aside: list[str], installed: list[str]
) -> list[str]:
    for name in installed:
        _unlink_quietly(name, directory)
    for staging in staged:
        _unlink_quietly(staging, directory)
    kept: list[str] = []
    for name in aside:
        try:
            os.rename(name + _PREVIOUS_SUFFIX, name, src_dir_fd=directory, dst_dir_fd=directory)
        except OSError:
            kept.append(name)
    return kept


def _open_private_chain(agent_home: Path, harness: str, *, create: bool) -> int | None:
    """Open ``secrets/native/<harness>`` under the home without following links."""

    try:
        current = os.open(agent_home, _DIRECTORY_FLAGS)
    except OSError as error:
        raise NativeCredentialError(
            ipc_errors.PROVIDER_UNSAFE_PATH,
            f"agent home {agent_home} is not a real directory ({error.strerror})",
        ) from error
    shown = agent_home
    try:
        _require_private_directory(current, shown)
        for part in ("secrets", "native", harness):
            shown = shown / part
            child = _open_or_create_directory(part, current, shown, create=create)
            os.close(current)
            current = -1
            if child is None:
                return None
            current = child
        result, current = current, -1
        return result
    finally:
        if current != -1:
            os.close(current)


def _open_or_create_directory(
    part: str, parent: int, shown: Path, *, create: bool
) -> int | None:
    made = False
    try:
        child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(part, 0o700, dir_fd=parent)
        except OSError as error:
            raise NativeCredentialError(
                ipc_errors.PROVIDER_UNSAFE_PATH,
                f"cannot create {shown}: {error.strerror}",
            ) from error
        made = True
        child = _open_directory_or_refuse(part, parent, shown)
    except OSError as error:
        raise _unsafe(shown, error) from error
    try:
        if made:
            os.fchmod(child, 0o700)
        _require_private_directory(child, shown)
    except BaseException:
        os.close(child)
        raise
    return child


def _require_private_directory(descriptor: int, shown: Path) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (
            posix_metadata_enforced()
            and (
                metadata.st_uid != current_uid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            )
        )
    ):
        raise NativeCredentialError(
            ipc_errors.PROVIDER_UNSAFE_PATH,
            f"{shown} must be a 0700 directory owned by this user",
        )


def _open_directory_or_refuse(part: str, parent: int, shown: Path) -> int:
    try:
        return os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as error:
        raise _unsafe(shown, error) from error


def _unsafe(shown: Path, error: OSError) -> NativeCredentialError:
    if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
        return NativeCredentialError(
            ipc_errors.PROVIDER_UNSAFE_PATH,
            f"{shown} is a symlink or not a directory; refusing to follow it",
        )
    return NativeCredentialError(
        ipc_errors.PROVIDER_UNSAFE_PATH, f"cannot open {shown}: {error.strerror}"
    )


def _lstat_at(name: str, directory: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _read_at(name: str, directory: int) -> bytes:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NativeCredentialError(
                ipc_errors.PROVIDER_UNSAFE_PATH, f"{name} is not a regular file"
            )
        chunks = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _create_private_at(name: str, content: bytes, directory: int) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_quietly(name: str, directory: int) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass
