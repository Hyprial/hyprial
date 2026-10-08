"""AT07 first slice: land a validated bundle onto a target agent home.

The receive side is deliberately conservative: it validates the bundle first
(AT06 gate), refuses to touch a non-empty destination, and undoes everything
it created when any step fails.  It does not rotate entity tokens, re-grant
capabilities or rewrite the registry yet — those need the target daemon and
are the next slice.

Known limit: the payload is validated before the copy (AT06 gate) and the
copied file is written whole, but the digest is not re-checked afterwards, so a
bundle that is rewritten *while* it is being received is not detected.  The
transport is expected to hand over a quiescent bundle (the exporter refuses a
live source for the same reason).
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.transfer.archive.bundle import (
    BundleError,
    BundleManifest,
    validate_bundle)

__all__ = [
    "ReceiveError",
    "ReceiveDestinationNotEmpty",
    "ReceiveActorMismatch",
    "ReceiveFailed",
    "ReceiveResult",
    "receive_bundle",
]


class ReceiveError(Exception):
    """Base class for receive-side failures."""

    code = "receive_error"


class ReceiveDestinationNotEmpty(ReceiveError):
    """The target home already has content; receiving would merge or clobber."""

    code = "receive_destination_not_empty"


class ReceiveActorMismatch(ReceiveError):
    """The bundle carries a different actor than the caller expects."""

    code = "receive_actor_mismatch"


class ReceiveFailed(ReceiveError):
    """The bundle did not land; everything created by this attempt was undone."""

    code = "receive_failed"


@dataclass(frozen=True)
class ReceiveResult:
    """What a successful receive did (or would do, with ``dry_run``)."""

    actor: str
    owner: str
    source_machine: str
    destination: Path
    files: int
    bytes: int
    dry_run: bool
    manifest: BundleManifest

    def as_dict(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "owner": self.owner,
            "source_machine": self.source_machine,
            "destination": str(self.destination),
            "files": self.files,
            "bytes": self.bytes,
            "dry_run": self.dry_run,
            # Card 355 item 1: a declaration with ``present=False`` is not a
            # failure, it is a job for the target side.  The receipt repeats
            # it so nothing is silently assumed to have come across.
            "credential_envelope": self.manifest.credential_envelope or None,
            "target_must_provide": [
                dict(item) for item in self.manifest.target_must_provide()
            ],
        }


def _is_empty(directory: Path) -> bool:
    try:
        next(os.scandir(directory))
    except StopIteration:
        return True
    return False


def receive_bundle(
    bundle_dir: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
    *,
    expect_actor: str | None = None,
    dry_run: bool = False,
) -> ReceiveResult:
    """Validate ``bundle_dir`` and materialise it at ``destination``.

    Raises ``BundleError`` when the bundle itself is bad, and the
    ``ReceiveError`` family for refusals and failed landings.
    """

    manifest = validate_bundle(bundle_dir)

    if expect_actor is not None and manifest.actor != expect_actor:
        raise ReceiveActorMismatch(
            f"bundle carries actor {manifest.actor!r}, expected {expect_actor!r}"
        )

    dest = Path(destination)
    if dest.exists():
        if not dest.is_dir():
            raise ReceiveDestinationNotEmpty(
                f"destination exists and is not a directory: {dest}"
            )
        if not _is_empty(dest):
            raise ReceiveDestinationNotEmpty(
                f"destination is not empty: {dest}"
            )

    total_bytes = sum(entry.size for entry in manifest.entries)
    if dry_run:
        return ReceiveResult(
            actor=manifest.actor,
            owner=manifest.owner,
            source_machine=manifest.source_machine,
            destination=dest,
            files=len(manifest.entries),
            bytes=total_bytes,
            dry_run=True,
            manifest=manifest,
        )

    payload_root = Path(bundle_dir) / "payload"
    created_root = not dest.exists()
    created: list[Path] = []

    def _undo() -> None:
        for path in reversed(created):
            try:
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                else:
                    path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue
        if created_root:
            try:
                dest.rmdir()
            except OSError:
                pass

    def _ensure_parents(leaf: Path) -> None:
        missing: list[Path] = []
        current = leaf
        while current != dest and not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)

    try:
        dest.mkdir(parents=True, exist_ok=True)
        for entry in manifest.entries:
            source_file = payload_root / entry.path
            target = dest / entry.path
            _ensure_parents(target.parent)
            shutil.copyfile(source_file, target)
            os.chmod(target, stat.S_IMODE(entry.mode))
            created.append(target)
    except Exception as error:  # noqa: BLE001 - every failure undoes the attempt
        _undo()
        if isinstance(error, BundleError):
            raise
        raise ReceiveFailed(f"receiving bundle failed: {error}") from error

    return ReceiveResult(
        actor=manifest.actor,
        owner=manifest.owner,
        source_machine=manifest.source_machine,
        destination=dest,
        files=len(manifest.entries),
        bytes=total_bytes,
        dry_run=False,
        manifest=manifest,
    )
