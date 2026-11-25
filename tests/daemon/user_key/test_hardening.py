"""Adversarial local trust, concurrency and audit checks."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.daemon.user_key.test_signer import setup


@pytest.mark.parametrize("expiry,seq", [(float("nan"), 1), (float("inf"), 1), (2000, -1), (2000, True)])
def test_invalid_trust_metadata_cannot_sign(tmp_path, expiry, seq):
    core, _, store, signer, caller, payload, _, _ = setup(tmp_path)
    signer.trust = lambda _: core.Trust(store.status()["publicKey"], expiry, seq)
    with pytest.raises(core.Error) as caught:
        signer.sign("test.fixture.v1", payload, caller)
    assert caught.value.code == "USER_KEY_UNBOUND"


def test_disk_bound_bit_is_not_trust(tmp_path):
    core, service, store, _, caller, payload, _, _ = setup(tmp_path)
    signer = service.Signer(store, enabled=True)
    with pytest.raises(core.Error):
        signer.sign("org.join", payload, caller)
    assert service.OwnerKeyPort().lookup("alice@test.invalid", store.status()["keyId"]).state == "unavailable"


def test_parallel_login_has_one_key(tmp_path):
    core, _, _, _, _, _, _, _ = setup(tmp_path / "fixture")
    def ensure(_):
        return core.Store(state_dir=tmp_path / "fresh", owner="parallel@test.invalid",
                          casdoor_sub="test-sub", device_uid="device", fingerprint="machine").ensure()["keyId"]
    with ThreadPoolExecutor(max_workers=16) as pool:
        keys = list(pool.map(ensure, range(64)))
    assert len(set(keys)) == 1


def test_audit_rotation_private_and_bounded(tmp_path, monkeypatch):
    _, _, store, signer, caller, payload, _, _ = setup(tmp_path)
    from hyprial.daemon.impl.user_key import audit
    monkeypatch.setattr(audit, "MAX_BYTES", 1600)
    for _ in range(20):
        signer.sign("test.fixture.v1", payload, caller)
    paths = list(store.root.glob("audit.jsonl*"))
    assert len(paths) == 5
    for path in paths:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_size <= 1600
        for line in path.read_text().splitlines():
            assert json.loads(line)["decision"] == "signed"


def test_valid_payload_tampering_and_other_purpose_fail_verification(tmp_path):
    core, _, _, signer, caller, payload, status, _ = setup(tmp_path)
    envelope = signer.sign("test.fixture.v1", payload, caller)
    purpose = signer.purposes["test.fixture.v1"]
    envelope["payload"]["target"] = "another-target"
    assert not core.verify(envelope, status["publicKey"], purpose=purpose)
    envelope = signer.sign("test.fixture.v1", payload, caller)
    other = core.Purpose("test.other", purpose.domain, "other-kind", purpose.fields)
    assert not core.verify(envelope, status["publicKey"], purpose=other)
