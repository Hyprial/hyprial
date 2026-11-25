"""U1-R2: repair local keys on serving relogin recovery without re-provisioning."""

from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.application.onboarding.lifecycle.recovery import ServingRecovery
from hyprial.daemon.impl.onboarding.operations.service import OperationService
from hyprial.daemon.impl.user_key.login_key import ensure_login_user_key
from hyprial.identity import (
    DeviceRecord,
    IdentityTransactionBusy,
    IdentityTransactionLock,
    MachineBinding,
    UserKey,
    device_key_path,
    machine_fingerprint,
    read_device_record,
    read_identity_snapshot,
    write_device_record,
    write_machine_binding,
)
from hyprial.kernel import resolve_node_id
from tests.shell.login.test_login_session_cli import _seed_login
from tests.support.runtime.environment import home_with_node_id


@pytest.fixture
def existing(tmp_path, monkeypatch):
    home = home_with_node_id(tmp_path / "home")
    (home / "state").mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home / "state"))
    monkeypatch.setenv("HYPRIAL_OWNER", "")
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    for name in (
        "HARNESS_SOCKET_PATH",
        "HARNESS_CONFIG",
        "HYPRIAL_WORKER_ACTOR",
        "HYPRIAL_WORKER_SESSION_REF",
        "HYPRIAL_MANAGED_WORKER",
    ):
        monkeypatch.delenv(name, raising=False)
    _seed_login(home)
    private = device_key_path(home)
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(b"SYNTHETIC-NOT-A-DEVICE-KEY")
    private.chmod(0o600)
    uid = "12345678123442348234123456789abc"
    write_device_record(
        home,
        DeviceRecord(
            resolve_node_id(),
            "alice",
            "nodekey:server",
            "nodekey:client",
            device_uid=uid,
            name="test-node",
        ),
    )
    write_machine_binding(home, MachineBinding(machine_fingerprint()))
    operations = OperationService.open(home / "state" / "operations")
    subject = json.loads((home / "settings.json").read_text())["userId"]
    store = UserKey.Store(
        state_dir=home / "state",
        owner="alice",
        casdoor_sub=subject,
        device_uid=uid,
        fingerprint=machine_fingerprint(),
    )
    return home, store, operations


class _ForbidProvisioner:
    def ensure(self, *, home, owner):
        del home, owner
        pytest.fail("existing valid device must not be re-provisioned")


def _recover(home, operations, *, user_key=ensure_login_user_key):
    recovery = ServingRecovery(
        operations=operations,
        home=home,
        identity_reader=read_identity_snapshot,
        device_reader=read_device_record,
        provisioner=_ForbidProvisioner(),
        user_key=user_key,
    )
    return recovery.recover_relogin("alice")


def test_plain_existing_login_initializes_then_reuses_key(existing):
    home, store, operations = existing
    first = _recover(home, operations)
    assert first["ready"] is True
    assert first["userKey"]["state"] == "unbound"
    record = json.loads((store.root / "key.json").read_text())
    assert record["deviceUid"] == store.device_uid
    second = _recover(home, operations)
    assert second["ready"] is True
    assert len(list(home.rglob("user-keys/*/key.json"))) == 1
    assert record["privateKey"] not in json.dumps(first) + json.dumps(second)


def test_existing_login_storage_failure_does_not_fail_session(existing):
    home, store, operations = existing
    original = store.ensure()
    (store.root / "key.json").chmod(0o644)
    blocked = _recover(home, operations)
    assert blocked["ready"] is True
    assert blocked["userKey"]["state"] == "unavailable"
    assert blocked["userKey"]["code"] == "USER_KEY_QUARANTINED"
    (store.root / "key.json").chmod(0o600)
    recovered = _recover(home, operations)
    assert recovered["ready"] is True
    assert recovered["userKey"]["state"] == "unbound"
    assert json.loads((store.root / "key.json").read_text())["keyId"] == original["keyId"]


def test_existing_login_quarantines_copied_key(existing):
    home, store, operations = existing
    store.fingerprint = "synthetic-other-machine"
    old = store.ensure()
    private = json.loads((store.root / "key.json").read_text())["privateKey"]
    recovered = _recover(home, operations)
    assert recovered["ready"] is True
    assert recovered["userKey"]["state"] == "unbound"
    current = json.loads((store.root / "key.json").read_text())
    assert current["keyId"] != old["keyId"]
    archive = store.root / "quarantine" / (old["keyId"] + ".json")
    assert "privateKey" not in json.loads(archive.read_text())
    assert private not in json.dumps(recovered) + archive.read_text()


def test_relogin_recovery_initializes_user_key_under_identity_fence(existing):
    home, store, operations = existing
    seen: list[str] = []

    def fenced(target_home, state_dir, owner, device):
        with pytest.raises(IdentityTransactionBusy):
            IdentityTransactionLock.acquire(target_home, timeout=0)
        result = ensure_login_user_key(target_home, state_dir, owner, device)
        seen.append(result["state"])
        return result

    block = _recover(home, operations, user_key=fenced)
    assert seen == ["unbound"]
    assert block["userKey"]["state"] == "unbound"
    assert (store.root / "key.json").exists()
