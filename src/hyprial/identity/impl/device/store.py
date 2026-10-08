"""Where the device's tailcat files live, and the record's read/write.

Four relpaths, one home: the sidecar writes the key file (``genkey``) and
the address file (``forward``); the daemon writes the rollback-safe public
record and a P1 identity sibling.  All four
live under the hyprial home so a wiped home is a wiped device identity —
the same locality rule the rest of the home follows.

The write itself reuses the kernel's ``atomic_json_write`` (0600, temp file
plus ``os.replace``) so a crash mid-write can never leave a half record
that a later ``read_device_record`` would have to guess about.
"""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.identity.impl.device.models import DeviceRecord
from hyprial.kernel import atomic_json_write

__all__ = [
    "ADDRESS_RELPATH",
    "DEVICE_KEY_RELPATH",
    "DEVICE_RECORD_RELPATH",
    "address_path",
    "device_key_path",
    "device_record_path",
    "read_device_record",
    "write_device_record",
]

#: 0600, produced by the sidecar's ``genkey``; Python never writes this.
DEVICE_KEY_RELPATH = "secrets/tailcat/device-key.json"
#: The record's public half; may be published into an org directory.
DEVICE_RECORD_RELPATH = "state/tailcat/device.json"
#: P1 immutable id and display name. Kept separate so older strict readers
#: continue to accept ``device.json`` during rollback.
DEVICE_IDENTITY_RELPATH = "state/tailcat/device-identity.json"
#: The sidecar-written local Tailcat address (secret, 0600).
ADDRESS_RELPATH = "state/tailcat/address"


def device_key_path(home: Path) -> Path:
    """The device key file under ``home`` (sidecar input)."""
    return Path(home) / DEVICE_KEY_RELPATH


def device_record_path(home: Path) -> Path:
    """The device record under ``home`` (public, publishable)."""
    return Path(home) / DEVICE_RECORD_RELPATH


def _device_identity_path(home: Path) -> Path:
    return Path(home) / DEVICE_IDENTITY_RELPATH


def address_path(home: Path) -> Path:
    """The sidecar-written Tailcat address under ``home`` (secret)."""
    return Path(home) / ADDRESS_RELPATH


def read_device_record(home: Path) -> DeviceRecord | None:
    """Read the record; ``None`` when absent, ``ValueError`` when corrupt.

    A missing record is a normal state (before first login); a present but
    unparseable one is an operator situation worth naming, not papering
    over with a reset.
    """
    path = device_record_path(home)
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"device record at {path} is not valid JSON: {error}") from error
    record = DeviceRecord.from_record(parsed)
    identity_path = _device_identity_path(home)
    try:
        identity_raw = identity_path.read_text("utf-8")
    except FileNotFoundError:
        return record
    try:
        identity = json.loads(identity_raw)
    except ValueError as error:
        raise ValueError(
            f"device identity at {identity_path} is not valid JSON: {error}"
        ) from error
    return record.with_identity_record(identity)


def write_device_record(home: Path, record: DeviceRecord) -> None:
    """Atomically write the rollback-safe record and P1 identity sibling."""

    atomic_json_write(device_record_path(home), record.as_record())
    identity_path = _device_identity_path(home)
    if record.device_uid is None:
        identity_path.unlink(missing_ok=True)
    else:
        atomic_json_write(identity_path, record.as_identity_record())
