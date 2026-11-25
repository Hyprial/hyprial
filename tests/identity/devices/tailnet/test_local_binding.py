from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from hyprial.identity import (
    TransportCredentialError,
    commit_transport_credential,
    read_device_record,
    read_transport_credential,
    stage_transport_credential,
    validate_enrollment_response,
    write_device_record,
)
from tests.identity.devices.test_transport_credential import OWNER, _device_record
from tests.identity.devices.transport_safety.inventory import inventory
from tests.identity.devices.transport_safety.test_commit_preflight import _pending_renewal

OTHER_OWNER = "bob@example.test"


def _assert_owner_rejection(error: TransportCredentialError, device_uid: str) -> None:
    assert error.code == "TRANSPORT_OWNER_MISMATCH"
    diagnostic = f"{error!s} {error!r}"
    for value in (OWNER, OTHER_OWNER, device_uid, "-----BEGIN", "secrets/transport"):
        assert value not in diagnostic


def _change_local_owner(home: Path) -> None:
    record = read_device_record(home)
    assert record is not None
    write_device_record(home, replace(record, owner=OTHER_OWNER))


def test_stage_rejects_wrong_local_owner_before_creating_transport_files(tmp_path: Path) -> None:
    uid = uuid4().hex
    write_device_record(tmp_path, _device_record(uid))
    before = inventory(tmp_path)

    with pytest.raises(TransportCredentialError) as caught:
        stage_transport_credential(
            tmp_path,
            operation_id=str(uuid4()),
            owner=OTHER_OWNER,
            device_uid=uid,
            expected_credential_generation=0,
        )

    _assert_owner_rejection(caught.value, uid)
    assert inventory(tmp_path) == before
    assert not (tmp_path / "secrets/transport").exists()
    assert not (tmp_path / "state/transport").exists()


def test_validate_rejects_changed_local_owner_and_preserves_ready_and_staged(tmp_path: Path) -> None:
    staged, credential, bundle, previous = _pending_renewal(tmp_path)
    # The same response has already passed real certificate validation in the fixture.
    _change_local_owner(tmp_path)
    before = inventory(tmp_path)

    with pytest.raises(TransportCredentialError) as caught:
        validate_enrollment_response(
            credential.as_record(),
            staged=staged,
            pinned_trust_bundle_pem=bundle,
            effective_owner=staged.owner,
        )

    _assert_owner_rejection(caught.value, staged.device_uid)
    assert inventory(tmp_path) == before
    assert read_transport_credential(tmp_path) == previous


def test_commit_rejects_changed_local_owner_and_preserves_ready_and_staged(tmp_path: Path) -> None:
    staged, credential, bundle, previous = _pending_renewal(tmp_path)
    _change_local_owner(tmp_path)
    before = inventory(tmp_path)

    with pytest.raises(TransportCredentialError) as caught:
        commit_transport_credential(
            tmp_path,
            staged=staged,
            credential=credential,
            pinned_trust_bundle_pem=bundle,
        )

    _assert_owner_rejection(caught.value, staged.device_uid)
    assert inventory(tmp_path) == before
    assert read_transport_credential(tmp_path) == previous
