"""Service grant S1: the production wiring of the published encryption key.

``_build_service`` is the one place every publish path constructs the network
service: the CLI ``org.*`` handlers, ``publish_self_for_app`` and the per-org
actor.  The reader is injected there, so a service built from the application
publishes the pair without any caller having to remember it.

These cases run the real ``org create`` handler over a real OrgFS runtime, the
same way ``test_org_network_ipc`` does, and add only what a key needs: a
committed ``userId``, a device ``deviceUid`` and the application state
directory.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
import hyprial.daemon.impl.org.network.device_key as device_key_module
from hyprial.daemon.impl.org.network.device_key import ensure_encryption_key
from hyprial.daemon.impl.org.network.ipc import (
    _build_service,
    ipc_org_create,
    publish_self_for_app,
)
from hyprial.identity import (
    DeviceRecord,
    EncryptionKey,
    address_path,
    remove_logout_artifacts,
    write_device_record,
)
from hyprial.kernel import ProcessFileLock
from tests.daemon.org.network.test_org_network_ipc import NODE_ID, OWNER, _FakeApp
from tests.support.tailnet_membership import key

USER_ID = "subject-alice"
DEVICE_UID = uuid.UUID(int=0x1234, version=4).hex
ORG = "group-acme"
ROW_PATH = f"directory/devices/{OWNER}/{NODE_ID}.json"


class _EmptyAgents:
    def list(self) -> list[object]:
        return []


def _app(
    tmp_path: Path, *, user_id: str | None, device_uid: str | None = DEVICE_UID
) -> tuple[_FakeApp, Path]:
    (tmp_path / "state").mkdir(mode=0o700)
    runtime = OrgFsRuntime(tmp_path / "state", node_id=NODE_ID, author=f"user:{OWNER}")
    write_device_record(
        tmp_path,
        DeviceRecord(
            device_id=NODE_ID,
            owner=OWNER,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
            device_uid=device_uid,
            name="device-a" if device_uid else None,
        ),
    )
    address_path(tmp_path).write_text("tcp://10.0.0.9:7447#ipc-psk")
    settings = {
        "owner": OWNER,
        "identityMode": "casdoor",
        "identityIssuer": "https://issuer.example.test",
    }
    if user_id is not None:
        settings["userId"] = user_id
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    key_state = tmp_path / "key-state"
    key_state.mkdir(mode=0o700)
    app = _FakeApp(runtime, tmp_path)
    app.state_dir = key_state
    app.agents = _EmptyAgents()
    return app, key_state


def _key_records(key_state: Path) -> dict[str, str]:
    root = key_state / "identity" / "encryption-keys"
    records = [json.loads(path.read_text("utf-8")) for path in sorted(root.glob("*/key.json"))]
    return {record["keyId"]: record["publicKey"] for record in records}


def _device_row(app: _FakeApp, org: str = ORG) -> dict[str, object]:
    space_id = next(
        space.space_id for space in app._runtime.facade.spaces() if space.name == org
    )
    text, _version = app._runtime.facade.read_text(space_id, ROW_PATH)
    return json.loads(text)


def _fresh_app(app: _FakeApp, key_state: Path) -> _FakeApp:
    restarted = _FakeApp(app._runtime, app.hyprial_home)
    restarted.state_dir = key_state
    restarted.agents = _EmptyAgents()
    return restarted


def test_org_create_publishes_the_pair_from_the_committed_account(tmp_path: Path) -> None:
    app, key_state = _app(tmp_path, user_id=USER_ID)
    try:
        created = ipc_org_create(app, {"org": ORG})
        row = _device_row(app)
    finally:
        app._runtime.close()

    assert created["devicePublished"] is True
    [public_key] = _key_records(key_state).values()
    assert row["encryptionPublicKey"] == public_key
    assert row["encryptionKeyId"] in _key_records(key_state)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda tmp_path: _app(tmp_path, user_id=None), id="no-user-id"),
        pytest.param(
            lambda tmp_path: _app(tmp_path, user_id=USER_ID, device_uid=None),
            id="no-device-uid",
        ),
    ],
)
def test_org_create_publishes_no_pair_without_the_prerequisites(
    tmp_path: Path, make
) -> None:
    app, key_state = make(tmp_path)
    try:
        created = ipc_org_create(app, {"org": ORG})
        row = _device_row(app)
    finally:
        app._runtime.close()

    assert created["devicePublished"] is True
    assert "encryptionKeyId" not in row
    assert "encryptionPublicKey" not in row
    assert not (key_state / "identity").exists()


def test_a_refresh_publishes_the_pair_a_later_login_made_possible(tmp_path: Path) -> None:
    """The account is read on every build, not once when the daemon started."""

    app, key_state = _app(tmp_path, user_id=None)
    try:
        ipc_org_create(app, {"org": ORG})
        assert "encryptionKeyId" not in _device_row(app)
        settings = json.loads((tmp_path / "settings.json").read_text("utf-8"))
        settings["userId"] = USER_ID
        (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

        published = {"published": [ORG], "skipped": [], "errors": []}
        unchanged = {"published": [], "skipped": [], "errors": []}
        assert _build_service(app).publish_self() == published
        row = _device_row(app)
        assert _build_service(app).publish_self() == unchanged
        assert _device_row(app) == row
    finally:
        app._runtime.close()

    assert len(_key_records(key_state)) == 1
    assert "encryptionKeyId" in row


def test_an_application_without_a_state_directory_gets_no_reader(tmp_path: Path) -> None:
    """The handler layer still works for a caller that offers no private state."""

    app, _key_state = _app(tmp_path, user_id=USER_ID)
    del app.state_dir
    try:
        created = ipc_org_create(app, {"org": ORG})
        row = _device_row(app)
    finally:
        app._runtime.close()

    assert created["devicePublished"] is True
    assert "encryptionKeyId" not in row


def test_after_a_logout_the_service_no_longer_offers_a_key(tmp_path: Path) -> None:
    """The key belongs to an account, so a logged-out home gives none and makes none."""

    app, key_state = _app(tmp_path, user_id=USER_ID)
    try:
        ipc_org_create(app, {"org": ORG})
        remove_logout_artifacts(tmp_path)

        assert _build_service(app)._encryption_key() is None
    finally:
        app._runtime.close()

    assert len(_key_records(key_state)) == 1


@pytest.mark.parametrize("publish_first", [True, False], ids=["publish-first", "serving-first"])
def test_the_two_entrances_to_the_key_leave_one_key_and_one_row(
    tmp_path: Path, publish_first: bool
) -> None:
    """Serving start and the publish itself each ensure the key; neither makes a second.

    Which one runs first depends on when the forwarding maintenance tick
    fires, so both orders are tried against the real key store.
    """

    app, key_state = _app(tmp_path, user_id=USER_ID)
    try:
        entrances = (
            (ipc_org_create, ensure_encryption_key) if publish_first
            else (ensure_encryption_key, ipc_org_create)
        )
        for entrance in entrances:
            if entrance is ensure_encryption_key:
                entrance(tmp_path, key_state)
            else:
                entrance(app, {"org": ORG})
        ensure_encryption_key(tmp_path, key_state)
        assert _build_service(app).publish_self() == {
            "published": [],
            "skipped": [],
            "errors": [],
        }
        row = _device_row(app)
    finally:
        app._runtime.close()

    records = _key_records(key_state)
    assert list(records) == [row["encryptionKeyId"]]
    assert row["encryptionPublicKey"] == records[row["encryptionKeyId"]]


def test_a_held_store_lock_keeps_the_published_pair_and_does_not_block(
    tmp_path: Path,
) -> None:
    """Critic finding 1 & 2: a busy store lock must not erase the published pair or wait 15 s."""

    app, key_state = _app(tmp_path, user_id=USER_ID)
    try:
        ipc_org_create(app, {"org": ORG})
        before = _device_row(app)
        assert "encryptionKeyId" in before
        [key_dir] = list((key_state / "identity" / "encryption-keys").iterdir())
        lock = ProcessFileLock(key_dir / ".lock")
        restarted = _fresh_app(app, key_state)
        lock.acquire(0.0)
        try:
            started = time.monotonic()
            outcome = publish_self_for_app(restarted)
            elapsed = time.monotonic() - started
        finally:
            lock.release()
        during = _device_row(restarted)
        after_release = publish_self_for_app(restarted)
        after = _device_row(restarted)
    finally:
        app._runtime.close()

    assert (
        during.get("encryptionKeyId"),
        during.get("encryptionPublicKey"),
        elapsed < 3.0,
    ) == (
        before["encryptionKeyId"],
        before["encryptionPublicKey"],
        True,
    )
    assert outcome == {"published": [], "skipped": [], "errors": []}
    assert after_release == {"published": [], "skipped": [], "errors": []}
    assert after == before


def test_periodic_publishes_reuse_the_cached_pair_without_reentering_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steady-state periodic publishes hit memory cache and never call ``store.ensure()``."""

    app, _key_state = _app(tmp_path, user_id=USER_ID)
    try:
        ipc_org_create(app, {"org": "group-acme"})
        ipc_org_create(app, {"org": "group-beta"})
        publish_self_for_app(app)

        calls = 0
        real_ensure = EncryptionKey.Store.ensure

        def counted_ensure(self):
            nonlocal calls
            calls += 1
            return real_ensure(self)

        monkeypatch.setattr(EncryptionKey.Store, "ensure", counted_ensure)
        assert publish_self_for_app(app) == {"published": [], "skipped": [], "errors": []}
        assert publish_self_for_app(app) == {"published": [], "skipped": [], "errors": []}
    finally:
        app._runtime.close()

    assert calls == 0


