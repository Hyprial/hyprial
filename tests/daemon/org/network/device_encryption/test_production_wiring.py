"""Service grant S1: the production wiring of the published encryption key.

``_build_service`` is the one place every publish path constructs the network
service: the CLI ``org.*`` handlers, ``publish_self_for_app`` at startup and
on every refresh of the org directory, and the per-org actor.  The reader is
injected there, so a service built from the application publishes the pair
without any caller having to remember it.

These cases run the real ``org create`` handler over a real OrgFS runtime, the
same way ``test_org_network_ipc`` does, and add only what a key needs: a
committed ``userId``, a device ``deviceUid`` and the application state
directory.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.org.network.ipc import _build_service, ipc_org_create
from hyprial.identity import (
    DeviceRecord,
    address_path,
    remove_logout_artifacts,
    write_device_record,
)
from tests.daemon.org.network.test_org_network_ipc import NODE_ID, OWNER, _FakeApp
from tests.support.tailnet_membership import key

USER_ID = "subject-alice"
DEVICE_UID = uuid.UUID(int=0x1234, version=4).hex
ORG = "group-acme"
ROW_PATH = f"directory/devices/{OWNER}/{NODE_ID}.json"


def _app(
    tmp_path: Path, *, user_id: str | None, device_uid: str | None = DEVICE_UID
) -> tuple[_FakeApp, Path]:
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
    return app, key_state


def _key_records(key_state: Path) -> dict[str, str]:
    root = key_state / "identity" / "encryption-keys"
    records = [json.loads(path.read_text("utf-8")) for path in sorted(root.glob("*/key.json"))]
    return {record["keyId"]: record["publicKey"] for record in records}


def _device_row(app: _FakeApp) -> dict[str, object]:
    space_id = next(
        space.space_id for space in app._runtime.facade.spaces() if space.name == ORG
    )
    text, _version = app._runtime.facade.read_text(space_id, ROW_PATH)
    return json.loads(text)


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
