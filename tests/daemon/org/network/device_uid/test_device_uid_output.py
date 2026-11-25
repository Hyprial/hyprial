"""UUID addressing lane C: ``deviceUid`` on the device output faces.

The device's immutable uid is an additive output field.  The local device
record's persisted wire shape and every routing key stay as they are.  The
org network ``self`` block always names the local uid (null before P1
provisioning).  Directory rows read an optional ``deviceUid``; this device
publishes it only once id-form minting is on, so with the switch off the
published directory document is unchanged.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import hyprial.identity.impl.agents.addressing as addressing
from hyprial.daemon.impl.org.network.status.view import device_view
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from hyprial.identity.impl.org_directory.models import DirectoryDevice
from tests.daemon.org.network.test_org_network_service import (
    NODE_ID,
    OWNER,
    FakeFs,
    _service,
)
from tests.support.tailnet_membership import key

DEVICE_UID = uuid.UUID(int=0x1234, version=4).hex
ORG = "group-acme"


def _directory_device(**fields: object) -> DirectoryDevice:
    return DirectoryDevice(
        device_id="node-7f3a",
        owner="allenwoods@example.org",
        server_public="nodekey:AAAAabcdefghijklmnopqrstuvwxyz123456789",
        client_public="nodekey:BBBBabcdefghijklmnopqrstuvwxyz123456789",
        address="10.0.0.1:41271#psk-secret",
        key_generation=1,
        updated_at="2026-10-03T09:00:00Z",
        **fields,
    )


def _home(tmp_path: Path, *, provisioned: bool) -> Path:
    write_device_record(
        tmp_path,
        DeviceRecord(
            device_id=NODE_ID,
            owner=OWNER,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
            device_uid=DEVICE_UID if provisioned else None,
            name="laptop" if provisioned else None,
        ),
    )
    return tmp_path


def _own_documents(fs: FakeFs) -> list[dict[str, object]]:
    rows = []
    for text in fs.docs.values():
        try:
            document = json.loads(text)
        except ValueError:
            continue
        if isinstance(document, dict) and document.get("deviceId") == NODE_ID:
            rows.append(document)
    return rows


def test_a_directory_row_without_a_uid_keeps_its_exact_shape() -> None:
    device = _directory_device()
    assert device.device_uid is None
    assert "deviceUid" not in device.as_record()
    assert DirectoryDevice.from_record(device.as_record()) == device


def test_a_directory_row_reads_and_writes_an_optional_uid() -> None:
    device = _directory_device(device_uid=DEVICE_UID)
    record = device.as_record()
    assert record["deviceUid"] == DEVICE_UID
    assert DirectoryDevice.from_record(record) == device
    assert device_view(device)["deviceUid"] == DEVICE_UID


@pytest.mark.parametrize("bad", ["", "XYZ", "0" * 32, 7])
def test_a_directory_row_rejects_a_malformed_uid(bad: object) -> None:
    record = _directory_device().as_record() | {"deviceUid": bad}
    with pytest.raises(ValueError):
        DirectoryDevice.from_record(record)


@pytest.mark.parametrize("provisioned", [True, False])
def test_network_self_block_names_the_local_device_uid(tmp_path, provisioned) -> None:
    home = _home(tmp_path, provisioned=provisioned)
    view = _service(FakeFs(), home).network()
    assert view["self"]["deviceId"] == NODE_ID
    assert view["self"]["deviceUid"] == (DEVICE_UID if provisioned else None)


def test_switch_off_publishes_the_unchanged_directory_document(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    fs = FakeFs()
    service = _service(fs, _home(tmp_path, provisioned=True))
    service.create(ORG)
    documents = _own_documents(fs)
    assert documents and all("deviceUid" not in row for row in documents)
    rows = service.network()["directory"][ORG]
    own = [row for row in rows if row["deviceId"] == NODE_ID]
    assert own and "deviceUid" not in own[0]


def test_switch_on_publishes_and_shows_the_device_uid(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    fs = FakeFs()
    service = _service(fs, _home(tmp_path, provisioned=True))
    service.create(ORG)
    documents = _own_documents(fs)
    assert documents and all(row["deviceUid"] == DEVICE_UID for row in documents)
    rows = service.network()["directory"][ORG]
    own = [row for row in rows if row["deviceId"] == NODE_ID]
    assert own and own[0]["deviceUid"] == DEVICE_UID


def test_an_explicit_null_uid_is_rejected_so_the_row_round_trips() -> None:
    record = _directory_device().as_record() | {"deviceUid": None}
    with pytest.raises(ValueError):
        DirectoryDevice.from_record(record)


def test_flipping_the_switch_on_republishes_an_existing_row_with_the_uid(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    fs = FakeFs()
    service = _service(fs, _home(tmp_path, provisioned=True))
    service.create(ORG)
    assert all("deviceUid" not in row for row in _own_documents(fs))
    assert service.publish_self()["published"] == []
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    assert service.publish_self()["published"] == [ORG]
    documents = _own_documents(fs)
    assert documents and all(row["deviceUid"] == DEVICE_UID for row in documents)
    assert service.publish_self()["published"] == []


def test_flipping_the_switch_back_off_republishes_the_row_without_the_uid(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", True)
    fs = FakeFs()
    service = _service(fs, _home(tmp_path, provisioned=True))
    service.create(ORG)
    assert all(row["deviceUid"] == DEVICE_UID for row in _own_documents(fs))
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", False)
    assert service.publish_self()["published"] == [ORG]
    documents = _own_documents(fs)
    assert documents and all("deviceUid" not in row for row in documents)
