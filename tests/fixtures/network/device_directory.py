"""The device-directory fixture and the frozen device-row line shape.

The N-A2 device row, as the consumption side reads it, is one JSON object per
line with exactly these fields (the frozen line shape):

    {deviceId, device_uid, name, owner, serverPublic, clientPublic, keyGeneration}

The envelope gate consumes only ``owner`` (the identity authority a present
``author`` must match) and ``clientPublic`` (the peer ``derive(role="server",
peer=...)`` takes); the other fields are carried for the acceptance batch's
out-of-process read face and are validated here so a malformed row fails at
the seam, not somewhere inside a request.  Production sourcing of this
directory (which supported daemon boundary serves it) is on the external
prerequisite list -- this module is the fixture and the shape it pins.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The frozen field set of one device row line.
DEVICE_ROW_FIELDS = frozenset(
    {
        "deviceId",
        "device_uid",
        "name",
        "owner",
        "serverPublic",
        "clientPublic",
        "keyGeneration",
    }
)

#: The directory fixture next to this module.
DEVICE_DIRECTORY_FIXTURE = Path(__file__).resolve().parent / "device_directory.json"


def load_device_directory(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load and validate a device directory; ``ValueError`` on any bad row.

    The returned mapping is keyed by ``deviceId``; callers adapt each row to
    the gate's ``DeviceRow(owner=row["owner"], client_public=row["clientPublic"])``.
    """

    source = Path(path) if path is not None else DEVICE_DIRECTORY_FIXTURE
    devices: dict[str, dict[str, Any]] = {}
    for lineno, line in enumerate(
        source.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"device directory line {lineno}: not JSON") from error
        if not isinstance(row, dict) or set(row) != DEVICE_ROW_FIELDS:
            raise ValueError(
                f"device directory line {lineno}: fields must be exactly "
                f"{sorted(DEVICE_ROW_FIELDS)}"
            )
        for field in DEVICE_ROW_FIELDS - {"keyGeneration"}:
            if not isinstance(row[field], str) or not row[field]:
                raise ValueError(
                    f"device directory line {lineno}: {field} must be a non-empty string"
                )
        generation = row["keyGeneration"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError(
                f"device directory line {lineno}: keyGeneration must be a positive integer"
            )
        if row["deviceId"] in devices:
            raise ValueError(
                f"device directory line {lineno}: duplicate deviceId {row['deviceId']!r}"
            )
        devices[row["deviceId"]] = row
    return devices
