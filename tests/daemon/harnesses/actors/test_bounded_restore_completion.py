from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.harnesses.actor import HarnessSettlementReceipt
from hyprial.daemon.impl.harnesses.actor import facade as facade_module
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
from hyprial.kernel import HarnessLaunchSpec
from tests.support.network_owner_context import OwnerContextTracer


class _Process:
    running = True
    pid = None

    def stop(self) -> None:
        self.running = False


class _BlockedLauncher:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def start(self, _spec: HarnessLaunchSpec) -> _Process:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(3.0)
        return _Process()


def test_restore_recovery_uses_the_exact_operation_not_current_target_state(
    monkeypatch,
) -> None:
    operation_ids = iter(
        ("restore-operation", "later-restore-operation", "stop-operation")
    )
    monkeypatch.setattr(
        facade_module,
        "uuid",
        SimpleNamespace(
            uuid4=lambda: SimpleNamespace(hex=next(operation_ids))
        ),
    )
    launcher = _BlockedLauncher()
    launcher.release.set()
    runtime = ManagedHarnessRuntime(launcher, start_timeout_seconds=0.0)
    spec = HarnessLaunchSpec("fake", "worker", True)
    receipt = HarnessSettlementReceipt(
        "restore-operation", ("fake:worker",)
    )
    try:
        runtime._actor._lifecycle_effect_resources.add("fake:worker")
        summary = runtime.restore((spec,))
        assert summary.deferred == 1

        runtime._actor._lifecycle_effect_resources.remove("fake:worker")
        assert runtime.restore((spec,)).restored == 1
        recovered = runtime._facade.recover_restore(receipt)
        assert recovered is not None
        assert recovered.deferred == 1
        assert recovered.restored == 0
        assert runtime._facade.recover_restore(
            HarnessSettlementReceipt("never-admitted", ("fake:worker",))
        ) is None
    finally:
        runtime.stop(3.0)


def test_timer_disabled_restore_is_recoverable_after_restart_without_resubmit(
    tmp_path,
) -> None:
    desired_path = tmp_path / "desired-state.json"
    launcher = _BlockedLauncher()
    runtime = ManagedHarnessRuntime(
        launcher,
        start_timeout_seconds=0.0,
        start_max_workers=1,
        desired_state=DesiredStateStore(desired_path),
    )
    runtime._facade._reply_timeout = 0.05
    spec = HarnessLaunchSpec("fake", "worker", True)
    outcome: list[object] = []

    def restore() -> None:
        try:
            outcome.append(runtime.restore((spec,)))
        except BaseException as error:  # noqa: BLE001 - asserted as receipt below
            outcome.append(error)

    caller = threading.Thread(target=restore)
    restarted: ManagedHarnessRuntime | None = None
    runtime_stopped = False
    try:
        with OwnerContextTracer().installed():
            caller.start()
            assert launcher.entered.wait(1.0)
            caller.join(0.5)
        assert not caller.is_alive(), "harness settlement waited without a bound"
        assert len(outcome) == 1
        error = outcome[0]
        assert isinstance(error, TimeoutError)
        receipt = getattr(error, "receipt", None)
        assert receipt is not None
        assert receipt.operation_id
        assert launcher.calls == 1

        launcher.release.set()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            summary = runtime._facade.recover_restore(receipt)
            if summary is not None:
                break
            time.sleep(0.01)
        assert summary is not None
        assert summary.attempted == summary.restored == 1
        assert summary.failed == summary.deferred == 0
        assert launcher.calls == 1
        runtime.stop(3.0)
        runtime_stopped = True
        restarted_launcher = _BlockedLauncher()
        restarted_launcher.release.set()
        restarted = ManagedHarnessRuntime(
            restarted_launcher,
            start_timeout_seconds=0.0,
            start_max_workers=1,
            desired_state=DesiredStateStore(desired_path),
        )
        recovered = restarted._facade.recover_restore(receipt)
        assert recovered is not None
        assert recovered.restored == 1
        assert restarted_launcher.calls == 0
    finally:
        launcher.release.set()
        caller.join(3.0)
        if not runtime_stopped:
            runtime.stop(3.0)
        if restarted is not None:
            restarted.stop(3.0)
