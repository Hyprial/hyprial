# Decision record (native_credential, W1E-1 2026-11-21):
# - Per Owner Shell-2 ruling, removed POSIX-only descriptor-relative traversal
#   (dir_fd, O_DIRECTORY, O_NOFOLLOW, O_NONBLOCK, fchmod) and the
#   _require_private_descriptor_io UNSUPPORTED gate so Windows and POSIX share
#   one portable implementation.
# - Inner-home redundant st_uid and 0o600/0o700 mode assertions are dropped in
#   favor of the HYPRIAL_HOME root boundary, while preserving the per-harness
#   filename allowlist (NATIVE_CREDENTIAL_FILES), single-pass lstat +
#   manysys.path_is_reparse link/reparse rejection, and all-or-nothing
#   .incoming/.previous os.replace staging with rollback.
# - Post-promotion .previous backup cleanup converts OSError (such as Windows
#   sharing violations or read-only attributes) into NativeCredentialError with
#   DAEMON_ERROR without unlinking already-promoted targets, preventing
#   multi-file partial-unlink data loss while keeping leftover .previous files
#   visible as partial state, keeping import_record invalidated, and cleaning
#   stale .incoming/.previous residue across the full plan on subsequent runs.
"""Install harness-native credentials into an agent's secrets tree.

An agent runs in an isolated HOME, so it does not inherit the operator's
``~/.claude``/``~/.codex``/pi configuration.  This module writes and inspects
the canonical native tree (``<agent_home>/secrets/native/<harness>/``) that the
runtime consumes.

Preserved invariants:

- filenames are constrained to the per-harness allowlist below, so this API
  can never be used to plant an arbitrary file inside the secrets tree;
- every traversed directory and target entry is checked with ``lstat`` and
  ``manysys.path_is_reparse`` so symlinks and Windows junctions/reparse points
  are refused rather than followed;
- :func:`install_native_credential_set` stages every file as
  ``<name>.incoming``, moves existing files aside to ``<name>.previous`` with
  ``os.replace``, promotes the new set with ``os.replace`` (rolling the whole
  set back if staging or promotion fails), and raises ``DAEMON_ERROR`` if
  post-promotion ``.previous`` cleanup fails so partial backup residue never
  retains or acquires import provenance.  No error message ever carries
  credential bytes.

Native import records are stored by the Agent registry outside the native tree.
All entry points share a per-home/harness lock.  A registry record binds the
Agent incarnation, home resource and bounded file digest; invalidation precedes
writes, and a failed write restores old provenance only after matching the old
digest.
"""

from __future__ import annotations

import errno
import os
import stat
import uuid
from collections.abc import Sequence
from pathlib import Path

from hyprial.kernel import ipc_errors, manysys
from .native_imports import NativeImportRecord, serialized
from .native_imports.manifest import document_digest, observe

NATIVE_CREDENTIAL_FILES: dict[str, frozenset[str]] = {
    "claude": frozenset({".credentials.json"}),
    "codex": frozenset({"auth.json"}),
    "pi": frozenset({"auth.json", "models-store.json"}),
    "agy": frozenset({"antigravity-oauth-token"}),
}

_STAGING_SUFFIX = ".incoming"
_PREVIOUS_SUFFIX = ".previous"
_CLAUDE_LAUNCH_PROVISIONED = ".hyprial-launch-provisioned"