def test_publish_self_for_app_reads_once_across_multiple_orgs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``publish_self_for_app`` pass resolves the device key once for every org."""

    app, key_state = _app(tmp_path, user_id=USER_ID)
    orgs = ("group-alpha", "group-beta", "group-gamma")
    try:
        for name in orgs:
            ipc_org_create(app, {"org": name})
        fresh = _fresh_app(app, key_state)

        ensure_calls = 0
        subject_calls = 0
        real_ensure = EncryptionKey.Store.ensure
        real_subject = device_key_module.read_onboarding_subject

        def counted_ensure(self):
            nonlocal ensure_calls
            ensure_calls += 1
            return real_ensure(self)

        def counted_subject(home: Path):
            nonlocal subject_calls
            subject_calls += 1
            return real_subject(home)

        monkeypatch.setattr(EncryptionKey.Store, "ensure", counted_ensure)
        monkeypatch.setattr(device_key_module, "read_onboarding_subject", counted_subject)
        outcome = publish_self_for_app(fresh)
        warm_outcome = publish_self_for_app(app)
    finally:
        app._runtime.close()

    assert outcome == {"published": [], "skipped": [], "errors": []}
    assert warm_outcome == {"published": [], "skipped": [], "errors": []}
    assert ensure_calls == 1
    assert subject_calls == 2


def test_cache_invalidates_when_the_account_or_device_changes(
    tmp_path: Path,
) -> None:
    """Changing ``userId`` or ``deviceUid`` invalidates the cached pair and republishes."""

    app, key_state = _app(tmp_path, user_id=USER_ID)
    second_uid = uuid.UUID(int=0x5678, version=4).hex
    try:
        ipc_org_create(app, {"org": ORG})
        first = _device_row(app)["encryptionKeyId"]

        settings = json.loads((tmp_path / "settings.json").read_text("utf-8"))
        settings["userId"] = "subject-bob"
        (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        assert publish_self_for_app(app)["published"] == [ORG]
        second = _device_row(app)["encryptionKeyId"]

        write_device_record(
            tmp_path,
            DeviceRecord(
                device_id=NODE_ID,
                owner=OWNER,
                server_public=key("server-node-a"),
                client_public=key("client-node-a"),
                device_uid=second_uid,
                name="device-b",
            ),
        )
        assert publish_self_for_app(app)["published"] == [ORG]
        third = _device_row(app)["encryptionKeyId"]
    finally:
        app._runtime.close()

    assert len({first, second, third}) == 3
    assert len(_key_records(key_state)) == 3

