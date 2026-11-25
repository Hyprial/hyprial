"""U1 security assertions use disposable state and synthetic identities only."""
from __future__ import annotations

import json
import shutil
import stat

import pytest

import hyprial.identity as identity
from hyprial.daemon import DaemonApplication, export_bundle
from hyprial.kernel import DaemonRequestError


def api():
    surface = getattr(identity, "UserKey", None)
    assert surface is not None, "daemon-owned user signing-key core must exist"
    return surface


def store(tmp_path, **overrides):
    args = dict(state_dir=tmp_path / "state", owner="alice@test.invalid",
                casdoor_sub="synthetic-sub", device_uid="test-device",
                fingerprint="synthetic-machine")
    args.update(overrides)
    return api().Store(**args)


def test_private_material_is_local_and_status_is_public(tmp_path):
    keys = store(tmp_path)
    first = keys.ensure()
    assert first == keys.ensure()
    assert first["state"] == "unbound"
    path = keys.root / "key.json"
    record = json.loads(path.read_text())
    assert record["keyId"] == first["keyId"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(keys.root.stat().st_mode) == 0o700
    assert "privateKey" not in first
    assert record["privateKey"] not in json.dumps(keys.status())
    agent = tmp_path / "agents" / "synthetic-agent"
    agent.mkdir(parents=True)
    (agent / "config.json").write_text("{}")
    bundle = tmp_path / "bundle"
    manifest = export_bundle(agent, bundle, owner=keys.owner, actor="synthetic-agent",
                             source_machine="synthetic-node", created_at="2026-10-31T00:00:00Z")
    assert [entry.path for entry in manifest.entries] == ["config.json"]
    for exported in bundle.rglob("*"):
        if exported.is_file():
            assert record["privateKey"].encode() not in exported.read_bytes()
    before = path.read_bytes()
    shutil.move(str(agent), str(tmp_path / "moved-agent"))
    shutil.rmtree(tmp_path / "moved-agent")
    assert keys.ensure()["keyId"] == first["keyId"]
    assert path.read_bytes() == before
    assert not list((tmp_path / "agents").rglob("key.json"))


@pytest.mark.parametrize("level", ["identity", "user-keys", "owner", "key.json"])
def test_symlink_never_followed(tmp_path, level):
    keys = store(tmp_path)
    keys.ensure()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "key.json"
    sentinel.write_text("DO-NOT-READ-OR-WRITE")
    targets = {"identity": tmp_path / "state" / "identity",
               "user-keys": keys.root.parent, "owner": keys.root,
               "key.json": keys.root / "key.json"}
    target = targets[level]
    if target.is_dir():
        shutil.rmtree(target)
        target.symlink_to(external, target_is_directory=True)
    else:
        target.unlink()
        target.symlink_to(sentinel)
    with pytest.raises(api().Error) as caught:
        keys.ensure()
    assert caught.value.code == "USER_KEY_QUARANTINED"
    assert sentinel.read_text() == "DO-NOT-READ-OR-WRITE"
    assert "DO-NOT-READ" not in str(caught.value)


@pytest.mark.parametrize("level,mode", [("owner", 0o755), ("key.json", 0o644)])
def test_loose_modes_refused(tmp_path, level, mode):
    keys = store(tmp_path)
    keys.ensure()
    (keys.root if level == "owner" else keys.root / level).chmod(mode)
    with pytest.raises(api().Error) as caught:
        keys.status()
    assert caught.value.code == "USER_KEY_QUARANTINED"


def test_copied_home_quarantines_then_login_rekeys_without_archived_private_key(tmp_path):
    original = store(tmp_path)
    old = original.ensure()
    record = json.loads((original.root / "key.json").read_text())
    copied = store(tmp_path, fingerprint="another-machine")
    assert copied.status()["state"] == "quarantined"
    assert json.loads((copied.root / "key.json").read_text())["state"] == "quarantined"
    new = copied.ensure()
    assert new["keyId"] != old["keyId"]
    assert new["state"] == "unbound"
    archives = list((copied.root / "quarantine").glob("*.json"))
    assert len(archives) == 1
    assert "privateKey" not in json.loads(archives[0].read_text())
    assert record["privateKey"] not in archives[0].read_text()


def test_slug_collision_is_not_key_adoption(tmp_path):
    store(tmp_path, owner="alice@test.invalid").ensure()
    with pytest.raises(api().Error):
        store(tmp_path, owner="alice-test.invalid").ensure()


@pytest.mark.parametrize("owner", ["..", ".", "", "/"])
def test_path_traversal_owner_refused(tmp_path, owner):
    with pytest.raises(api().Error):
        store(tmp_path, owner=owner).ensure()


def test_sub_change_cannot_reuse_same_owner_key(tmp_path):
    keys = store(tmp_path)
    old = keys.ensure()
    changed = store(tmp_path, casdoor_sub="different-sub")
    assert changed.status()["state"] == "quarantined"
    assert changed.ensure()["keyId"] != old["keyId"]


@pytest.mark.parametrize("method", ["userKey.sign", "userKey.rotate", "userKey.revoke"])
def test_worker_cannot_claim_operator_through_ipc(tmp_path, method):
    # Exercise the real public router even on the RED parent, without starting
    # a daemon. Production readiness and stores are outside this auth assertion.
    import threading
    app = object.__new__(DaemonApplication)
    app._restore_done = threading.Event()
    app._restore_done.set()
    app._inbox = app._harnesses = app._presence = object()
    app.owner = "alice@test.invalid"
    app.state_dir = tmp_path / "state"
    app.hyprial_home = tmp_path
    with pytest.raises(DaemonRequestError) as caught:
        app.handle(method, {"purpose": "org.join", "payload": {},
                            "caller": "operator", "actor": "user:alice@test.invalid"},
                   _trusted_message_origin="worker")
    assert caught.value.code == "CALLER_NOT_AUTHORIZED"