class NativeCredentialError(Exception):
    """Refusal to install a native credential; ``code`` is the CLI-facing code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def native_secrets_root(agent_home: Path, harness: str) -> Path:
    """The canonical directory the runtime reads for ``harness``."""

    return Path(agent_home) / "secrets" / "native" / harness


def prepare_legacy_claude_native_root(agent_home: Path) -> tuple[Path, bool]:
    """Publish a private root with negative provenance before making it visible.

    Existing roots retain file-less/Keychain admission. New roots remain missing
    across retries until an explicit native installation removes the marker.
    Only the actor lifecycle owner calls this on its verified home.
    """

    home = Path(agent_home)
    _require_real_directory(home, is_home=True)
    native_parent = home
    for part in ("secrets", "native"):
        native_parent = _ensure_child_directory(native_parent / part, create=True)
        assert native_parent is not None
    root = native_parent / "claude"
    temporary: Path | None = None
    try:
        if _lstat_entry(root) is None:
            temporary = native_parent / f".claude-launch-{uuid.uuid4().hex}"
            temporary.mkdir(mode=0o700)
            _require_real_directory(temporary)
            _create_private_at(_CLAUDE_LAUNCH_PROVISIONED, b"", temporary)
            manysys.fsync_directory(temporary)
            try:
                os.rename(temporary, root)
                temporary = None
                manysys.fsync_directory(native_parent)
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY, errno.EACCES) and _lstat_entry(root) is None:
                    raise
        _require_real_directory(root)
        provisioned = _lstat_at(_CLAUDE_LAUNCH_PROVISIONED, root) is not None
        return root, provisioned
    finally:
        if temporary is not None:
            _unlink_quietly(_CLAUDE_LAUNCH_PROVISIONED, temporary)
            try:
                temporary.rmdir()
            except FileNotFoundError:
                pass


def supported_harnesses() -> tuple[str, ...]:
    return tuple(sorted(NATIVE_CREDENTIAL_FILES))


@serialized
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


@serialized
def install_native_credential_set(
    agent_home: Path,
    harness: str,
    documents: Sequence[tuple[str, bytes]],
    *,
    force: bool = False,
    import_record: NativeImportRecord | None = None,
) -> dict[str, object]:
    """Write every credential file ``harness`` reads, or none of them."""

    return _install(agent_home, harness, documents, force=force, whole_set=True, import_record=import_record)


@serialized
def read_native_credential_set(agent_home: Path, harness: str) -> list[tuple[str, bytes]]:
    """Read only an explicitly authorized owner's private native files."""

    allowed = _allowed(harness)
    directory = _open_private_chain(Path(agent_home), harness, create=False)
    if directory is None:
        raise NativeCredentialError(ipc_errors.PROVIDER_SOURCE_MISSING, "native default source is absent")
    documents: list[tuple[str, bytes]] = []
    for name in sorted(allowed):
        metadata = _lstat_at(name, directory)
        if metadata is None:
            raise NativeCredentialError(ipc_errors.PROVIDER_SOURCE_MISSING, "native default file is absent")
        if _is_link_or_non_regular(metadata):
            raise NativeCredentialError(ipc_errors.PROVIDER_UNSAFE_PATH, "native default file is not private")
        content = _read_at(name, directory)
        if not content:
            raise NativeCredentialError(ipc_errors.PROVIDER_SOURCE_MISSING, "native default file is empty")
        documents.append((name, content))
    return documents


@serialized
def inspect_native_credentials(
    agent_home: Path, harness: str, *, import_record: NativeImportRecord | None = None,
) -> dict[str, object]:
    """Observe allowlisted private files and bounded import provenance.

    File timestamps do not establish import provenance or authentication.
    A supplied registry record permits internal digest reads up to 1 MiB per
    file. Only basenames, completeness and an observed import time are returned;
    unsafe paths raise rather than being reported as an empty inventory.
    """

    allowed = _allowed(harness)
    directory = _open_private_chain(Path(agent_home), harness, create=False)
    present: list[str] = []
    imported_at = None
    confirmed_at = None
    if directory is not None:
        for name in sorted(allowed):
            metadata = _lstat_at(name, directory)
            if metadata is None:
                continue
            if _is_link_or_non_regular(metadata):
                raise NativeCredentialError(ipc_errors.PROVIDER_UNSAFE_PATH, "native credential file is not private")
            if metadata.st_size > 0:
                present.append(name)
        incomplete = any(
            _lstat_at(name + suffix, directory) is not None
            for name in allowed for suffix in (_STAGING_SUFFIX, _PREVIOUS_SUFFIX)
        )
        exists = any(_lstat_at(name, directory) is not None for name in allowed)
        if (import_record is not None and not incomplete and set(present) == allowed
                and import_record.exists(harness)):
            manifest = observe(directory, allowed)
            imported_at = import_record.read(harness, manifest)
            confirmed_at = import_record.confirmed_at(harness, manifest)
    else:
        incomplete = exists = False
    missing = sorted(allowed.difference(present))
    state = "partial" if incomplete or (exists and missing) else "stored" if present else "absent"
    result: dict[str, object] = {"state": state, "files": present, "missingFiles": missing}
    if import_record is not None:
        result["importedAt"] = imported_at
        result["startupConfirmedAt"] = confirmed_at
    return result


