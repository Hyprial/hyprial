"""DeviceRecord round trips and strict bad-data rejection (§4.1)."""

from __future__ import annotations

import pytest

from hyprial.identity.impl.device.models import DeviceRecord


def _record() -> DeviceRecord:
    return DeviceRecord(
        device_id="node-7f3a",
        device_uid="7da7712684f3441aaac4e2f4048dba01",
        name="mac-studio",
        owner="alicesmith@example.org",
        server_public="nodekey:AAAAabcdefghijklmnopqrstuvwxyz123456789",
        client_public="nodekey:BBBBabcdefghijklmnopqrstuvwxyz123456789",
    )


def test_public_record_round_trip_preserves_legacy_fields() -> None:
    record = _record()
    restored = DeviceRecord.from_record(record.as_record())
    assert restored == DeviceRecord(
        device_id=record.device_id,
        owner=record.owner,
        server_public=record.server_public,
        client_public=record.client_public,
        key_generation=record.key_generation,
    )


def test_record_is_camel_case_with_exactly_the_frozen_keys() -> None:
    assert _record().as_record() == {
        "deviceId": "node-7f3a",
        "owner": "alicesmith@example.org",
        "serverPublic": "nodekey:AAAAabcdefghijklmnopqrstuvwxyz123456789",
        "clientPublic": "nodekey:BBBBabcdefghijklmnopqrstuvwxyz123456789",
        "keyGeneration": 1,
    }


def test_key_generation_defaults_to_one_and_round_trips() -> None:
    assert _record().key_generation == 1
    rotated = DeviceRecord(
        device_id="node-7f3a",
        device_uid="7da7712684f3441aaac4e2f4048dba01",
        name="mac-studio",
        owner="alicesmith@example.org",
        server_public="nodekey:AAAAabcdefghijklmnopqrstuvwxyz123456789",
        client_public="nodekey:BBBBabcdefghijklmnopqrstuvwxyz123456789",
        key_generation=4,
    )
    assert DeviceRecord.from_record(rotated.as_record()).key_generation == 4


def test_identity_record_is_separate_and_strict() -> None:
    record = _record()
    assert record.as_identity_record() == {
        "deviceUid": "7da7712684f3441aaac4e2f4048dba01",
        "name": "mac-studio",
    }
    legacy = DeviceRecord.from_record(record.as_record())
    assert legacy.with_identity_record(record.as_identity_record()) == record


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda r: r.pop("deviceId"), id="missing-deviceId"),
        pytest.param(lambda r: r.pop("keyGeneration"), id="missing-keyGeneration"),
        pytest.param(lambda r: r.update({"extra": 1}), id="unknown-key"),
        pytest.param(lambda r: r.update({"deviceId": ""}), id="empty-deviceId"),
        pytest.param(lambda r: r.update({"owner": 7}), id="owner-not-a-string"),
        pytest.param(lambda r: r.update({"owner": None}), id="owner-none"),
        pytest.param(lambda r: r.update({"serverPublic": "privkey:nope"}), id="not-a-nodekey"),
        pytest.param(lambda r: r.update({"clientPublic": "nodekey:"}), id="bare-prefix"),
        pytest.param(lambda r: r.update({"keyGeneration": 0}), id="generation-zero"),
        pytest.param(lambda r: r.update({"keyGeneration": -1}), id="generation-negative"),
        pytest.param(lambda r: r.update({"keyGeneration": True}), id="generation-bool"),
        pytest.param(lambda r: r.update({"keyGeneration": "1"}), id="generation-string"),
    ],
)
def test_bad_data_is_rejected(mutation) -> None:
    payload = _record().as_record()
    mutation(payload)
    with pytest.raises(ValueError):
        DeviceRecord.from_record(payload)


def test_non_object_payload_is_rejected() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        DeviceRecord.from_record([_record().as_record()])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param({}, id="missing-both"),
        pytest.param({"deviceUid": "7da7712684f3441aaac4e2f4048dba01"}, id="missing-name"),
        pytest.param({"name": "mac-studio"}, id="missing-deviceUid"),
        pytest.param(
            {"deviceUid": "", "name": "mac-studio"}, id="empty-deviceUid"
        ),
        pytest.param(
            {"deviceUid": "not-a-uuid", "name": "mac-studio"},
            id="bad-deviceUid",
        ),
        pytest.param(
            {"deviceUid": "7da7712684f3441aaac4e2f4048dba01", "name": ""},
            id="empty-name",
        ),
        pytest.param(
            {
                "deviceUid": "7da7712684f3441aaac4e2f4048dba01",
                "name": "mac-studio",
                "extra": True,
            },
            id="unknown-key",
        ),
    ],
)
def test_bad_identity_data_is_rejected(identity: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        DeviceRecord.from_record(_record().as_record()).with_identity_record(identity)


@pytest.mark.parametrize(
    "reserved_id",
    [
        "u.ou_" + "b" * 32,
        "u.6f8b1d21-33a8-4688-a32e-b1b23e87d5d9",
        "d." + "b" * 32,
        "a." + "a" * 26,
        # Case-insensitive reservation (APFS): uppercase variants are refused too.
        "U.OU_" + "B" * 32,
        "D." + "B" * 32,
        "A." + "A" * 26,
    ],
)
def test_device_display_name_rejects_exact_identity_id_shapes(reserved_id: str) -> None:
    with pytest.raises(ValueError, match="identity id shape"):
        DeviceRecord(
            device_id="node-7f3a",
            device_uid="7da7712684f3441aaac4e2f4048dba01",
            name=reserved_id,
            owner="alicesmith@example.org",
            server_public="nodekey:server",
            client_public="nodekey:client",
        )


@pytest.mark.parametrize("name", ["d.lane@example.com", "a.smith@x", "u.zhang"])
def test_device_display_name_accepts_id_prefix_lookalikes(name: str) -> None:
    record = _record()
    values = record.as_identity_record() | {"name": name}
    assert DeviceRecord.from_record(record.as_record()).with_identity_record(values).name == name
