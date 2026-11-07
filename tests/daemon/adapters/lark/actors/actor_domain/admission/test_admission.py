from __future__ import annotations

import threading
import time
import pytest
from hyprial.kernel import ActorState
from hyprial.daemon.impl.adapters.lark import ActorTarget, HarnessDelivery
from hyprial.daemon.impl.adapters.lark.contracts.errors import AdapterStartError
from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.adapters.lark.ports.ports import AdapterMutationCompleted, PinAdapterCommand, StartAdapterCommand
from hyprial.kernel import PortAdmission, PortCommandRejected
from hyprial.kernel import ChannelConfiguration, LarkGatewayConfig

from ..support import (
    _DesiredState,
    _Events,
    _Launcher,
    _Process,
    _gateway,
)


def test_reload_and_desired_state_survive_actor_generation_restart() -> None:
    launcher = _Launcher()
    runtime = AdapterRuntime(
        launcher,
        ChannelConfiguration(gateways=(_gateway("a"),)),
    )
    try:
        assert runtime.start("a") is True
        runtime.reload_gateways(
            ChannelConfiguration(gateways=(_gateway("a"), _gateway("b")))
        )
        handle = runtime._handle
        assert handle is not None
        assert runtime._runtime.tell(handle, object()).value == "accepted"
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            snapshot = runtime._runtime.snapshot(handle)
            if snapshot.generation > 1 and snapshot.state is ActorState.RUNNING:
                break
            time.sleep(0.01)
        else:
            pytest.fail("adapter actor did not restart")

        assert runtime.gateway_names == ("a", "b")
        (status,) = runtime.status("a")
        assert status["desired"] is True
        assert status["processRunning"] is True
    finally:
        runtime.stop()