@serialized
def remove_native_credentials(
    agent_home: Path, harness: str, *, import_record: NativeImportRecord | None = None,
) -> dict[str, object]:
    """Delete the credential files ``harness`` reads; report which existed."""

    allowed = _allowed(harness)
    root = native_secrets_root(agent_home, harness)
    directory = _open_private_chain(Path(agent_home), harness, create=False)
    removed: list[str] = []
    absent: list[str] = []
    try:
        if import_record is not None:
            import_record.clear(harness)
        if directory is None:
            absent = [str(root / name) for name in sorted(allowed)]
            return {"provider": harness, "removed": removed, "absent": absent}
        for name in sorted(allowed):
            _unlink_quietly(name + _STAGING_SUFFIX, directory)
            _unlink_quietly(name + _PREVIOUS_SUFFIX, directory)
            metadata = _lstat_at(name, directory)
            if metadata is None:
                absent.append(str(root / name))
            else:
                if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and manysys.path_is_reparse(metadata):
                    manysys.unlink_directory_link(directory / name)
                else:
                    (directory / name).unlink()
                removed.append(str(root / name))
    except OSError as error:
        raise NativeCredentialError(
            ipc_errors.DAEMON_ERROR,
            f"cannot remove credentials under {root}: {error.strerror}",
        ) from error
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
    import_record: NativeImportRecord | None = None,
) -> dict[str, object]:
    allowed = _allowed(harness)
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
    expected = document_digest(documents) if import_record is not None else None
    directory = _open_private_chain(Path(agent_home), harness, create=False)
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
    old_manifest = observe(directory, allowed) if import_record is not None else None
    old_time = import_record.read(harness, old_manifest) if import_record is not None else None
    old_snapshot = import_record.snapshot(harness, old_manifest) if import_record is not None else None
    old_confirmation = import_record.confirmed_at(harness, old_manifest) if import_record is not None else None
    update_record = import_record is not None and (old_time is None or any(not row[3] for row in plan))
    if update_record:
        import_record.clear(harness)
    try:
        _write_plan(plan, directory, root)
    except NativeCredentialError:
        if update_record and old_time is not None and old_snapshot is not None and observe(directory, allowed) == old_manifest:
            import_record.restore(harness, old_snapshot, old_time, old_confirmation)
        raise
    try:
        if harness == "claude":
            _unlink_quietly(_CLAUDE_LAUNCH_PROVISIONED, directory)
            manysys.fsync_directory(directory)
        if update_record and expected is not None:
            if observe(directory, allowed) != expected:
                raise NativeCredentialError(ipc_errors.DAEMON_ERROR, "native files changed before recording their import")
            manysys.fsync_directory(directory)
            import_record.write(harness, expected)
    except OSError as error:
        detail = error.strerror or str(error)
        raise NativeCredentialError(
            ipc_errors.DAEMON_ERROR,
            f"finalizing credentials under {root} failed ({detail})",
        ) from error
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
    name: str, content: bytes, directory: Path | None, root: Path
) -> tuple[bool, bool]:
    if directory is None:
        return False, False
    metadata = _lstat_at(name, directory)
    if metadata is None:
        return False, False
    if _is_link_or_non_regular(metadata):
        raise NativeCredentialError(
            ipc_errors.PROVIDER_UNSAFE_PATH, f"{root / name} is not a regular file"
        )
    if metadata.st_size != len(content):
        return True, False
    return True, _read_at(name, directory) == content


