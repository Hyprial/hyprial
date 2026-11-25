"""Serving recovery after a promoted first login (Z-5).

A start that promoted a bootstrap runs the recovery pass too: when the first
login left the device unregistered and the initialize receipt ``promoting``, the
pass registers the device, ensures the user key after it, and completes the
receipt.  Real ``OperationService`` journal under ``tmp_path`` and fake ports.
"""

from __future__ import annotations

from hyprial.daemon.impl.application.onboarding.account_lifecycle import (
    ServingOnboardingGateway,
)
from hyprial.identity import IdentityTransactionLock

from tests.daemon.onboarding.application.test_serving_recovery import (
    OWNER,
    Home,
    _bootstrap,
    _device,
)


class KeyRecorder:
    def __init__(self, home: Home) -> None:
        self.home = home
        self.calls: list[tuple] = []
        self.device_at_call: list[object] = []

    def __call__(self, home_dir, state_dir, owner, device):
        self.calls.append((home_dir, state_dir, owner, device))
        self.device_at_call.append(self.home.device)
        return {"state": "unbound", "code": "USER_KEY_UNBOUND"}


def _promoted_gateway(home: Home, accepted, completed: list, keys) -> ServingOnboardingGateway:
    gateway = ServingOnboardingGateway(
        operations=home.service,
        home=home.dir,
        complete_initialize=lambda: completed.append(True),
        promotion_operation_id=accepted.operation_id,
        identity_reader=home.read_identity,
        device_reader=lambda _home: home.device,
        journal_reader=lambda root: home.journal_reader(root),
        provisioner=home.provisioner,
    )
    gateway._recovery._user_key = keys
    return gateway


def test_a_promoted_start_runs_recovery_and_registers_a_missing_device(tmp_path):
    home = Home(tmp_path)
    accepted = home.accept()
    completed: list[bool] = []
    keys = KeyRecorder(home)
    gateway = _promoted_gateway(home, accepted, completed, keys)
    gateway._finalizer.mark_generation_migrated()

    gateway.on_daemon_ready()

    assert gateway._recovery.join(10) is True
    assert completed == [True]
    assert home.provisioner.calls == 1
    assert home.device is not None and home.device.owner == OWNER
    assert home.initialize_states() == [(OWNER, "completed")]


def test_the_user_key_is_ensured_after_the_device_and_only_then(tmp_path):
    home = Home(tmp_path)
    accepted = home.accept()
    keys = KeyRecorder(home)
    gateway = _promoted_gateway(home, accepted, [], keys)
    gateway._finalizer.mark_generation_migrated()

    gateway.on_daemon_ready()
    assert gateway._recovery.join(10) is True

    assert len(keys.calls) == 1
    home_dir, state_dir, owner, device = keys.calls[0]
    assert (home_dir, state_dir, owner) == (home.dir, tmp_path, OWNER)
    assert device is home.device
    assert keys.device_at_call == [home.device]


def test_a_start_whose_device_was_ready_runs_recovery_as_a_no_op(tmp_path):
    home = Home(tmp_path, device=_device())
    accepted = home.accept()
    home.service.mark_initialize(_bootstrap(), accepted.operation_id, "completed")
    keys = KeyRecorder(home)
    gateway = _promoted_gateway(home, accepted, [], keys)
    gateway._finalizer.mark_generation_migrated()

    gateway.on_daemon_ready()

    assert gateway._recovery.join(10) is True
    assert home.provisioner.calls == 0
    assert keys.calls == []
    assert home.initialize_states() == [(OWNER, "completed")]


def test_a_busy_identity_transaction_skips_the_user_key_but_completes_initialize(
    tmp_path, monkeypatch
):
    from hyprial.daemon.impl.application.onboarding.lifecycle import recovery

    monkeypatch.setattr(recovery, "_KEY_LOCK_SECONDS", 0.2)
    home = Home(tmp_path)
    home.accept()
    keys = KeyRecorder(home)
    recovering = home.recovery()
    recovering._user_key = keys

    with IdentityTransactionLock.acquire(home.dir, timeout=0):
        assert recovering.run() == "resumed"

    assert keys.calls == []
    assert home.initialize_states() == [(OWNER, "completed")]
    assert home.device is not None


def test_a_user_key_failure_never_blocks_the_initialize_receipt(tmp_path):
    home = Home(tmp_path)
    home.accept()
    recovering = home.recovery()

    def broken(*_args):
        raise RuntimeError("controlled key failure")

    recovering._user_key = broken

    assert recovering.run() == "resumed"
    assert home.initialize_states() == [(OWNER, "completed")]