def test_reply_precheck_uses_configured_route_without_refresh_or_status_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured routing is stable while the actor owns the final live check."""

    runtime = AdapterRuntime(
        _Launcher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
    )
    try:
        def reject_refresh() -> None:
            raise AssertionError("reply_online must not refresh the adapter actor")

        monkeypatch.setattr(runtime, "_refresh", reject_refresh)
        assert runtime.reply_online("a") is True
        assert runtime.reply_online("missing") is False
        delivery = HarnessDelivery(
            delivery_id="reply-offline",
            message_id="reply-offline",
            reply_to="om-inbound",
            from_actor=ActorTarget("actor", "actor", "actor"),
            text="reply",
        )
        assert runtime.deliver_reply("a", delivery) is False
    finally:
        runtime.stop()


def test_reply_effect_uses_live_process_when_actor_observation_is_stale() -> None:
    class StaleObservationLauncher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            process = super().spawn(gateway)
            process.health = {"streamHealth": "checking"}
            return process

    runtime = AdapterRuntime(
        StaleObservationLauncher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
    )
    try:
        assert runtime.start("a") is True
        (status,) = runtime.status("a")
        assert status["online"] is False
        delivery = HarnessDelivery(
            delivery_id="reply-stale-projection",
            message_id="reply-stale-projection",
            reply_to="om-inbound",
            from_actor=ActorTarget("actor", "actor", "actor"),
            text="reply",
        )
        assert runtime.deliver_reply("a", delivery) is True
    finally:
        runtime.stop()


def test_start_failure_rolls_back_new_desired_intent() -> None:
    desired = _DesiredState()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            raise RuntimeError("injected spawn failure")

    runtime = AdapterRuntime(
        Launcher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
        desired_state=desired,
    )
    try:
        with pytest.raises(AdapterStartError, match="injected spawn failure"):
            runtime.start("a")
        assert desired.active == set()
        assert desired.operations == []
        status = runtime.status("a")[0]
        assert status["desired"] is False
        assert status["processRunning"] is False
    finally:
        runtime.stop()


def test_effect_capacity_is_bounded_and_overload_is_explicit() -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowLauncher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            if gateway.name == "a":
                entered.set()
                assert release.wait(3.0)
            return super().spawn(gateway)

    runtime = AdapterRuntime(
        SlowLauncher(),
        ChannelConfiguration(gateways=(_gateway("a"), _gateway("b"))),
        effect_capacity=1,
    )
    first: list[object] = []
    thread = threading.Thread(target=lambda: first.append(runtime.start("a")))
    thread.start()
    assert entered.wait(1.0)
    try:
        with pytest.raises(AdapterStartError, match="capacity"):
            runtime.start("b")
    finally:
        release.set()
        thread.join(3.0)
        runtime.stop()
    assert first == [True]


def test_concurrent_lifecycle_command_is_rejected_until_start_commits() -> None:
    first_entered = threading.Event()
    release_first = threading.Event()
    processes: list[_Process] = []

    class RacingLauncher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            process = _Process(len(processes) + 1)
            processes.append(process)
            if len(processes) == 1:
                first_entered.set()
                assert release_first.wait(3.0)
            return process

    runtime = AdapterRuntime(
        RacingLauncher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
    )
    first_errors: list[BaseException] = []

    def first_start() -> None:
        try:
            runtime.start("a")
        except BaseException as error:
            first_errors.append(error)

    first = threading.Thread(target=first_start)
    first.start()
    assert first_entered.wait(1.0)
    with pytest.raises(RuntimeError, match="already in progress"):
        runtime.remove("a")
    with pytest.raises(RuntimeError, match="already in progress"):
        runtime.start("a")
    release_first.set()
    first.join(3.0)
    try:
        assert len(processes) == 1
        assert processes[0].stopped is False
        assert runtime._processes["a"] is processes[0]
        assert first_errors == []
        assert runtime.remove("a") is True
        assert runtime.start("a") is True
        assert len(processes) == 2
    finally:
        runtime.stop()


def test_frozen_port_start_is_admission_only_and_completes_as_event() -> None:
    entered = threading.Event()
    release = threading.Event()
    events = _Events()

    class SlowLauncher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            entered.set()
            assert release.wait(3.0)
            return super().spawn(gateway)

    runtime = AdapterRuntime(
        SlowLauncher(),
        ChannelConfiguration(gateways=(_gateway("a"),)),
        event_sink=events,
    )
    try:
        began = time.monotonic()
        admission = runtime.submit(StartAdapterCommand("corr", "a"))
        elapsed = time.monotonic() - began
        assert admission is PortAdmission.ACCEPTED
        assert elapsed < 0.2
        assert entered.wait(1.0)
        assert events.values == []
        release.set()
        assert events.changed.wait(2.0)
        assert isinstance(events.values[0], AdapterMutationCompleted)
        assert events.values[0].changed is True
        projection = runtime.read_adapter("a")
        assert projection is not None and projection.process_running is True
    finally:
        release.set()
        runtime.stop()


def test_pin_port_is_typed_agent_domain_rejection_without_adapter_mutation() -> None:
    launcher = _Launcher()
    events = _Events()
    runtime = AdapterRuntime(
        launcher,
        ChannelConfiguration(gateways=(_gateway("a"),)),
        event_sink=events,
    )
    try:
        before = runtime.read_adapters()
        assert runtime.submit(PinAdapterCommand("pin", "a", "agent:x")) is PortAdmission.ACCEPTED
        assert events.changed.wait(1.0)
        assert isinstance(events.values[0], PortCommandRejected)
        assert events.values[0].code == "PIN_OWNED_BY_AGENT_DOMAIN"
        assert runtime.read_adapters() == before
        assert launcher.calls == 0
    finally:
        runtime.stop()


def test_public_status_is_clean_after_a_successful_retry() -> None:
    """A retry that worked must not keep looking like a failure.

    The actor is already stopped, so nothing runs a completion to clear the
    debt or republish. Emptying the registry while status() went on reporting
    the old PID running and custody unsettled is a success wearing a failure's
    clothes -- and permanently, since nothing would ever revise it.
    """

    stop_allowed = threading.Event()

    class RefusingProcess(_Process):
        def stop(self, timeout: float = 5.0) -> None:
            del timeout
            if not stop_allowed.is_set():
                raise RuntimeError("shutdown stop failed")
            super().stop()

    class Launcher(_Launcher):
        def spawn(self, gateway: LarkGatewayConfig) -> _Process:
            del gateway
            return RefusingProcess(84)

    runtime = AdapterRuntime(
        Launcher(), ChannelConfiguration(gateways=(_gateway("a"),))
    )
    assert runtime.start("a") is True

    with pytest.raises(RuntimeError, match="still holds a"):
        runtime.stop(timeout=0.5)
    [first] = runtime.status("a")
    assert first["processRunning"] is True
    assert first["unsettled"] == "WORKER_STOP_FAILED"

    stop_allowed.set()
    assert runtime.stop(timeout=2.0) is True

    [second] = runtime.status("a")
    assert second["processRunning"] is False, "the worker really did stop"
    assert "unsettled" not in second, "settled custody must clear the debt"
    assert "error" not in second
    assert "pid" not in second, "no stale PID may survive the stop"
