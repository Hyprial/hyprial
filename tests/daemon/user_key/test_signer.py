"""Positive signing uses an injected TEST-ONLY purpose and trust port."""
from __future__ import annotations

import base64
import json

import pytest

import hyprial.daemon as daemon
import hyprial.identity as identity


def surfaces():
    core = getattr(identity, "UserKey", None)
    service = getattr(daemon, "UserKey", None)
    assert core is not None and service is not None, "U1 public namespaces required"
    return core, service


def setup(tmp_path, *, bound=True, enabled=True):
    core, service = surfaces()
    store = core.Store(state_dir=tmp_path / "state", owner="alice@test.invalid",
                       casdoor_sub="test-sub", device_uid="test-device",
                       fingerprint="test-machine")
    status = store.ensure()
    if bound:
        # Test-only issuer stand-in. The file's state alone still cannot sign:
        # the independently injected verifier must return matching fresh trust.
        path = store.root / "key.json"
        record = json.loads(path.read_text())
        record["state"] = "bound"
        path.write_text(json.dumps(record))
    purpose = core.Purpose("test.fixture.v1", "hyprial-user-op-v1", "test-fixture",
                           frozenset({"owner", "org", "target", "issuedAtMs"}))
    clock = [1000.0]

    def trust(record):
        if not bound:
            raise core.Error("USER_KEY_UNBOUND")
        return core.Trust(record.public_key, 2000, 1)

    signer = service.Signer(store, trust=trust, enabled=enabled,
                            purposes={purpose.name: purpose}, clock=lambda: clock[0])
    caller = service.Caller("operator", "user:alice@test.invalid", frozenset({"org-test"}), pid=123)
    payload = {"target": "synthetic-target", "org": "org-test", "owner": "alice@test.invalid"}
    return core, service, store, signer, caller, payload, status, clock


def test_unbound_and_default_off_never_sign(tmp_path):
    for index, flags in enumerate([{"bound": False}, {"enabled": False}]):
        core, _, _, signer, caller, payload, _, _ = setup(tmp_path / str(index), **flags)
        with pytest.raises(core.Error) as caught:
            signer.sign("test.fixture.v1", payload, caller)
        assert caught.value.code == "USER_KEY_UNBOUND"


def test_canonical_sign_verify_and_no_key_bytes(tmp_path):
    core, _, store, signer, caller, payload, status, _ = setup(tmp_path)
    first = signer.sign("test.fixture.v1", payload, caller)
    second = signer.sign("test.fixture.v1", dict(reversed(list(payload.items()))), caller)
    assert first == second
    assert first["payload"]["issuedAtMs"] == 1000000
    assert first["signer"] == {"keyKind": "owner", "owner": "alice@test.invalid", "keyId": status["keyId"]}
    assert core.verify(first, status["publicKey"], purpose=signer.purposes["test.fixture.v1"])
    private = json.loads((store.root / "key.json").read_text())["privateKey"]
    public_output = json.dumps([first, signer.status()]) + (store.root / "audit.jsonl").read_text()
    assert private not in public_output
    assert base64.b64decode(private).hex() not in public_output
    assert "test.fixture.v1" not in core.PURPOSES
    first["signer"]["keyKind"] = "device"
    assert not core.verify(first, status["publicKey"], purpose=signer.purposes["test.fixture.v1"])


@pytest.mark.parametrize("mutation", [
    {"owner": "mallory"}, {"extra": "synthetic-secret"}, {"org": "unauthorized"},
    {"issuedAtMs": 1}, {"target": []}, {"target": float("nan")},
])
def test_bad_payload_refused_without_echo(tmp_path, mutation):
    core, _, _, signer, caller, payload, _, _ = setup(tmp_path)
    payload.update(mutation)
    with pytest.raises(core.Error) as caught:
        signer.sign("test.fixture.v1", payload, caller)
    assert caught.value.code in {"PAYLOAD_INVALID", "CALLER_NOT_AUTHORIZED"}
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("classification", ["session", "squire", "adapter", "channel", "worker"])
def test_non_operator_refused(tmp_path, classification):
    core, service, _, signer, _, payload, _, _ = setup(tmp_path)
    caller = service.Caller(classification, "synthetic-agent", frozenset({"org-test"}))
    with pytest.raises(core.Error) as caught:
        signer.sign("test.fixture.v1", payload, caller)
    assert caught.value.code == "CALLER_NOT_AUTHORIZED"


def test_rate_limit_and_replenish(tmp_path):
    core, _, _, signer, caller, payload, _, clock = setup(tmp_path)
    for _ in range(30):
        signer.sign("test.fixture.v1", payload, caller)
    with pytest.raises(core.Error) as caught:
        signer.sign("test.fixture.v1", payload, caller)
    assert caught.value.code == "RATE_LIMITED"
    clock[0] += 2
    assert signer.sign("test.fixture.v1", payload, caller)


def test_audit_signed_and_refused_no_payload(tmp_path):
    core, _, store, signer, caller, payload, _, _ = setup(tmp_path)
    signer.sign("test.fixture.v1", payload, caller)
    with pytest.raises(core.Error):
        signer.sign("transfer.bundle", payload, caller)
    rows = [json.loads(line) for line in (store.root / "audit.jsonl").read_text().splitlines()]
    assert [row["decision"] for row in rows] == ["signed", "refused"]
    assert rows[1]["code"] == "PURPOSE_UNKNOWN"
    assert set(rows[0]) == {"ts", "requestId", "owner", "keyId", "purpose", "kind", "domain",
                            "payloadSha256", "org", "target", "caller", "decision", "code"}
    assert "payload" not in rows[0]
    assert set(rows[0]["caller"]) == {"class", "actor", "sessionRefSha256", "pid"}
    assert rows[0]["payloadSha256"] == core.digest({
        **payload, "issuedAtMs": 1000000})


def test_audit_failure_cannot_release_signature(tmp_path):
    core, _, store, signer, caller, payload, _, _ = setup(tmp_path)
    (store.root / "audit.jsonl").symlink_to(tmp_path / "outside")
    with pytest.raises(core.Error):
        signer.sign("test.fixture.v1", payload, caller)
    assert not (tmp_path / "outside").exists()


def test_trust_mismatch_and_expiry_fail_closed(tmp_path):
    core, _, store, signer, caller, payload, _, _ = setup(tmp_path)
    for trust in [core.Trust("A" * 44, 2000, 1),
                  core.Trust(store.status()["publicKey"], 1000, 1)]:
        signer.trust = lambda record: trust
        with pytest.raises(core.Error) as caught:
            signer.sign("test.fixture.v1", payload, caller)
        assert caught.value.code in {"USER_KEY_UNBOUND", "USER_KEY_BINDING_EXPIRED"}
