"""AT07 first slice: land a validated bundle onto a target agent home.

The receive side is deliberately conservative: it validates the bundle first
(AT06 gate), refuses to touch a non-empty destination, and undoes everything
it created when any step fails.  It does not rotate entity tokens, re-grant
capabilities or rewrite the registry yet — those need the target daemon and
are the next slice.

Read binding: the manifest is read once (``load_sealed_manifest``) and that
one reading is handed to every gate -- payload validation, the signature gate
and the copy loop -- so a writer cannot swap the file between gates and have
"validated", "signed" and "landed" describe three different manifests.  Every
copied file is also re-hashed against its manifest entry while it is staged,
and only a verified file is moved to its final name; a bundle rewritten
*while* it is being received is refused with ``BundleTamperError`` and the
whole attempt is undone.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.transfer.archive.bundle import (
    BundleError,
    BundleManifest,
    BundleTamperError,
    sha256_file,
    validate_bundle)
from hyprial.daemon.impl.transfer.landing.signature_gate import (
    SignatureAttestation,
    SignaturePolicy,
    require_trusted_signature)
from hyprial.daemon.impl.transfer.manifest_reading import (
    SealedManifest,
    load_sealed_manifest)

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
    signature: SignatureAttestation | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "owner": self.owner,
            "source_machine": self.source_machine,
            "destination": str(self.destination),
            "files": self.files,
            "bytes": self.bytes,
            "dry_run": self.dry_run,
            # AT06 slice 2b: who signed this bundle, as this target recorded
            # it, or ``null`` on a rehearsal path that lands unsigned bundles.
            "signature": None if self.signature is None else self.signature.as_dict(),
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
    signature: SignaturePolicy,
    sealed: SealedManifest | None = None,
) -> ReceiveResult:
    """Validate ``bundle_dir`` and materialise it at ``destination``.

    Raises ``BundleError`` when the bundle itself is bad, the ``SignatureError``
    family when it cannot be attributed to a trusted source key, and the
    ``ReceiveError`` family for refusals and failed landings.

    ``sealed`` is the bundle's single manifest reading when the caller already
    took one; everything below then binds to it instead of re-opening the
    file.
    """

    reading = sealed if sealed is not None else load_sealed_manifest(bundle_dir)
    manifest = validate_bundle(bundle_dir, manifest=reading.manifest)
    attestation = require_trusted_signature(
        bundle_dir,
        policy=signature,
        credential_envelope=manifest.credential_envelope or None,
        sealed=reading,
    )

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
            signature=attestation,
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
            # Stage beside the target: the copy is verified against the
            # manifest entry *before* it is moved to its final name, so a
            # payload rewritten between validation and this copy never lands
            # (and a partial copy is never visible under the final name).
            handle, staging_name = tempfile.mkstemp(
                prefix=".hyprial-receive-", suffix=".partial",
                dir=str(target.parent),
            )
            os.close(handle)
            staging = Path(staging_name)
            created.append(staging)
            shutil.copyfile(source_file, staging)
            actual_size = staging.stat().st_size
            if actual_size != entry.size:
                raise BundleTamperError(
                    f"payload changed while receiving: size mismatch for "
                    f"{entry.path!r}: manifest={entry.size} actual={actual_size}"
                )
            actual_digest = sha256_file(staging)
            if actual_digest != entry.sha256:
                raise BundleTamperError(
                    f"payload changed while receiving: sha256 mismatch for "
                    f"{entry.path!r}: manifest={entry.sha256} "
                    f"actual={actual_digest}"
                )
            os.chmod(staging, stat.S_IMODE(entry.mode))
            os.replace(staging, target)
            created[-1] = target
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
        signature=attestation,
    )
