"""X1 local-only checks. No production directory, signer or provider use."""
from __future__ import annotations

import base64
import json
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

import hyprial.identity as identity
from hyprial.daemon import export_bundle


def api():
    surface = getattr(identity, "EncryptionKey", None)
    assert surface is not None, "independent local X25519 core must exist"
    return surface


def store(tmp_path, *, kind="user", **overrides):
    args = dict(state_dir=tmp_path / "state", casdoor_sub="synthetic-sub" if kind == "user" else None,
                device_uid="device-a", fingerprint="machine-a", kind=kind)
    args.update(overrides)
    return api().Store(**args)


def test_independent_x25519_generation_and_idempotency(tmp_path):
    keys = store(tmp_path)
    first = keys.ensure()
    assert first == keys.ensure()
    assert first["algorithm"] == "X25519"
    assert first["keyId"].startswith("x25519:")
    assert first["state"] == "local"
    second = store(tmp_path, device_uid="device-b", fingerprint="machine-b")
    assert second.ensure()["keyId"] != first["keyId"]
    other_user = store(tmp_path, casdoor_sub="other-sub")
    assert other_user.ensure()["keyId"] != first["keyId"]
    assert len({keys.root, second.root, other_user.root}) == 3
    signing = identity.UserKey.Store(state_dir=tmp_path / "state", owner="alice@test.invalid",
                                    casdoor_sub="synthetic-sub", device_uid="device-a",
                                    fingerprint="machine-a")
    signing.ensure()
    signed = json.loads((signing.root / "key.json").read_text())
    encrypted = json.loads((keys.root / "key.json").read_text())
    assert signed["privateKey"] != encrypted["privateKey"]
    assert signing.root != keys.root


def test_exchange_is_x25519_and_rejects_wrong_key_id(tmp_path):
    a, b = store(tmp_path), store(tmp_path, device_uid="device-b")
    ap, bp = a.ensure(), b.ensure()
    assert a.exchange(bp["publicKey"], key_id=ap["keyId"]) == b.exchange(
        ap["publicKey"], key_id=bp["keyId"])
    with pytest.raises(identity.UserKey.Error):
        a.exchange(bp["publicKey"], key_id=bp["keyId"])
    for bad in (base64.b64encode(bytes(32)).decode(), "not-a-key", ap["publicKey"] + "="):
        with pytest.raises(identity.UserKey.Error):
            a.exchange(bad, key_id=ap["keyId"])


def test_host_key_has_no_login_owner_and_never_reuses_user_key(tmp_path):
    host = store(tmp_path, kind="host")
    before = host.ensure()
    store(tmp_path, casdoor_sub="alice-sub").ensure()
    store(tmp_path, casdoor_sub="bob-sub").ensure()
    assert store(tmp_path, kind="host").ensure() == before
    assert before["casdoorSub"] is None
    assert before["keyId"] != store(tmp_path).ensure()["keyId"]
    with pytest.raises(identity.UserKey.Error):
        store(tmp_path, kind="host", casdoor_sub="spoofed-login-owner").ensure()


@pytest.mark.parametrize("kind,signer,accepted", [
    ("user", "owner", True), ("host", "device", True),
    ("host", "owner", False), ("user", "device", False),
    ("host", "host", False), ("user", "unknown", False),
])
def test_signer_kind_policy_is_not_signature_verification(kind, signer, accepted):
    # This policy prerequisite does NOT claim a trusted declaration was verified.
    assert api().signer_kind_allowed(kind, signer) is accepted


