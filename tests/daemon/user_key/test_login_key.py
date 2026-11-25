"""The login user-key step the daemon runs itself (Z-5), over the public identity face.

Real key store under ``tmp_path``; nothing outside it is touched.  The step must
create the same record the shell's login stage creates for the same inputs, must
be idempotent, must never mint substitutes for a missing prerequisite, and must
never put key material in its answer.
"""

from __future__ import annotations

import json
from uuid import UUID

from hyprial.daemon.impl.user_key.login_key import ensure_login_user_key
from hyprial.identity import DeviceRecord, write_device_record
from hyprial.kernel import atomic_json_write, ipc_errors

OWNER = "alice@test.invalid"
UID = UUID("12345678-1234-4234-8234-123456789abc").hex


def _home(tmp_path, *, user_id: str | None = "test-sub", device_uid: str | None = UID):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    settings = {
        "owner": OWNER,
        "identityMode": "casdoor",
        "identityIssuer": "https://issuer.example.test",
    }
    if user_id is not None:
        settings["userId"] = user_id
    atomic_json_write(home / "settings.json", settings)
    device = DeviceRecord(
        "test-node",
        OWNER,
        "nodekey:server",
        "nodekey:client",
        device_uid=device_uid,
        name=None if device_uid is None else "synthetic-device",
    )
    write_device_record(home, device)
    return home, device


def _keys(state):
    return sorted(state.rglob("user-keys/*/key.json"))


def test_the_daemon_creates_the_key_and_answers_secret_free(tmp_path):
    home, device = _home(tmp_path)
    state = tmp_path / "state"

    answer = ensure_login_user_key(home, state, OWNER, device)

    assert answer == {"state": "unbound", "code": ipc_errors.USER_KEY_UNBOUND}
    (path,) = _keys(state)
    record = json.loads(path.read_text())
    assert (record["owner"], record["casdoorSub"], record["deviceUid"]) == (
        OWNER,
        "test-sub",
        UID,
    )
    assert record["privateKey"] not in json.dumps(answer)
    assert ensure_login_user_key(home, state, OWNER, device) == answer
    assert json.loads(path.read_text())["keyId"] == record["keyId"]


def test_a_missing_prerequisite_never_mints_a_substitute(tmp_path):
    home, device = _home(tmp_path, user_id=None)
    state = tmp_path / "state"

    assert ensure_login_user_key(home, state, OWNER, device)["state"] == "unavailable"
    assert ensure_login_user_key(home, state, OWNER, None)["state"] == "unavailable"
    foreign = DeviceRecord("n", "mallory@test.invalid", "nodekey:s", "nodekey:c")
    assert ensure_login_user_key(home, state, OWNER, foreign)["state"] == "unavailable"
    assert _keys(state) == []


def test_a_device_without_a_device_uid_has_no_key(tmp_path):
    home, device = _home(tmp_path, device_uid=None)

    answer = ensure_login_user_key(home, tmp_path / "state", OWNER, device)

    assert answer == {"state": "unavailable", "code": ipc_errors.USER_KEY_ABSENT}
    assert _keys(tmp_path / "state") == []
