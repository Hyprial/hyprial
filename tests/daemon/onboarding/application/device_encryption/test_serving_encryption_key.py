"""Service grant S1: serving start ensures this device's encryption key.

A daemon that has just become serving and already holds a committed account
(``userId``) and a device record must leave serving start with exactly one
encryption key in the local key store, whether or not the device is a member
of any org yet.  The step runs on every serving start and on a relogin, is
idempotent, and never changes the recovery outcome: a device that cannot
have a key is not a failed recovery.

Every case uses the real operation journal and the real key store under
``tmp_path``; identity and device records are read from a home directory
under ``tmp_path``.  Nothing outside it is touched.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest

from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
    ServingOnboardingGateway,
)
from hyprial.daemon.impl.application.onboarding.lifecycle import (
    ServingRecovery,
    recovery as recovery_module,
)
from hyprial.daemon.impl.onboarding.operations import OperationService, journal
from hyprial.daemon.impl.org.network.device_key import ensure_encryption_key
from hyprial.identity import (
    DeviceRecord,
    IdentitySnapshot,
    IdentityTransactionLock,
    write_device_record,
)

ISSUER = "https://issuer.example.test"
OWNER = "alice@example"
USER_ID = "subject-alice"
DEVICE_UID = uuid.UUID(int=0x1234, version=4).hex


def _device(device_uid: str | None = DEVICE_UID) -> DeviceRecord:
    return DeviceRecord(
        device_id="device-000001",
        owner=OWNER,
        server_public="nodekey:server-public",
        client_public="nodekey:client-public",
        key_generation=1,
        device_uid=device_uid,
        name="device-a" if device_uid else None,
    )


class KeyHome:
    """A home with a committed account, a device record and an empty journal.

    The journal sits at ``<state_dir>/onboarding``, the production layout: the
    pass derives the state directory as the parent of the journal root.
    """

    def __init__(self, tmp_path: Path, *, user_id: str | None = USER_ID) -> None:
        self.dir = tmp_path / "home"
        self.dir.mkdir()
        self.state = tmp_path / "state"
        self.state.mkdir(mode=0o700)
        settings = {"owner": OWNER, "identityMode": "casdoor", "identityIssuer": ISSUER}
        if user_id is not None:
            settings["userId"] = user_id
        (self.dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        self.identity = IdentitySnapshot(OWNER, "casdoor", ISSUER)
        self.device: DeviceRecord | None = None
        self.provisioned = 0
        self.service = OperationService.open(self.state / "onboarding")

    def registered(self, device_uid: str | None = DEVICE_UID) -> "KeyHome":
        write_device_record(self.dir, _device(device_uid))
        self.device = _device(device_uid)
        return self

    def ensure(self, *, home, owner: str) -> DeviceRecord:
        self.provisioned += 1
        self.device = _device()
        write_device_record(self.dir, self.device)
        return self.device

    def keys(self) -> list[str]:
        root = self.state / "identity" / "encryption-keys"
        return sorted(path.parent.name for path in root.glob("*/key.json"))

    def initialize_states(self) -> list[tuple[str | None, str]]:
        state = journal.read_journal(self.service.root)
        records = () if state is None else state.snapshot.records
        return [(r.account_owner, r.state) for r in records if r.kind == "initialize"]

    def recovery(self, encryption_key=ensure_encryption_key) -> ServingRecovery:
        return ServingRecovery(
            operations=self.service,
            home=self.dir,
            identity_reader=lambda _home: self.identity,
            device_reader=lambda _home: self.device,
            journal_reader=lambda root: journal.read_journal(root),
            provisioner=self,
            encryption_key=encryption_key,
        )

    def gateway(self) -> ServingOnboardingGateway:
        return ServingOnboardingGateway(
            operations=self.service,
            home=self.dir,
            identity_reader=lambda _home: self.identity,
            device_reader=lambda _home: self.device,
            journal_reader=lambda root: journal.read_journal(root),
            provisioner=self,
        )


def _completed(home: KeyHome) -> None:
    """Leave the journal as a device that was initialized on an earlier start."""

    assert home.recovery().run() == "initialized"
    for path in (home.state / "identity").rglob("key.json"):
        path.unlink()


def test_serving_start_gives_a_registered_device_its_key(tmp_path: Path) -> None:
    home = KeyHome(tmp_path).registered()

    assert home.recovery().run() == "initialized"

    assert len(home.keys()) == 1


def test_a_device_that_needs_nothing_else_still_gets_its_key(tmp_path: Path) -> None:
    """A device registered before this change is ``already-complete`` on start."""

    home = KeyHome(tmp_path).registered()
    _completed(home)

    assert home.recovery().run() == "already-complete"

    assert len(home.keys()) == 1


def test_a_device_provisioned_by_this_start_gets_its_key(tmp_path: Path) -> None:
    home = KeyHome(tmp_path)

    assert home.recovery().run() == "initialized"

    assert home.provisioned == 1
    assert len(home.keys()) == 1


@pytest.mark.parametrize(
    "make_home",
    [
        pytest.param(lambda tmp_path: KeyHome(tmp_path, user_id=None).registered(), id="no-user-id"),
        pytest.param(lambda tmp_path: KeyHome(tmp_path).registered(None), id="no-device-uid"),
    ],
)
def test_without_the_prerequisites_no_key_is_made_and_the_outcome_stands(
    tmp_path: Path, make_home
) -> None:
    home = make_home(tmp_path)

    assert home.recovery().run() == "initialized"

    assert home.initialize_states() == [(OWNER, "completed")]
    assert not (home.state / "identity").exists()


def test_an_ownerless_home_makes_no_key(tmp_path: Path) -> None:
    home = KeyHome(tmp_path).registered()
    home.identity = IdentitySnapshot(None, None, None)

    assert home.recovery().run() == "no-owner"

    assert not (home.state / "identity").exists()


def test_a_reprovisioned_device_gets_the_key_of_the_new_record(tmp_path: Path) -> None:
    old_device_uid = uuid.UUID(int=0x5678, version=4).hex
    home = KeyHome(tmp_path).registered(old_device_uid)
    home.device = DeviceRecord(
        device_id="device-000001",
        owner="bob@example",
        server_public="nodekey:server-public",
        client_public="nodekey:client-public",
        key_generation=1,
        device_uid=old_device_uid,
        name="device-b",
    )
    write_device_record(home.dir, home.device)

    assert home.recovery().run() == "initialized"

    assert len(home.keys()) == 1
    [record] = [
        json.loads(path.read_text("utf-8"))
        for path in (home.state / "identity" / "encryption-keys").glob("*/key.json")
    ]
    assert record["deviceUid"] == DEVICE_UID
    assert record["deviceUid"] != old_device_uid


def test_repeated_starts_keep_one_key(tmp_path: Path) -> None:
    home = KeyHome(tmp_path).registered()

    outcomes = [home.recovery().run() for _ in range(4)]

    assert outcomes == ["initialized"] + ["already-complete"] * 3
    assert len(home.keys()) == 1


def test_a_key_step_that_raises_does_not_change_the_outcome(tmp_path: Path) -> None:
    home = KeyHome(tmp_path).registered()

    def exploded(_home: Path, _state: Path) -> None:
        raise RuntimeError("boom")

    assert home.recovery(exploded).run() == "initialized"

    assert home.initialize_states() == [(OWNER, "completed")]


def test_the_ready_hook_ends_with_the_key_in_place(tmp_path: Path) -> None:
    home = KeyHome(tmp_path).registered()
    gateway = home.gateway()
    try:
        gateway.on_daemon_ready()
        assert gateway._recovery.join(30) is True
    finally:
        gateway.close()

    assert len(home.keys()) == 1


def test_a_relogin_ensures_the_key_for_the_committed_account(tmp_path: Path) -> None:
    home = KeyHome(tmp_path).registered()
    recovery = home.recovery()

    block = recovery.recover_relogin(OWNER)

    assert block["ready"] is True
    assert len(home.keys()) == 1


def test_a_stuck_key_step_does_not_hold_serving_start(tmp_path: Path) -> None:
    """The key step runs off the startup thread, like the rest of recovery."""

    home = KeyHome(tmp_path).registered()
    entered = threading.Event()

    def stuck(_home: Path, _state: Path) -> None:
        entered.set()
        threading.Event().wait()

    gateway = home.gateway()
    recovery = home.recovery(stuck)
    gateway._recovery.close()
    gateway._recovery = recovery
    try:
        gateway.on_daemon_ready()
        assert entered.wait(10)
    finally:
        gateway.close()


def test_the_pass_hands_the_key_step_the_home_and_the_daemon_state_directory(
    tmp_path: Path,
) -> None:
    """The production step takes its two paths positionally, like the signing-key step.

    A step that is called with another convention raises ``TypeError``; the
    pass contains it and only logs, so the device would silently never get a
    key.  This pins the convention the production default is called with.
    """

    home = KeyHome(tmp_path).registered()
    calls: list[tuple[Path, ...]] = []

    def record(*paths: Path) -> dict[str, str] | None:
        calls.append(paths)
        return ensure_encryption_key(*paths)

    assert home.recovery(record).run() == "initialized"

    assert calls == [(home.dir, home.state)]
    assert len(home.keys()) == 1


def test_the_serving_key_is_a_user_key_bound_to_the_account_and_the_device(
    tmp_path: Path,
) -> None:
    """Only a ``user`` kind key can open a sealed credential on this device."""

    home = KeyHome(tmp_path).registered()
    home.recovery().run()

    [record] = [
        json.loads(path.read_text("utf-8"))
        for path in (home.state / "identity" / "encryption-keys").glob("*/key.json")
    ]
    assert record["keyKind"] == "user"
    assert record["algorithm"] == "X25519"
    assert (record["casdoorSub"], record["deviceUid"]) == (USER_ID, DEVICE_UID)
    assert record["state"] == "local"
    assert list(home.state.joinpath("identity").iterdir()) == [
        home.state / "identity" / "encryption-keys"
    ]


def test_the_encryption_key_step_holds_the_identity_transaction_lock(
    tmp_path: Path,
) -> None:
    """Critic finding 3: encryption-key ensure runs inside ``IdentityTransactionLock``."""

    home = KeyHome(tmp_path).registered()
    lock_held: list[bool] = []

    def observe(home_dir: Path, state_dir: Path) -> dict[str, str] | None:
        lock_held.append(IdentityTransactionLock.is_held_in_process(home_dir))
        return ensure_encryption_key(home_dir, state_dir)

    assert home.recovery(observe).run() == "initialized"

    assert lock_held == [True]
    assert len(home.keys()) == 1


def test_a_busy_identity_transaction_skips_the_encryption_key_without_failing_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy identity transaction skips encryption-key creation without blocking recovery."""

    monkeypatch.setattr(recovery_module, "_KEY_LOCK_SECONDS", 0.1)
    home = KeyHome(tmp_path).registered()

    with IdentityTransactionLock.acquire(home.dir, timeout=0):
        assert home.recovery().run() == "initialized"

    assert home.initialize_states() == [(OWNER, "completed")]
    assert not (home.state / "identity").exists()

