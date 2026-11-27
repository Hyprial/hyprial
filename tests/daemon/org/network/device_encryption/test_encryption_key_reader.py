"""Service grant device encryption key: the production key reader behind the published pair.

``encryption_key_reader`` is the one place the daemon asks for this device's
encryption key.  It answers ``None`` whenever the key store's prerequisites
are not met and never raises, so a device without a key publishes a row
without the pair instead of failing its publish.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path

import pytest

import hyprial.daemon.impl.org.network.device_key as device_key_module
from hyprial.daemon.impl.org.network.device_key import (
    KEY_UNAVAILABLE,
    encryption_key_reader,
    ensure_encryption_key,
)
from hyprial.identity import (
    DEVICE_RECORD_RELPATH,
    DeviceRecord,
    write_device_record,
)
from hyprial.kernel import atomic_json_write
from tests.support.tailnet_membership import key

OWNER = "alice@example"
DEVICE_ID = "device-a"
USER_ID = "subject-alice"
DEVICE_UID = uuid.UUID(int=0x1234, version=4).hex
ISSUER = "https://issuer.example.test"


def _settings(**overrides: object) -> dict[str, object]:
    return {
        "owner": OWNER,
        "identityMode": "casdoor",
        "identityIssuer": ISSUER,
        "userId": USER_ID,
        **overrides,
    }


def _home(
    tmp_path: Path,
    *,
    settings: dict[str, object] | None = None,
    device_owner: str = OWNER,
    device_uid: str | None = DEVICE_UID,
) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    atomic_json_write(home / "settings.json", settings or _settings())
    write_device_record(
        home,
        DeviceRecord(
            device_id=DEVICE_ID,
            owner=device_owner,
            server_public=key("server-node-a"),
            client_public=key("client-node-a"),
            device_uid=device_uid,
            name="device-a" if device_uid else None,
        ),
    )
    return home


def _keys(state: Path) -> list[dict[str, object]]:
    root = state / "identity" / "encryption-keys"
    return [json.loads(path.read_text("utf-8")) for path in sorted(root.glob("*/key.json"))]


@pytest.mark.parametrize(
    "settings, device_owner, device_uid",
    [
        pytest.param({"userId": None}, OWNER, DEVICE_UID, id="no-user-id"),
        pytest.param({"identityIssuer": None}, OWNER, DEVICE_UID, id="no-issuer"),
        pytest.param({}, "bob@example", DEVICE_UID, id="device-of-another-owner"),
        pytest.param({}, OWNER, None, id="device-without-a-uid"),
    ],
)
def test_an_incomplete_account_yields_no_key_and_mints_nothing(
    tmp_path: Path, settings: dict[str, object], device_owner: str, device_uid: str | None
) -> None:
    home = _home(
        tmp_path,
        settings=_settings(**settings) if settings else None,
        device_owner=device_owner,
        device_uid=device_uid,
    )
    state = tmp_path / "state"

    assert encryption_key_reader(home=home, state_dir=state)() is None

    assert not (state / "identity" / "encryption-keys").exists()


def test_a_complete_account_yields_the_public_half_with_the_store_digest_id(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)

    answer = encryption_key_reader(home=home, state_dir=tmp_path / "state")()

    [record] = _keys(tmp_path / "state")
    assert answer == {"keyId": record["keyId"], "publicKey": record["publicKey"]}
    assert (record["casdoorSub"], record["deviceUid"], record["keyKind"]) == (
        USER_ID,
        DEVICE_UID,
        "user",
    )
    raw = base64.b64decode(answer["publicKey"], validate=True)
    assert len(raw) == 32
    assert answer["keyId"] == "x25519:" + hashlib.sha256(raw).hexdigest()[:32]
    assert "privateKey" not in answer


def test_a_quarantined_store_yields_no_key(tmp_path: Path) -> None:
    home = _home(tmp_path)
    state = tmp_path / "state"
    assert ensure_encryption_key(home, state) is not None
    (state / "identity" / "encryption-keys").chmod(0o755)

    assert ensure_encryption_key(home, state) is None
    assert encryption_key_reader(home=home, state_dir=state)() is KEY_UNAVAILABLE


def test_a_copied_key_from_another_machine_yields_no_key(tmp_path: Path) -> None:
    """The store quarantines a record made under another machine fingerprint."""

    home = _home(tmp_path)
    state = tmp_path / "state"
    first = ensure_encryption_key(home, state)
    assert first is not None
    [path] = (state / "identity" / "encryption-keys").glob("*/key.json")
    record = json.loads(path.read_text("utf-8"))
    record["machineFingerprint"] = "0" * 64
    path.chmod(0o600)
    path.write_text(json.dumps(record), encoding="utf-8")

    assert encryption_key_reader(home=home, state_dir=state)() is KEY_UNAVAILABLE
    assert ensure_encryption_key(home, state) is None
    assert json.loads(path.read_text("utf-8"))["keyId"] == first["keyId"]


def test_a_corrupt_settings_file_is_no_key_not_a_crash(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "settings.json").write_text("{not json", encoding="utf-8")

    assert encryption_key_reader(home=home, state_dir=tmp_path / "state")() is None


def test_a_corrupt_device_record_is_no_key_not_a_crash(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / DEVICE_RECORD_RELPATH).write_text("{not json", encoding="utf-8")

    assert encryption_key_reader(home=home, state_dir=tmp_path / "state")() is None


def test_the_answer_repeats_identically_and_mints_one_key(tmp_path: Path) -> None:
    home = _home(tmp_path)
    reader = encryption_key_reader(home=home, state_dir=tmp_path / "state")

    assert reader() == reader() == reader()

    assert len(_keys(tmp_path / "state")) == 1


def test_a_missing_state_directory_is_created_privately(tmp_path: Path) -> None:
    home = _home(tmp_path)
    state = tmp_path / "state" / "nested"

    assert encryption_key_reader(home=home, state_dir=state)() is not None

    assert (state / "identity").stat().st_mode & 0o777 == 0o700


def test_the_key_is_scoped_to_the_account_and_the_device(tmp_path: Path) -> None:
    home = _home(tmp_path)
    state = tmp_path / "state"
    first = encryption_key_reader(home=home, state_dir=state)()
    atomic_json_write(home / "settings.json", _settings(userId="subject-bob"))
    other_account = encryption_key_reader(home=home, state_dir=state)()

    assert other_account is not None and first is not None
    assert other_account["keyId"] != first["keyId"]
    assert [record["casdoorSub"] for record in _keys(state)] == ["subject-alice", "subject-bob"]


def test_a_relative_state_directory_is_no_key(tmp_path: Path) -> None:
    home = _home(tmp_path)

    assert encryption_key_reader(home=home, state_dir=Path("state"))() is None


def test_a_concurrent_device_reprovision_cannot_mint_a_chimeric_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critic finding 3: the reader must use ``subject.device_uid``, never re-read ``device.json``."""

    home = _home(tmp_path)
    state = tmp_path / "state"
    other_uid = uuid.UUID(int=0x9999, version=4).hex
    real_subject = device_key_module.read_onboarding_subject

    def racing_subject(path: Path):
        subject = real_subject(path)
        write_device_record(
            path,
            DeviceRecord(
                device_id=DEVICE_ID,
                owner="bob@example",
                server_public=key("server-node-b"),
                client_public=key("client-node-b"),
                device_uid=other_uid,
                name="device-b",
            ),
        )
        return subject

    monkeypatch.setattr(device_key_module, "read_onboarding_subject", racing_subject)
    answer = ensure_encryption_key(home, state)

    assert answer is not None
    [record] = _keys(state)
    assert record["casdoorSub"] == USER_ID
    assert record["deviceUid"] == DEVICE_UID
    assert record["deviceUid"] != other_uid

