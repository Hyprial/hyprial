"""Serving-start auto-completion of device registration and initialize (Z-3).

Every case uses the real ``OperationService`` journal under ``tmp_path`` and
fake identity/device/provisioner ports.  No daemon, socket, sidecar, issuer or
home outside ``tmp_path`` is touched.
"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
    ServingOnboardingGateway,
    observe_account,
)
from hyprial.daemon.impl.application.onboarding.lifecycle import ServingRecovery
from hyprial.daemon.impl.application.onboarding.provisioning import (
    DeviceProvisioningError,
)
from hyprial.daemon.impl.onboarding.operations import OperationService, journal
from hyprial.daemon.impl.onboarding.operations.journal import (
    JournalState,
    OperationReceiptError,
)
from hyprial.daemon.impl.request_catalog.onboarding import CallContext, Surface
from hyprial.daemon.impl.request_catalog.onboarding.state import StoreSnapshot
from hyprial.identity import DeviceRecord, IdentitySnapshot

ISSUER = "https://issuer.example.test"
OWNER = "alice@example.test"
OTHER_OWNER = "mallory@example.test"
SERVING = CallContext(
    surface=Surface("home-binding", "host-control", "view", "serving-1"),
    phase="serving",
    caller="local-gui",
)


def _device(owner: str = OWNER) -> DeviceRecord:
    return DeviceRecord(
        device_id="device-000001",
        owner=owner,
        server_public="nodekey:server-public",
        client_public="nodekey:client-public",
        key_generation=1,
    )


class CallableProvisioner:
    def __init__(self, function) -> None:
        self.ensure = function


def _bootstrap(owner: str = OWNER) -> CallContext:
    return CallContext(
        surface=Surface("home-binding", "host-control", "view", "bootstrap-1"),
        phase="bootstrap",
        caller="local-bootstrap",
        owner=owner,
    )


class Provisioner:
    def __init__(self, home: "Home", error: BaseException | None = None) -> None:
        self.home = home
        self.error = error
        self.calls = 0

    def ensure(self, *, home, owner):
        self.calls += 1
        if self.error is not None:
            raise self.error
        self.home.device = _device(owner)
        return self.home.device


class Home:
    def __init__(self, tmp_path, *, owner=OWNER, mode="casdoor", device=None) -> None:
        self.dir = tmp_path / "home"
        self.dir.mkdir()
        self.identity = IdentitySnapshot(owner, mode, ISSUER if owner else None)
        self.device = device
        self.service = OperationService.open(tmp_path / "operations")
        self.provisioner = Provisioner(self)
        self.identity_error: BaseException | None = None
        self.journal_reader = journal.read_journal

    def read_identity(self, _home):
        if self.identity_error is not None:
            raise self.identity_error
        return self.identity

    def recovery(self) -> ServingRecovery:
        return ServingRecovery(
            operations=self.service,
            home=self.dir,
            identity_reader=self.read_identity,
            device_reader=lambda _home: self.device,
            journal_reader=lambda root: self.journal_reader(root),
            provisioner=self.provisioner,
        )

    def gateway(self) -> ServingOnboardingGateway:
        return ServingOnboardingGateway(
            operations=self.service,
            home=self.dir,
            identity_reader=self.read_identity,
            device_reader=lambda _home: self.device,
            journal_reader=lambda root: self.journal_reader(root),
            provisioner=self.provisioner,
        )

    def accept(self, *, owner: str = OWNER, key: str = "initialize-key-0001"):
        return self.service.accept_initialize(
            _bootstrap(owner),
            key,
            "op-" + key,
            "receipt-" + key,
            promotion=lambda _record: None,
        )

    def initialize_states(self) -> list[tuple[str | None, str]]:
        state = journal.read_journal(self.service.root)
        records = () if state is None else state.snapshot.records
        return [(r.account_owner, r.state) for r in records if r.kind == "initialize"]

    def device_states(self) -> list[str]:
        state = journal.read_journal(self.service.root)
        records = () if state is None else state.snapshot.records
        return [r.state for r in records if r.kind == "device"]

    def project(self):
        return observe_account(
            home=self.dir,
            operations_root=self.service.root,
            identity_reader=self.read_identity,
            device_reader=lambda _home: self.device,
        )


def test_promoting_receipt_of_the_same_owner_is_completed(tmp_path):
    home = Home(tmp_path, device=_device())
    accepted = home.accept()
    assert accepted.state == "promoting"

    assert home.recovery().run() == "resumed"

    assert home.initialize_states() == [(OWNER, "completed")]
    assert home.project().initialized is True
    assert home.provisioner.calls == 0


def test_unknown_receipt_of_the_same_owner_is_completed(tmp_path):
    home = Home(tmp_path, device=_device())
    accepted = home.accept()
    home.service.mark_initialize(_bootstrap(), accepted.operation_id, "unknown")

    assert home.recovery().run() == "resumed"

    assert home.initialize_states() == [(OWNER, "completed")]


def test_accepted_receipt_is_left_alone_without_raising(tmp_path):
    home = Home(tmp_path, device=_device())
    accepted = home.accept()
    current = journal.read_journal(home.service.root)
    pending = replace(accepted, state="accepted", cancelable=True)
    home.journal_reader = lambda _root: JournalState(
        snapshot=StoreSnapshot((pending,), current.snapshot.key_index),
        writer_identities=current.writer_identities,
        controller_identities=current.controller_identities,
    )

    assert home.recovery().run() == "nothing-to-resume"

    assert home.initialize_states() == [(OWNER, "promoting")]


def test_receipt_of_another_owner_is_never_touched(tmp_path):
    home = Home(tmp_path, device=_device())
    home.accept(owner=OTHER_OWNER, key="initialize-key-other")

    home.recovery().run()

    states = dict(home.initialize_states())
    assert states[OTHER_OWNER] == "promoting"
    assert states[OWNER] == "completed"


def test_device_record_of_another_owner_is_reprovisioned_for_the_current_owner(tmp_path):
    home = Home(tmp_path, device=_device(OTHER_OWNER))
    previous = home.accept(owner=OTHER_OWNER, key="initialize-key-other")
    home.service.mark_initialize(_bootstrap(OTHER_OWNER), previous.operation_id, "completed")
    before = [
        r for r in journal.read_journal(home.service.root).snapshot.records
        if r.account_owner == OTHER_OWNER
    ]
    assert home.project().next_actions == ("device.ensure",)

    assert home.recovery().run() == "initialized"

    assert home.provisioner.calls == 1
    assert home.device.owner == OWNER
    assert home.initialize_states().count((OWNER, "completed")) == 1
    after = [
        r for r in journal.read_journal(home.service.root).snapshot.records
        if r.account_owner == OTHER_OWNER
    ]
    assert after == before
    projected = home.project()
    assert (projected.device_ready, projected.initialized) == (True, True)


def test_failed_initialize_receipt_is_followed_by_a_fresh_initialize(tmp_path):
    home = Home(tmp_path, device=_device())
    failed = home.accept()
    home.service.mark_initialize(_bootstrap(), failed.operation_id, "failed")
    before = next(
        r for r in journal.read_journal(home.service.root).snapshot.records
        if r.operation_id == failed.operation_id
    )
    assert home.project().next_actions == ("initialize",)

    assert home.recovery().run() == "initialized"

    assert sorted(home.initialize_states()) == [(OWNER, "completed"), (OWNER, "failed")]
    after = next(
        r for r in journal.read_journal(home.service.root).snapshot.records
        if r.operation_id == failed.operation_id
    )
    assert after == before
    assert home.project().initialized is True
    assert home.recovery().run() == "already-complete"
    assert len(home.initialize_states()) == 2


def test_fifty_failed_starts_keep_the_journal_bounded_and_a_healthy_start_recovers(tmp_path):
    home = Home(tmp_path)
    home.provisioner.error = DeviceProvisioningError("sidecar missing")
    path = journal.journal_path(home.service.root)
    sizes = []
    for _ in range(50):
        assert home.recovery().run() == "device-failed"
        sizes.append((len(journal.read_journal(home.service.root).snapshot.records),
                      path.stat().st_size))

    assert home.provisioner.calls == 50
    assert max(count for count, _ in sizes) <= 8
    assert sizes[-1] == sizes[7]
    assert max(size for _, size in sizes) <= 16 * 1024
    assert home.device_states() == ["failed"] * 8
    assert home.project().next_actions == ("device.ensure",)

    home.provisioner.error = None
    assert home.recovery().run() == "initialized"
    assert home.initialize_states() == [(OWNER, "completed")]
    assert sorted(home.device_states()) == ["failed"] * 8 + ["ready"]
    assert home.project().initialized is True


def test_authenticated_home_without_device_is_auto_completed(tmp_path):
    home = Home(tmp_path)
    assert home.project().next_actions == ("device.ensure",)

    assert home.recovery().run() == "initialized"

    assert home.provisioner.calls == 1
    assert home.device_states() == ["ready"]
    assert home.initialize_states() == [(OWNER, "completed")]
    projected = home.project()
    assert (projected.device_ready, projected.initialized) == (True, True)
    assert projected.next_actions == ()


def test_registered_device_without_initialize_only_initializes(tmp_path):
    home = Home(tmp_path, device=_device())

    assert home.recovery().run() == "initialized"

    assert home.provisioner.calls == 0
    assert home.initialize_states() == [(OWNER, "completed")]


def test_second_start_is_idempotent(tmp_path):
    home = Home(tmp_path)
    home.recovery().run()
    before = journal.read_journal(home.service.root).snapshot

    assert home.recovery().run() == "already-complete"

    assert journal.read_journal(home.service.root).snapshot == before
    assert home.provisioner.calls == 1


def test_provisioner_failure_keeps_the_status_truthful_and_the_next_start_retries(
    tmp_path,
):
    home = Home(tmp_path)
    home.provisioner.error = DeviceProvisioningError("sidecar missing")

    assert home.recovery().run() == "device-failed"

    assert home.device_states() == ["failed"]
    assert home.initialize_states() == []
    projected = home.project()
    assert (projected.device_ready, projected.initialized) == (False, False)
    assert projected.next_actions == ("device.ensure",)

    home.provisioner.error = None
    assert home.recovery().run() == "initialized"
    assert sorted(home.device_states()) == ["failed", "ready"]
    assert home.project().initialized is True


def test_outcome_unknown_provisioning_is_recorded_unknown(tmp_path):
    home = Home(tmp_path)
    home.provisioner.error = DeviceProvisioningError("lost", outcome_unknown=True)

    assert home.recovery().run() == "device-failed"

    assert home.device_states() == ["unknown"]
    assert home.initialize_states() == []


def test_unexpected_provisioner_exception_is_contained(tmp_path):
    home = Home(tmp_path)
    home.provisioner.error = RuntimeError("boom")

    assert home.recovery().run() == "device-failed"

    assert home.initialize_states() == []


def test_provisioner_returning_another_owners_device_is_refused(tmp_path):
    home = Home(tmp_path)

    class Wrong:
        def ensure(self, *, home, owner):
            return _device(OTHER_OWNER)

    home.provisioner = Wrong()

    assert home.recovery().run() == "device-failed"
    assert home.device_states() == ["failed"]
    assert home.initialize_states() == []


def test_owner_change_during_provisioning_leaves_initialize_unwritten(tmp_path):
    home = Home(tmp_path)

    def ensure(*, home, owner):
        home_object.identity = IdentitySnapshot(None, None, None)
        home_object.device = _device(owner)
        return home_object.device

    home_object = home
    home.provisioner = CallableProvisioner(ensure)

    assert home.recovery().run() == "owner-changed"

    assert home.initialize_states() == []


def test_corrupt_journal_never_raises_and_leaves_state_untouched(tmp_path):
    home = Home(tmp_path, device=_device())
    home.accept()
    journal.journal_path(home.service.root).write_bytes(b"not a journal")

    gateway = home.gateway()
    gateway.on_daemon_ready()

    assert gateway._recovery.join(30) is True
    assert journal.journal_path(home.service.root).read_bytes() == b"not a journal"
    assert home.recovery().run() == "failed"


def test_unreadable_journal_reader_is_contained(tmp_path):
    home = Home(tmp_path, device=_device())

    def broken(_root):
        raise OperationReceiptError("OPERATION_STORE_CORRUPT", "journal is corrupt")

    home.journal_reader = broken

    assert home.recovery().run() == "failed"
    assert home.provisioner.calls == 0


def test_identity_read_failure_is_contained(tmp_path):
    home = Home(tmp_path, device=_device())
    home.accept()
    home.identity_error = ValueError("settings are corrupt")

    assert home.recovery().run() == "failed"

    assert home.initialize_states() == [(OWNER, "promoting")]


@pytest.mark.parametrize(
    ("owner", "mode"), [(None, None), (OWNER, "local"), (OWNER, None)]
)
def test_only_a_committed_account_identity_is_recovered(tmp_path, owner, mode):
    home = Home(tmp_path, owner=owner, mode=mode)

    home.recovery().run()

    assert home.provisioner.calls == 0
    assert home.initialize_states() == []
    assert home.device_states() == []


def test_ready_hook_returns_while_provisioning_is_still_running(tmp_path):
    home = Home(tmp_path)
    release = threading.Event()
    entered = threading.Event()

    def ensure(*, home, owner):
        entered.set()
        assert release.wait(30)
        home_object.device = _device(owner)
        return home_object.device

    home_object = home
    home.provisioner = CallableProvisioner(ensure)
    gateway = home.gateway()
    try:
        gateway.on_daemon_ready()
        assert entered.wait(30)
        status = gateway.status(SERVING)
        assert (status["deviceReady"], status["initialized"]) == (False, False)
        assert status["nextActions"] == ["device.ensure"]
    finally:
        release.set()
    assert gateway._recovery.join(30) is True
    status = gateway.status(SERVING)
    assert (status["deviceReady"], status["initialized"]) == (True, True)
    assert status["nextActions"] == []
    gateway.close()


def test_ready_hook_is_started_once_per_gateway(tmp_path):
    home = Home(tmp_path)
    gateway = home.gateway()
    gateway.on_daemon_ready()
    gateway.on_daemon_ready()
    assert gateway._recovery.join(30) is True
    assert home.provisioner.calls == 1
    gateway.close()
