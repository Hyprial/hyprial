from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from hyprial.identity.interface import (
    DeviceRecord,
    TransportCredentialError,
    retire_transport_credential,
    stage_transport_credential,
    write_device_record,
)

OWNER = "alice@example.test"


def _stage(home: Path):
    device_uid = uuid4().hex
    write_device_record(
        home,
        DeviceRecord(
            device_id="node-test",
            owner=OWNER,
            server_public="nodekey:server",
            client_public="nodekey:client",
            device_uid=device_uid,
            name="test-device",
        ),
    )
    return stage_transport_credential(
        home,
        operation_id=str(uuid4()),
        owner=OWNER,
        device_uid=device_uid,
        expected_credential_generation=0,
    )


@pytest.mark.parametrize(
    "relative",
    ["state/tailcat/device.json", "state/tailcat/device-identity.json"],
    ids=["device-record", "device-identity"],
)
def test_stage_rejects_hardlinked_device_files_without_creating_material(
    tmp_path: Path, relative: str
) -> None:
    home = tmp_path / "home"
    staged = _stage(home)
    retire_transport_credential(home)
    (home / "state/transport").rmdir()
    path = home / relative
    external = tmp_path / f"external-{path.name}"
    external.write_bytes(path.read_bytes())
    os.chmod(external, 0o600)
    path.unlink()
    os.link(external, path)

    with pytest.raises(TransportCredentialError) as caught:
        stage_transport_credential(
            home,
            operation_id=str(uuid4()),
            owner=OWNER,
            device_uid=staged.device_uid,
            expected_credential_generation=0,
        )

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert not (home / "secrets/transport").exists()
    assert not (home / "state/transport").exists()
    assert path.stat().st_nlink == external.stat().st_nlink == 2


@pytest.mark.parametrize(
    ("name", "mode"),
    [("credential.json", 0o600), ("enrollment.json", 0o600), ("trust-bundle.pem", 0o644)],
)
def test_retire_rejects_hardlinked_state_file_before_any_deletion(
    tmp_path: Path, name: str, mode: int
) -> None:
    staged = _stage(tmp_path)
    state_root = tmp_path / "state/transport"
    files = {
        "credential.json": state_root / "credential.json",
        "enrollment.json": state_root / "enrollment.json",
        "trust-bundle.pem": state_root / "trust-bundle.pem",
    }
    for item_name, path in files.items():
        if not path.exists():
            path.write_bytes(f"{item_name}-sentinel".encode())
        os.chmod(path, 0o644 if item_name == "trust-bundle.pem" else 0o600)
    target = files[name]
    external = tmp_path / f"external-{name}"
    external.write_bytes(target.read_bytes())
    os.chmod(external, mode)
    target.unlink()
    os.link(external, target)
    before = {item_name: path.read_bytes() for item_name, path in files.items()}
    key_bytes = staged._key_path.read_bytes()

    with pytest.raises(TransportCredentialError) as caught:
        retire_transport_credential(tmp_path)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert staged._key_path.read_bytes() == key_bytes
    assert {item_name: path.read_bytes() for item_name, path in files.items()} == before
    assert target.stat().st_nlink == external.stat().st_nlink == 2


def test_stage_rejects_symlinked_tailcat_parent_without_creating_material(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    staged = _stage(home)
    retire_transport_credential(home)
    (home / "state/transport").rmdir()
    tailcat = home / "state/tailcat"
    external = tmp_path / "external-tailcat"
    tailcat.rename(external)
    tailcat.symlink_to(external, target_is_directory=True)
    before = {path.name: path.read_bytes() for path in external.iterdir()}

    with pytest.raises(TransportCredentialError) as caught:
        stage_transport_credential(
            home,
            operation_id=str(uuid4()),
            owner=OWNER,
            device_uid=staged.device_uid,
            expected_credential_generation=0,
        )

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert not (home / "secrets/transport").exists()
    assert not (home / "state/transport").exists()
    assert {path.name: path.read_bytes() for path in external.iterdir()} == before
