"""Narrow offline discard of an owned, conclusively abandonable staging slot."""

from dataclasses import dataclass
from hashlib import sha256

from ..transport_credential import (
    TRANSPORT_ENROLLMENT_RELPATH,
    TRANSPORT_KEY_RELPATH,
    TRANSPORT_SECRETS_RELPATH,
    TRANSPORT_STAGED_RELPATH,
    TRANSPORT_STATE_RELPATH,
    _load_private_key,
    _parse_enrollment,
    _public_spki_sha256,
)
from ..transport_storage import (
    TransportCredentialError,
    _fsync_directory,
    _preflight_directories,
    _preflight_file,
    _read_json_if_present,
    _read_safe_file,
)
from .guards import fail


@dataclass
class _Discard:
    key_marker: tuple
    enrollment_marker: tuple
    key_attempted: bool = False
    enrollment_attempted: bool = False


def _marker(path):
    metadata = path.lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        sha256(_read_safe_file(path, 0o600)).hexdigest(),
    )


def _preflight(home, op):
    staged = op.staged
    key = home / TRANSPORT_STAGED_RELPATH / op.operation_id / "key.pem"
    enrollment = home / TRANSPORT_ENROLLMENT_RELPATH
    if (
        staged._home != home
        or staged._key_path != key
        or staged.operation_id != op.operation_id
        or staged.owner != op.record.owner
        or staged.device_uid != op.record.device_uid
        or staged.expected_credential_generation
        != (op.target.credential_generation if op.target else 0)
    ):
        fail("TAILNET_DISCARD_MISMATCH", "discard", op.outcome)
    _preflight_directories(
        (
            home / TRANSPORT_SECRETS_RELPATH,
            home / TRANSPORT_STAGED_RELPATH,
            key.parent,
            home / TRANSPORT_STATE_RELPATH,
        ),
        root=home,
        final_mode=0o700,
        require_owner=True,
        require_present=True,
    )
    # Inspect every target, including optional installed key, before deleting any.
    key_present = _preflight_file(key, 0o600)
    enrollment_present = _preflight_file(enrollment, 0o600)
    committed = home / TRANSPORT_KEY_RELPATH
    committed_present = _preflight_file(committed, 0o600)
    progress = op.discard
    if (not key_present and not (progress and progress.key_attempted)) or (
        not enrollment_present and not (progress and progress.enrollment_attempted)
    ):
        fail("TAILNET_DISCARD_MISMATCH", "discard", op.outcome)
    if enrollment_present:
        parsed = _parse_enrollment(_read_json_if_present(enrollment))
        if parsed != {
            "operationId": op.operation_id,
            "owner": staged.owner,
            "deviceUid": staged.device_uid,
            "expectedCredentialGeneration": staged.expected_credential_generation,
            "spkiSha256": staged.spki_sha256,
        }:
            fail("TAILNET_DISCARD_MISMATCH", "discard", op.outcome)
    if key_present and (
        _public_spki_sha256(_load_private_key(key).public_key()) != staged.spki_sha256
    ):
        fail("TAILNET_DISCARD_MISMATCH", "discard", op.outcome)
    if committed_present and (
        _public_spki_sha256(_load_private_key(committed).public_key())
        == staged.spki_sha256
    ):
        fail("TAILNET_DISCARD_MISMATCH", "discard", op.outcome)
    key_marker = _marker(key) if key_present else None
    enrollment_marker = _marker(enrollment) if enrollment_present else None
    if progress and (
        (key_present and key_marker != progress.key_marker)
        or (enrollment_present and enrollment_marker != progress.enrollment_marker)
    ):
        fail("TAILNET_DISCARD_MISMATCH", "discard", op.outcome)
    return key, enrollment, key_marker, enrollment_marker


def discard(home, op, *, confirmed):
    # Terminal replay must precede confirmation, preflight and every file access.
    if op.discard_receipt is not None:
        return dict(op.discard_receipt)
    if (
        confirmed is not True
        or op.kind not in ("enroll", "renew")
        or not op.origin_proven
        or op.phase == "COMMITTED"
        or not (
            op.outcome == "NOT_SENT"
            or (
                op.outcome == "REJECTED"
                and op.rejection is not None
                and op.rejection.outcome == "REJECTED"
                and op.rejection.retryable is False
                and op.rejection.operation_id == op.operation_id
            )
        )
    ):
        fail("TAILNET_DISCARD_INELIGIBLE", "discard", op.outcome)
    try:
        key, enrollment, key_marker, enrollment_marker = _preflight(home, op)
        if op.discard is None:
            op.discard = _Discard(key_marker, enrollment_marker)
            op.phase = "DISCARD_PENDING"
        # Attempts are latched first: an unlink can succeed before reporting IO
        # failure. Retry still validates any surviving inode and redoes fsync.
        op.discard.key_attempted = True
        if key_marker is not None:
            key.unlink()
        _fsync_directory(key.parent)
        op.discard.enrollment_attempted = True
        if enrollment_marker is not None:
            enrollment.unlink()
        _fsync_directory(enrollment.parent)
    except OSError:
        raise TransportCredentialError(
            "TRANSPORT_DISCARD_IO", "transport staging discard is incomplete"
        ) from None
    op.discard_receipt = {"operationId": op.operation_id, "phase": "DISCARDED"}
    op.phase = "DISCARDED"
    return dict(op.discard_receipt)
