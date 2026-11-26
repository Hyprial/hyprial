"""Service grant S1: the daemon ensures this device's encryption key and publishes it.

``publish_self`` carries ``encryptionKeyId`` / ``encryptionPublicKey`` on this
device's own directory row only when the local key store holds a key for the
committed account.  The service takes an injected key reader, the same way it
takes ``read_own_address``; the production reader is
``encryption_key_for_app`` and is tested here against the real store.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import uuid
from pathlib import Path

import pytest

import hyprial.identity as identity
from hyprial.daemon.impl.org.network.device_key import encryption_key_reader
from hyprial.daemon.impl.org.network.directory import InvalidDeviceQuarantine
from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.identity import id_form_minting
from hyprial.identity.impl.device import DeviceRecord, write_device_record
from tests.daemon.org.network.test_org_network_service import (
    NODE_ID,
    OWN_ADDRESS,
    OWNER,
    FakeFs,
    _create_org_spaces,
    _service,
)
from tests.support.tailnet_membership import key

ORG = "group-acme"
USER_ID = "subject-alice"
DEVICE_UID = uuid.UUID(int=0x1234, version=4).hex
ROW_PATH = f"directory/devices/{OWNER}/{NODE_ID}.json"


def _home(
    tmp_path: Path,
    *,
    user_id: str | None,
    device_uid: str | None = DEVICE_UID,
) -> Path:
    """A home whose account is (or is not) complete enough to hold a key."""

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
    settings = {"owner": OWNER, "identityIssuer": "https://issuer.example"}
    if user_id is not None:
        settings["userId"] = user_id
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return tmp_path


def _row(fs: FakeFs) -> dict[str, object]:
    return json.loads(fs.docs[("space-" + ORG, ROW_PATH)])


def _key_files(state: Path) -> list[Path]:
    root = state / "identity" / "encryption-keys"
    return sorted(root.glob("*/key.json")) if root.is_dir() else []


def _digest_id(public_key: str) -> str:
    return "x25519:" + hashlib.sha256(
        base64.b64decode(public_key, validate=True)
    ).hexdigest()[:32]


def test_the_service_takes_an_injected_key_reader() -> None:
    parameter = inspect.signature(OrgNetworkService.__init__).parameters
    assert "encryption_key" in parameter


@pytest.mark.parametrize(
    "user_id, device_uid",
    [
        pytest.param(None, DEVICE_UID, id="no-user-id"),
        pytest.param(USER_ID, None, id="no-device-uid"),
    ],
)
def test_without_a_complete_account_no_key_is_generated_and_the_row_has_no_pair(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, user_id, device_uid
) -> None:
    """Nothing is minted by a publish or by a read: an incomplete account has no key."""

    home = _home(tmp_path, user_id=user_id, device_uid=device_uid)
    state = tmp_path_factory.mktemp("state")
    fs = FakeFs()
    _create_org_spaces(fs)
    reader = encryption_key_reader(home=home, state_dir=state)

    assert _service(fs, home, encryption_key=reader).publish_self()["published"] == [ORG]

    row = _row(fs)
    assert "encryptionKeyId" not in row
    assert "encryptionPublicKey" not in row
    assert _key_files(state) == []
    assert reader() is None
    assert _key_files(state) == []


def test_a_generated_key_is_published_with_its_id_and_public_key(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The id is the store's digest of the published public key.

    A reader can therefore tell the two belong together without trusting
    either alone.
    """

    home = _home(tmp_path, user_id=USER_ID)
    state = tmp_path_factory.mktemp("state")
    fs = FakeFs()
    _create_org_spaces(fs)

    published = _service(
        fs, home, encryption_key=encryption_key_reader(home=home, state_dir=state)
    ).publish_self()
    assert published == {"published": [ORG], "skipped": [], "errors": []}

    row = _row(fs)
    [stored] = _key_files(state)
    record = json.loads(stored.read_text(encoding="utf-8"))
    assert row["encryptionKeyId"] == record["keyId"]
    assert row["encryptionPublicKey"] == record["publicKey"]
    assert row["encryptionKeyId"] == _digest_id(row["encryptionPublicKey"])
    assert record["keyKind"] == "user"
    assert record["casdoorSub"] == USER_ID
    assert record["deviceUid"] == DEVICE_UID
    assert record["state"] == "local"


