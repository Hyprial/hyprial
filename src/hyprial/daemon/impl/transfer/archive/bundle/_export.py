"""AT06 bundle export half, split out of the original archive/bundle.py.

``BundleManifest``/``BundleEntry`` and validation live in ``._base``; this
module owns ``export_bundle`` and the source-unchanged witness.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from hyprial.kernel import harden_private_path

from ._base import (
    BUNDLE_PROTOCOL_VERSION,
    BundleDestinationNotEmpty,
    BundleDestinationOverlapsSource,
    BundleEntry,
    BundleError,
    BundleManifest,
    BundlePathError,
    BundleProtocolMismatch,
    BundleSourceChanged,
    BundleSourceLive,
    EXCLUDED_ROOT_FILE_NAMES,
    PAYLOAD_DIRNAME,
    SUPPORTED_PROTOCOL_VERSIONS,
    _epoch_digest,
    _is_platform_binary,
    _iter_payload_files,
    _normalise_dependencies,
    _resolve_declared_path,
    _safe_relative,
    _within,
    sha256_file,
    write_manifest,
)


def export_bundle(
    source_dir: os.PathLike[str] | str,
    bundle_dir: os.PathLike[str] | str,
    *,
    owner: str,
    actor: str,
    source_machine: str,
    created_at: str,
    identity: Mapping[str, object] | None = None,
    session_refs: Iterable[str] = (),
    grants: Iterable[str] = (),
    credential_envelope: str = "",
    exclude_paths: Iterable[str] = (),
    extra_files: Mapping[str, tuple[bytes, int]] | None = None,
    replace_files: Mapping[str, tuple[bytes, int]] | None = None,
    dependencies: Iterable[Mapping[str, object]] = (),
    source_epoch: str = "",
    protocol_version: int = BUNDLE_PROTOCOL_VERSION,
    allow_live: bool = False,
) -> BundleManifest:
    """Copy ``source_dir`` into a fresh bundle and write its manifest.

    Card 355 item 3 asks for a consistency payload formed *after* writes stop.
    Two guards implement the stop-write boundary: a live daemon socket under the
    source home refuses the export unless ``allow_live``, and the source tree is
    re-scanned after the copy so a tree that changed mid-export is refused
    instead of being reported as a consistent snapshot.

    ``exclude_paths`` names files the caller has already carried another way
    (the sealed credential envelope): they are neither copied nor registered,
    and the stop-write witness ignores them for the same reason -- they are not
    payload.  ``extra_files`` adds files the export itself produced, registered
    exactly like a copied entry, so a declaration can always name a manifest
    record instead of a payload file nobody hashed.  ``replace_files`` writes
    generated payload files that supersede any same-path file in ``source_dir``
    while keeping ``exclude_paths`` and ``extra_files`` strictly non-overlapping.
    """

    if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise BundleProtocolMismatch(
            f"cannot export protocol {protocol_version}; "
            f"supported: {list(SUPPORTED_PROTOCOL_VERSIONS)}"
        )

    source = Path(source_dir)
    if not source.is_dir():
        raise BundleError(f"source is not a directory: {source}")
    if source.is_symlink():
        raise BundlePathError("source directory must not be a symlink")
    if not allow_live:
        socket_path = live_daemon_socket(source)
        if socket_path is not None:
            raise BundleSourceLive(
                f"source home still has a live daemon ({socket_path}); stop the "
                "agent before packing, or pass allow_live for a best-effort copy"
            )

    dest = _canonical(bundle_dir)
    source_canonical = _canonical(source)
    host_home = _canonical(Path.home())
    if source_canonical == host_home or _is_ancestor(source_canonical, host_home):
        raise BundlePathError(
            f"refusing to export the host HOME ({host_home}); an agent home "
            "is the agent's, and the operator's Keychain and dotfiles must "
            "not ride a bundle to another machine"
        )
    if dest == source_canonical or _is_ancestor(
        dest, source_canonical
    ) or _is_ancestor(source_canonical, dest):
        raise BundleDestinationOverlapsSource(
            f"refusing to pack {source_canonical} into {dest}: the destination "
            "is the source itself, or one contains the other"
        )
    if dest.exists():
        if not dest.is_dir():
            raise BundleDestinationNotEmpty(
                f"destination exists and is not a directory: {dest}"
            )
        if not _is_empty_dir(dest):
            raise BundleDestinationNotEmpty(
                f"destination is not empty: {dest}; export never removes a "
                "destination, pass a fresh directory"
            )
    excluded = frozenset(_safe_relative(item) for item in exclude_paths)
    replaced_items: dict[str, tuple[bytes, int]] = {}
    for relative, item in sorted((replace_files or {}).items()):
        safe = _safe_relative(relative)
        if safe in excluded:
            raise BundlePathError(
                f"replacement file {safe!r} is also excluded from the payload"
            )
        replaced_items[safe] = item
    source_excluded = excluded | frozenset(replaced_items)

    dest.mkdir(parents=True, exist_ok=True)
    payload = dest / PAYLOAD_DIRNAME
    _mkdir_private(payload)

    planned: list[tuple[str, Path, int, int, str]] = []
    skipped_binaries: list[dict[str, object]] = []
    for relative, absolute in _iter_payload_files(
        source, exclude_root_names=EXCLUDED_ROOT_FILE_NAMES
    ):
        if relative in source_excluded:
            continue
        if absolute.is_symlink():
            raise BundlePathError(f"symlink in source is not allowed: {relative}")
        info = absolute.stat()
        if not stat.S_ISREG(info.st_mode):
            if stat.S_ISSOCK(info.st_mode) or stat.S_ISFIFO(info.st_mode):
                # A live home holds runtime sockets and pipes; they are not
                # payload and cannot be copied.  Record nothing, ship nothing.
                continue
            raise BundlePathError(f"not a regular file: {relative}")
        if _is_platform_binary(absolute):
            # AT06 item 1: platform binaries belong to the machine.  They are
            # not payload; the manifest declares them so the target knows it
            # must provide its own (``present=False``, kind=binary).
            skipped_binaries.append(
                {
                    "kind": "binary",
                    "present": False,
                    "path": relative,
                    "note": "platform binary: the target supplies its own; not carried",
                }
            )
            continue
        mode = stat.S_IMODE(info.st_mode)
        planned.append(
            (relative, absolute, info.st_size, mode, sha256_file(absolute))
        )

    entries: list[BundleEntry] = []
    for relative, absolute, size, mode, digest in planned:
        target = payload / relative
        _ensure_payload_parent(payload, PurePosixPath(relative).parts)
        shutil.copyfile(absolute, target)
        os.chmod(target, mode)
        landing = sha256_file(target)
        if landing != digest:
            shutil.rmtree(dest, ignore_errors=True)
            raise BundleSourceChanged(
                f"source changed while packing {relative}: expected {digest}, "
                f"copied {landing}"
            )
        entries.append(
            BundleEntry(path=relative, size=size, sha256=landing, mode=mode)
        )

    if not allow_live:
        _require_unchanged_source(source, planned, dest, excluded=source_excluded)

    try:
        entries.extend(
            _write_extra_payload_files(payload, entries, extra_files or {}, excluded)
        )
        if replaced_items:
            entries.extend(
                _write_extra_payload_files(payload, entries, replaced_items, excluded)
            )
    except BundlePathError:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    declared = _normalise_dependencies(list(dependencies) + skipped_binaries)
    envelope = _safe_relative(credential_envelope) if credential_envelope else ""
    registered_paths = {entry.path for entry in entries}
    if envelope:
        # The envelope must be inside the bundle we just wrote, and it must be
        # a file the manifest already hashes.  A declaration nobody can honour
        # refuses the export instead of shipping a bundle that cannot present
        # its credentials.
        _resolve_declared_path(
            dest, envelope, label="credential envelope", registered=registered_paths
        )
    for item in declared:
        if item.get("present"):
            _resolve_declared_path(
                dest,
                str(item["path"]),
                label=f"dependency {item['kind']!r}",
                registered=registered_paths,
            )

    entries.sort(key=lambda item: item.path)
    manifest = BundleManifest(
        owner=owner,
        actor=actor,
        source_machine=source_machine,
        created_at=created_at,
        protocol_version=protocol_version,
        entries=tuple(entries),
        identity=dict(identity or {}),
        session_refs=tuple(session_refs),
        grants=tuple(grants),
        credential_envelope=envelope,
        dependencies=declared,
        source_epoch=_epoch_digest(source_epoch) if source_epoch else "",
    )
    write_manifest(dest, manifest)
    return manifest


def _write_extra_payload_files(
    payload: Path,
    entries: list[BundleEntry],
    extra_files: Mapping[str, tuple[bytes, int]],
    excluded: frozenset[str],
) -> list[BundleEntry]:
    """Write and register files the export itself produced (the envelope).

    They are registered exactly like copied files -- the manifest stays the
    single source of truth -- and an extra which collides with a copied entry
    or names an excluded path is refused instead of ordered.
    """

    written = {entry.path for entry in entries}
    extras: list[BundleEntry] = []
    for relative, item in sorted(extra_files.items()):
        safe = _safe_relative(relative)
        if safe in excluded:
            raise BundlePathError(
                f"extra file {safe!r} is also excluded from the payload"
            )
        if safe in written:
            raise BundlePathError(
                f"extra file {safe!r} collides with a payload entry"
            )
        try:
            data, mode = item
        except (TypeError, ValueError) as error:
            raise BundlePathError(
                f"extra file {safe!r} must be a (bytes, mode) pair"
            ) from error
        if not isinstance(data, (bytes, bytearray)) or not isinstance(mode, int):
            raise BundlePathError(
                f"extra file {safe!r} must be a (bytes, mode) pair"
            )
        target = payload / Path(*PurePosixPath(safe).parts)
        _ensure_payload_parent(payload, PurePosixPath(safe).parts)
        target.write_bytes(bytes(data))
        os.chmod(target, mode)
        extras.append(
            BundleEntry(
                path=safe,
                size=len(data),
                sha256=hashlib.sha256(bytes(data)).hexdigest(),
                mode=mode,
            )
        )
        written.add(safe)
    return extras


def _mkdir_private(directory: Path) -> None:
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        if directory.is_symlink() or not directory.is_dir():
            raise BundlePathError(f"bundle path collides with a file: {directory}")
    else:
        harden_private_path(directory, mode=0o700)


def _ensure_payload_parent(payload: Path, relative_parts: tuple[str, ...]) -> None:
    current = payload
    for part in relative_parts[:-1]:
        current = current / part
        _mkdir_private(current)


def _canonical(target: os.PathLike[str] | str) -> Path:
    """Fully resolved form used by the destination overlap check.

    ``Path.absolute()`` only puts the working directory in front; it leaves
    ``..`` and symlinks in place, so ``.../agents/../agents/foo`` or a symlink
    pointing back at the source would slip past a lexical comparison.
    """

    return Path(os.path.realpath(os.path.expanduser(str(target))))


def _is_ancestor(candidate: Path, path: Path) -> bool:
    """True when ``candidate`` is a strict ancestor of ``path``."""

    return candidate != path and _within(path, candidate)


def _is_empty_dir(directory: Path) -> bool:
    return not any(directory.iterdir())


def live_daemon_socket(home: os.PathLike[str] | str) -> Path | None:
    """Return the daemon socket of a live source home, if one exists."""

    root = Path(home)
    for candidate in (root / "state" / "daemon.sock", root / "daemon.sock"):
        if candidate.is_socket():
            return candidate
    return None


def _require_unchanged_source(
    source: Path,
    planned: list[tuple[str, Path, int, int, str]],
    dest: Path,
    *,
    excluded: frozenset[str] = frozenset(),
) -> None:
    """Refuse the export when the source tree moved under us.

    The second scan is the stop-write witness: files added, removed or rewritten
    during the copy mean the bundle is not a point-in-time snapshot.

    ``excluded`` paths are ignored on both sides of the comparison: the caller
    deliberately did not carry them (their values rode the sealed envelope), so
    the witness has no claim about them either.
    """

    expected = {item[0]: (item[2], item[3], item[4]) for item in planned}
    actual: dict[str, tuple[int, int, str]] = {}
    for relative, absolute in _iter_payload_files(
        source, exclude_root_names=EXCLUDED_ROOT_FILE_NAMES
    ):
        if relative in excluded:
            continue
        if absolute.is_symlink():
            shutil.rmtree(dest, ignore_errors=True)
            raise BundleSourceChanged(
                f"a symlink appeared while packing: {relative}"
            )
        info = absolute.stat()
        if not stat.S_ISREG(info.st_mode):
            if stat.S_ISSOCK(info.st_mode) or stat.S_ISFIFO(info.st_mode):
                continue
            shutil.rmtree(dest, ignore_errors=True)
            raise BundleSourceChanged(
                f"a non-regular file appeared while packing: {relative}"
            )
        if _is_platform_binary(absolute):
            # Not payload: the first pass declared it instead of copying it,
            # so the stop-write witness has to ignore it the same way.
            continue
        actual[relative] = (
            info.st_size,
            stat.S_IMODE(info.st_mode),
            sha256_file(absolute),
        )
    if actual != expected:
        added = sorted(set(actual) - set(expected))
        removed = sorted(set(expected) - set(actual))
        rewritten = sorted(
            path
            for path in set(actual) & set(expected)
            if actual[path] != expected[path]
        )
        shutil.rmtree(dest, ignore_errors=True)
        raise BundleSourceChanged(
            "source changed while packing: "
            f"added={added[:5]} removed={removed[:5]} rewritten={rewritten[:5]}"
        )

