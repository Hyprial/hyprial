"""Device paths and the record's atomic read/write (§4.1)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from hyprial.identity.impl.device import (
    address_path,
    device_key_path,
    device_record_path,
    read_device_record,
    write_device_record,
)
from hyprial.identity.impl.device.models import DeviceRecord


def _record() -> DeviceRecord:
    return DeviceRecord(
        device_id="node-7f3a",
        device_uid="7da7712684f3441aaac4e2f4048dba01",
        name="mac-studio",
        owner="alicesmith@example.org",
        server_public="nodekey:AAAAabcdefghijklmnopqrstuvwxyz123456789",
        client_public="nodekey:BBBBabcdefghijklmnopqrstuvwxyz123456789",
        key_generation=2,
    )


def test_paths_sit_under_the_given_home(tmp_path: Path) -> None:
    assert device_key_path(tmp_path) == tmp_path / "secrets/tailcat/device-key.json"
    assert device_record_path(tmp_path) == tmp_path / "state/tailcat/device.json"
    assert address_path(tmp_path) == tmp_path / "state/tailcat/address"


def test_read_returns_none_when_no_record_exists(tmp_path: Path) -> None:
    assert read_device_record(tmp_path) is None


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    write_device_record(tmp_path, _record())
    assert read_device_record(tmp_path) == _record()


def test_write_creates_parents_writes_0600_and_leaves_no_temporaries(
    tmp_path: Path,
) -> None:
    write_device_record(tmp_path, _record())
    path = device_record_path(tmp_path)
    identity_path = tmp_path / "state/tailcat/device-identity.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(identity_path.stat().st_mode) == 0o600
    # The kernel's atomic write must not leave its temp file behind.
    assert sorted(p.name for p in path.parent.iterdir()) == [
        "device-identity.json",
        "device.json",
    ]
    # The public on-disk shape stays exactly backward readable.
    assert json.loads(path.read_text("utf-8")) == _record().as_record()
    assert json.loads(identity_path.read_text("utf-8")) == _record().as_identity_record()


def test_overwrite_replaces_the_record_atomically(tmp_path: Path) -> None:
    write_device_record(tmp_path, _record())
    rotated = DeviceRecord(
        device_id="node-7f3a",
        device_uid=_record().device_uid,
        name=_record().name,
        owner="new-owner@example.org",
        server_public="nodekey:CCCCabcdefghijklmnopqrstuvwxyz123456789",
        client_public="nodekey:DDDDabcdefghijklmnopqrstuvwxyz123456789",
        key_generation=3,
    )
    write_device_record(tmp_path, rotated)
    assert read_device_record(tmp_path) == rotated
    assert sorted(p.name for p in device_record_path(tmp_path).parent.iterdir()) == [
        "device-identity.json",
        "device.json",
    ]


def test_new_record_is_accepted_by_origin_dev_strict_reader(tmp_path: Path) -> None:
    """Replicate origin/dev's exact strict key set for rollback compatibility."""

    write_device_record(tmp_path, _record())
    public = json.loads(device_record_path(tmp_path).read_text("utf-8"))
    dev_keys = {
        "deviceId",
        "owner",
        "serverPublic",
        "clientPublic",
        "keyGeneration",
    }
    assert set(public) == dev_keys
    assert DeviceRecord.from_record(public).device_id == _record().device_id


def test_corrupt_identity_record_fails_loudly(tmp_path: Path) -> None:
    write_device_record(tmp_path, _record())
    identity_path = tmp_path / "state/tailcat/device-identity.json"
    identity_path.write_text('{"deviceUid":"bad","name":"mac"}', encoding="utf-8")
    with pytest.raises(ValueError, match="deviceUid"):
        read_device_record(tmp_path)


def test_corrupt_record_fails_loudly(tmp_path: Path) -> None:
    write_device_record(tmp_path, _record())
    device_record_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        read_device_record(tmp_path)


def test_hand_edited_record_fails_validation(tmp_path: Path) -> None:
    write_device_record(tmp_path, _record())
    payload = json.loads(device_record_path(tmp_path).read_text("utf-8"))
    payload["deviceId"] = ""
    device_record_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="deviceId"):
        read_device_record(tmp_path)