def _write_plan(
    plan: list[tuple[str, bytes, bool, bool]], directory: Path, root: Path
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
        for name, _content, _existed, _unchanged in plan:
            _unlink_quietly(name + _STAGING_SUFFIX, directory)
            _unlink_quietly(name + _PREVIOUS_SUFFIX, directory)
        for name, content, _existed in pending:
            staged.append(name + _STAGING_SUFFIX)
            _create_private_at(name + _STAGING_SUFFIX, content, directory)
        for name, _content, existed in pending:
            if existed:
                os.replace(directory / name, directory / (name + _PREVIOUS_SUFFIX))
                aside.append(name)
        for name, _content, _existed in pending:
            os.replace(directory / (name + _STAGING_SUFFIX), directory / name)
            staged.remove(name + _STAGING_SUFFIX)
            installed.append(name)
    except OSError as error:
        kept, unremoved = _roll_back(
            directory, staged=staged, aside=aside, installed=installed
        )
        state = (
            "the previous files are back in place"
            if not kept
            else "could not restore "
            + ", ".join(kept)
            + f"; the previous copies are kept as <name>{_PREVIOUS_SUFFIX}"
        )
        run_state = (
            "nothing from this run was kept"
            if not unremoved
            else "rollback could not remove " + ", ".join(unremoved)
        )
        detail = error.strerror or str(error)
        raise NativeCredentialError(
            ipc_errors.DAEMON_ERROR,
            f"writing credentials under {root} failed ({detail}); "
            f"{run_state} and {state}",
        ) from error
    lingering: list[str] = []
    cleanup_error: OSError | None = None
    for name in aside:
        backup = name + _PREVIOUS_SUFFIX
        try:
            _unlink_quietly(backup, directory)
        except OSError as error:
            lingering.append(backup)
            if cleanup_error is None:
                cleanup_error = error
    for name, _content, _existed in pending:
        metadata = _lstat_at(name, directory)
        if metadata is None or _is_link_or_non_regular(metadata):
            raise NativeCredentialError(
                ipc_errors.PROVIDER_UNSAFE_PATH,
                f"{root / name} is not a regular file after writing",
            )
    if cleanup_error is not None:
        detail = cleanup_error.strerror or str(cleanup_error)
        raise NativeCredentialError(
            ipc_errors.DAEMON_ERROR,
            f"cleaning previous credentials under {root} failed ({detail}); "
            f"new files are in place and leftover backups remain: {', '.join(lingering)}",
        ) from cleanup_error


def _roll_back(
    directory: Path, *, staged: list[str], aside: list[str], installed: list[str]
) -> tuple[list[str], list[str]]:
    unremoved: list[str] = []
    for name in installed:
        try:
            _unlink_quietly(name, directory)
        except (OSError, NativeCredentialError):
            unremoved.append(name)
    for staging in staged:
        try:
            _unlink_quietly(staging, directory)
        except (OSError, NativeCredentialError):
            unremoved.append(staging)
    kept: list[str] = []
    for name in aside:
        try:
            os.replace(directory / (name + _PREVIOUS_SUFFIX), directory / name)
        except OSError:
            kept.append(name)
        else:
            if name in unremoved:
                unremoved.remove(name)
    return kept, unremoved


def _open_private_chain(agent_home: Path, harness: str, *, create: bool) -> Path | None:
    """Validate ``secrets/native/<harness>`` under the home without following links."""

    _require_real_directory(agent_home, is_home=True)
    shown = agent_home
    for part in ("secrets", "native", harness):
        child = _ensure_child_directory(shown / part, create=create)
        if child is None:
            return None
        shown = child
    return shown


def _ensure_child_directory(shown: Path, *, create: bool) -> Path | None:
    metadata = _lstat_entry(shown)
    if metadata is None:
        if not create:
            return None
        try:
            shown.mkdir(mode=0o700)
        except OSError as error:
            raise NativeCredentialError(
                ipc_errors.PROVIDER_UNSAFE_PATH,
                f"cannot create {shown}: {error.strerror}",
            ) from error
        metadata = _lstat_entry(shown)
    if metadata is None or stat.S_ISLNK(metadata.st_mode) or manysys.path_is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise NativeCredentialError(
            ipc_errors.PROVIDER_UNSAFE_PATH,
            f"{shown} is a symlink or not a directory; refusing to follow it",
        )
    return shown


def _require_real_directory(shown: Path, *, is_home: bool = False) -> None:
    try:
        metadata = shown.lstat()
    except OSError as error:
        if is_home:
            raise NativeCredentialError(
                ipc_errors.PROVIDER_UNSAFE_PATH,
                f"agent home {shown} is not a real directory ({error.strerror})",
            ) from error
        raise _unsafe(shown, error) from error
    if stat.S_ISLNK(metadata.st_mode) or manysys.path_is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise NativeCredentialError(
            ipc_errors.PROVIDER_UNSAFE_PATH,
            f"{shown} is a symlink or not a directory; refusing to follow it",
        )


def _is_link_or_non_regular(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISLNK(metadata.st_mode)
        or manysys.path_is_reparse(metadata)
        or not stat.S_ISREG(metadata.st_mode)
    )


def _unsafe(shown: Path, error: OSError) -> NativeCredentialError:
    if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
        return NativeCredentialError(
            ipc_errors.PROVIDER_UNSAFE_PATH,
            f"{shown} is a symlink or not a directory; refusing to follow it",
        )
    return NativeCredentialError(
        ipc_errors.PROVIDER_UNSAFE_PATH, f"cannot open {shown}: {error.strerror}"
    )


def _lstat_entry(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _unsafe(path, error) from error


def _lstat_at(name: str, directory: Path) -> os.stat_result | None:
    return _lstat_entry(directory / name)


def _read_at(name: str, directory: Path) -> bytes:
    target = directory / name
    metadata = _lstat_at(name, directory)
    if metadata is None or _is_link_or_non_regular(metadata):
        raise NativeCredentialError(
            ipc_errors.PROVIDER_UNSAFE_PATH, f"{name} is not a regular file"
        )
    with target.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise NativeCredentialError(
                ipc_errors.PROVIDER_UNSAFE_PATH, f"{name} is not a regular file"
            )
        return stream.read()


def _create_private_at(name: str, content: bytes, directory: Path) -> None:
    target = directory / name
    with open(target, "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        descriptor = stream.fileno()
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)


def _unlink_quietly(name: str, directory: Path) -> None:
    target = directory / name
    metadata = _lstat_at(name, directory)
    if metadata is None:
        return
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and manysys.path_is_reparse(metadata):
        manysys.unlink_directory_link(target)
        return
    try:
        target.unlink()
    except FileNotFoundError:
        pass