def test_secret_excluded_from_public_views_and_transfer(tmp_path, caplog):
    keys = store(tmp_path)
    status = keys.ensure()
    path = keys.root / "key.json"
    record = json.loads(path.read_text())
    private = record["privateKey"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(keys.root.stat().st_mode) == 0o700
    assert "privateKey" not in status
    assert private not in json.dumps(keys.status())
    agent = tmp_path / "agents" / "test-agent"
    agent.mkdir(parents=True)
    (agent / "config.json").write_text("{}")
    bundle = tmp_path / "bundle"
    export_bundle(agent, bundle, owner="alice@test.invalid", actor="test-agent",
                  source_machine="test-node", created_at="2026-10-31T00:00:00Z")
    for item in bundle.rglob("*"):
        if item.is_file():
            assert private.encode() not in item.read_bytes()
    with pytest.raises(identity.UserKey.Error) as caught:
        keys.exchange(private + "bad", key_id=status["keyId"])
    assert private not in str(caught.value)
    assert private not in caplog.text
    assert not list(agent.rglob("key.json"))


@pytest.mark.parametrize("kind", ["user", "host"])
def test_copied_fingerprint_quarantines_without_destructive_rekey(tmp_path, kind):
    keys = store(tmp_path, kind=kind)
    first = keys.ensure()
    copied = store(tmp_path, kind=kind, fingerprint="other-machine")
    assert copied.status()["state"] == "quarantined"
    before = (keys.root / "key.json").read_bytes()
    for action in (copied.ensure, lambda: copied.exchange(first["publicKey"], key_id=first["keyId"])):
        with pytest.raises(identity.UserKey.Error) as caught:
            action()
        assert caught.value.code == "USER_KEY_QUARANTINED"
    assert (keys.root / "key.json").read_bytes() == before
    assert keys.status()["state"] == "quarantined"


@pytest.mark.parametrize("field,value", [("deviceUid", "forged-device"),
    ("casdoorSub", "forged-sub"), ("keyKind", "host"), ("algorithm", "Ed25519"),
    ("keyId", "x25519:wrong"), ("extra", "unknown")])
def test_tampered_disk_identity_is_not_adopted(tmp_path, field, value):
    keys = store(tmp_path)
    keys.ensure()
    path = keys.root / "key.json"
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(identity.UserKey.Error) as caught:
        keys.ensure()
    assert caught.value.code == "USER_KEY_QUARANTINED"


def test_revoked_key_does_not_exchange_or_silently_regenerate(tmp_path):
    keys = store(tmp_path)
    first = keys.ensure()
    keys.revoke()
    assert keys.status()["state"] == "revoked"
    assert "privateKey" not in json.loads((keys.root / "key.json").read_text())
    for action in (keys.ensure, lambda: keys.exchange(first["publicKey"], key_id=first["keyId"])):
        with pytest.raises(identity.UserKey.Error):
            action()
    assert keys.status()["keyId"] == first["keyId"]


@pytest.mark.parametrize("level", ["identity", "encryption-keys", "key-root", "key.json", ".lock"])
def test_symlinks_refused(tmp_path, level):
    keys = store(tmp_path)
    keys.ensure()
    external = tmp_path / "outside"
    external.mkdir()
    sentinel = external / "key.json"
    sentinel.write_text("SENTINEL")
    target = {"identity": keys.root.parent.parent, "encryption-keys": keys.root.parent,
              "key-root": keys.root, "key.json": keys.root / "key.json",
              ".lock": keys.root / ".lock"}[level]
    if target.is_dir():
        shutil.rmtree(target)
        target.symlink_to(external, target_is_directory=True)
    else:
        target.unlink()
        target.symlink_to(sentinel)
    with pytest.raises(identity.UserKey.Error):
        keys.ensure()
    assert sentinel.read_text() == "SENTINEL"


@pytest.mark.parametrize("level,mode", [("root", 0o755), ("key.json", 0o644), (".lock", 0o644)])
def test_unsafe_permissions_refused(tmp_path, level, mode):
    keys = store(tmp_path)
    keys.ensure()
    (keys.root if level == "root" else keys.root / level).chmod(mode)
    with pytest.raises(identity.UserKey.Error):
        keys.status()


def test_concurrent_generation_is_single_key(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store(tmp_path).ensure()["keyId"], range(16)))
    assert len(set(results)) == 1


@pytest.mark.parametrize("field", ["privateKey", "deviceUid", "state"])
def test_duplicate_json_fields_are_not_silently_resolved(tmp_path, field):
    keys = store(tmp_path)
    keys.ensure()
    path = keys.root / "key.json"
    original = path.read_text()
    record = json.loads(original)
    # Both values are individually valid: reject ambiguity, not just bad bytes.
    path.write_text(original.rstrip()[:-1] + "," + json.dumps(field) + ":"
                    + json.dumps(record[field]) + "}")
    with pytest.raises(identity.UserKey.Error) as caught:
        keys.ensure()
    assert caught.value.code == "USER_KEY_QUARANTINED"
    assert record["privateKey"] not in str(caught.value)


def test_hardlinks_and_mismatched_private_key_refused(tmp_path):
    keys, other = store(tmp_path), store(tmp_path, casdoor_sub="other")
    keys.ensure()
    other.ensure()
    path = keys.root / "key.json"
    alias = tmp_path / "alias"
    alias.hardlink_to(path)
    with pytest.raises(identity.UserKey.Error):
        keys.status()
    alias.unlink()
    record = json.loads(path.read_text())
    record["privateKey"] = json.loads((other.root / "key.json").read_text())["privateKey"]
    path.write_text(json.dumps(record))
    with pytest.raises(identity.UserKey.Error):
        keys.ensure()


def test_unsupported_local_store_fails_closed_without_creating_files(tmp_path, monkeypatch):
    keys = store(tmp_path)
    monkeypatch.setattr(identity.UserKey.Store, "supported", staticmethod(lambda: False))
    for action in (keys.ensure, keys.status, keys.revoke):
        with pytest.raises(identity.UserKey.Error) as caught:
            action()
        assert caught.value.code == "USER_KEY_UNBOUND"
    assert not (tmp_path / "state").exists()


def test_fixture_is_local_only_not_frozen_protocol():
    from pathlib import Path
    fixture = json.loads(Path("contract/fixtures/provider-keys/encryption-keys.json").read_text())
    assert fixture["status"] == "draft-local-only"
    assert fixture["productionPublication"] is False
    assert fixture["declarationEncoding"] is None
    assert fixture["requiredSignerKind"] == {"user": "owner", "host": "device"}