def test_repeated_starts_keep_one_key_and_do_not_rewrite_an_unchanged_row(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    home = _home(tmp_path, user_id=USER_ID)
    state = tmp_path_factory.mktemp("state")
    fs = FakeFs()
    _create_org_spaces(fs)
    make = lambda: _service(  # noqa: E731 - the seam under test
        fs, home, encryption_key=encryption_key_reader(home=home, state_dir=state)
    )

    assert make().publish_self()["published"] == [ORG]
    published = _row(fs)
    key_id = published["encryptionKeyId"]

    for _ in range(3):
        assert make().publish_self() == {"published": [], "skipped": [], "errors": []}

    assert len(_key_files(state)) == 1
    assert _row(fs) == published
    assert _row(fs)["encryptionKeyId"] == key_id
    assert fs.calls.count(f"write:space-{ORG}:{ROW_PATH}") == 1


def test_a_row_published_before_the_key_existed_is_upgraded_once(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    home = _home(tmp_path, user_id=USER_ID)
    state = tmp_path_factory.mktemp("state")
    fs = FakeFs()
    _create_org_spaces(fs)
    without_key = _service(fs, home, encryption_key=lambda: None)
    assert without_key.publish_self()["published"] == [ORG]
    assert "encryptionKeyId" not in _row(fs)
    assert _key_files(state) == []

    reader = encryption_key_reader(home=home, state_dir=state)
    assert _service(fs, home, encryption_key=reader).publish_self()["published"] == [ORG]
    assert "encryptionKeyId" in _row(fs)
    assert _service(fs, home, encryption_key=reader).publish_self()["published"] == []


def test_a_key_the_store_refuses_publishes_no_pair_and_is_not_a_publish_error(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A quarantined or unsafe store is "no key", never a failed publish.

    The store is seeded first, then its directory is made unsafe so the fence
    refuses it.
    """

    home = _home(tmp_path, user_id=USER_ID)
    state = tmp_path_factory.mktemp("state")
    fs = FakeFs()
    _create_org_spaces(fs)
    reader = encryption_key_reader(home=home, state_dir=state)
    assert reader() is not None
    (state / "identity" / "encryption-keys").chmod(0o755)

    result = _service(fs, home, encryption_key=reader).publish_self()

    assert result == {"published": [ORG], "skipped": [], "errors": []}
    assert "encryptionKeyId" not in _row(fs)


def test_a_reader_that_raises_is_not_a_publish_error(tmp_path: Path) -> None:
    home = _home(tmp_path, user_id=USER_ID)
    fs = FakeFs()
    _create_org_spaces(fs)

    def exploded() -> dict:
        raise identity.UserKey.Error("USER_KEY_QUARANTINED")

    result = _service(fs, home, encryption_key=exploded).publish_self()

    assert result == {"published": [ORG], "skipped": [], "errors": []}
    assert "encryptionKeyId" not in _row(fs)


class _NoDirectoryGrant:
    """A policy that grants nothing, so another owner's row may not be written."""

    def allows(self, *, org: str, subject: str, action: str, resource: str) -> bool:
        return False


def test_a_refused_publish_never_reads_the_key(tmp_path: Path) -> None:
    """A daemon without the right to write the row must not touch the key store."""

    home = _home(tmp_path, user_id=USER_ID)
    fs = FakeFs()
    _create_org_spaces(fs)
    reads = 0

    def reader() -> None:
        nonlocal reads
        reads += 1
        return None

    result = OrgNetworkService(
        fs=fs,
        home=home,
        node_id=NODE_ID,
        owner="bob@example",
        invalid_devices=InvalidDeviceQuarantine(),
        policy=_NoDirectoryGrant(),
        read_own_address=lambda: OWN_ADDRESS,
        encryption_key=reader,
    ).publish_self()

    assert result == {
        "published": [],
        "skipped": [{"org": ORG, "reason": "ORG_ACTION_NOT_AUTHORIZED"}],
        "errors": [],
    }
    assert reads == 0


def test_the_private_half_is_never_published(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The row holds the public pair and nothing else that is new.

    ``deviceUid`` is on the row only while id-form minting is on.
    """

    home = _home(tmp_path, user_id=USER_ID)
    state = tmp_path_factory.mktemp("state")
    fs = FakeFs()
    _create_org_spaces(fs)
    _service(
        fs, home, encryption_key=encryption_key_reader(home=home, state_dir=state)
    ).publish_self()

    [stored] = _key_files(state)
    private = json.loads(stored.read_text(encoding="utf-8"))["privateKey"]
    text = fs.docs[("space-" + ORG, ROW_PATH)]
    assert private not in text
    row = _row(fs)
    expected = {
        "deviceId", "owner", "serverPublic", "clientPublic", "clientPublicByServer",
        "address", "keyGeneration", "updatedAt", "machineFingerprint",
        "firstPublishedAt", "encryptionKeyId", "encryptionPublicKey",
    } | ({"deviceUid"} if id_form_minting() else set())
    assert set(row) == expected


def test_a_temporarily_unavailable_reader_keeps_the_existing_pair_on_the_row(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Transient key-read errors preserve the published pair instead of erasing it."""

    home = _home(tmp_path, user_id=USER_ID)
    state = tmp_path_factory.mktemp("state")
    fs = FakeFs()
    _create_org_spaces(fs)
    reader = encryption_key_reader(home=home, state_dir=state)
    assert _service(fs, home, encryption_key=reader).publish_self()["published"] == [ORG]
    published = _row(fs)
    writes_before = fs.calls.count(f"write:space-{ORG}:{ROW_PATH}")

    def transient_io() -> dict:
        raise OSError("temporary I/O failure")

    result = _service(fs, home, encryption_key=transient_io).publish_self()

    assert result == {"published": [], "skipped": [], "errors": []}
    assert _row(fs) == published
    assert fs.calls.count(f"write:space-{ORG}:{ROW_PATH}") == writes_before


def test_an_address_change_during_a_transient_key_failure_preserves_the_pair(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """When another field changes while the key is temporarily unreadable, the pair stays."""

    home = _home(tmp_path, user_id=USER_ID)
    state = tmp_path_factory.mktemp("state")
    fs = FakeFs()
    _create_org_spaces(fs)
    reader = encryption_key_reader(home=home, state_dir=state)
    assert _service(fs, home, encryption_key=reader).publish_self()["published"] == [ORG]
    published = _row(fs)

    def busy() -> dict:
        raise identity.UserKey.Error("USER_KEY_QUARANTINED")

    result = _service(
        fs,
        home,
        read_own_address=lambda: "tcp://10.0.0.99:7447#rotated",
        encryption_key=busy,
    ).publish_self()

    assert result == {"published": [ORG], "skipped": [], "errors": []}
    updated = _row(fs)
    assert updated["address"] == "tcp://10.0.0.99:7447#rotated"
    assert updated["encryptionKeyId"] == published["encryptionKeyId"]
    assert updated["encryptionPublicKey"] == published["encryptionPublicKey"]


def test_a_definitely_absent_key_publishes_a_row_without_the_pair(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Only an explicit no-key answer (missing prerequisites) clears the pair."""

    home = _home(tmp_path, user_id=USER_ID)
    state = tmp_path_factory.mktemp("state")
    fs = FakeFs()
    _create_org_spaces(fs)
    reader = encryption_key_reader(home=home, state_dir=state)
    assert _service(fs, home, encryption_key=reader).publish_self()["published"] == [ORG]
    assert "encryptionKeyId" in _row(fs)

    result = _service(fs, home, encryption_key=lambda: None).publish_self()

    assert result == {"published": [ORG], "skipped": [], "errors": []}
    assert "encryptionKeyId" not in _row(fs)
    assert "encryptionPublicKey" not in _row(fs)

