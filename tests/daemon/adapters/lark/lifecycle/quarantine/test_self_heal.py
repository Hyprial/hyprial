"""Quarantine recovery uses fake workers and a driven clock, never Lark."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Event

import pytest

from hyprial.daemon.impl.adapters.lark.runtime.handler import _AdapterHandler
from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.kernel import ChannelConfiguration, LarkGatewayConfig


@dataclass
class Process:
    pid: int
    readiness: str = "online"
    running: bool = True
    error: str | None = None
    health: dict = field(default_factory=dict)
    last_sdk_output: str | None = None

    def wait_ready(self, timeout=None):
        return self.readiness

    def stop(self, timeout=5):
        self.running = False

    def drain_health_events(self):
        return ()


class Launcher:
    startup_timeout = 1

    def __init__(self):
        self.calls = 0
        self.outage = False
        self.processes = []

    def spawn(self, gateway):
        self.calls += 1
        if self.outage:
            raise RuntimeError("synthetic outage")
        process = Process(self.calls)
        self.processes.append(process)
        return process


@pytest.fixture
def system(monkeypatch):
    monkeypatch.setattr(
        "random.uniform",
        lambda low, high: (low + high) / 2,
    )
    now = [1000.0]
    launcher = Launcher()
    gateways = tuple(LarkGatewayConfig(
        name=name, app_id=f"app-{name}", credential_ref=name, routes=(),
    ) for name in ("one", "two", "three"))
    runtime = AdapterRuntime(
        launcher, ChannelConfiguration(gateways=gateways),
        retry_interval=0, clock=lambda: now[0],
    )
    try:
        yield runtime, launcher, now
    finally:
        runtime.stop()


def quarantine(runtime, launcher, names=("one",)):
    for name in names:
        assert runtime.start(name)
    launcher.outage = True
    for process in launcher.processes:
        process.running = False
    # A dead worker plus two failed replacement attempts exhaust the fast budget.
    assert runtime.reconcile() == 0
    assert runtime.reconcile() == 0
    for name in names:
        assert runtime.status(name)[0]["status"] == "quarantined"


def test_quarantine_retries_then_online_clears_all_failure_state(system):
    runtime, launcher, now = system
    quarantine(runtime, launcher)
    row = runtime.status("one")[0]
    assert 30 <= row["nextRetryAt"] - now[0] <= 36
    assert row["retryAttempt"] == 1
    payload = runtime.read_adapter("one").to_payload()
    assert payload["nextRetryAt"] == row["nextRetryAt"]
    assert payload["retryAttempt"] == 1
    calls = launcher.calls
    for _ in range(10):
        assert runtime.reconcile() == 0
    assert launcher.calls == calls
    now[0] = row["nextRetryAt"] - 0.001
    assert runtime.reconcile() == 0
    now[0] += 0.001
    launcher.outage = False
    assert runtime.reconcile() == 1
    row = runtime.status("one")[0]
    assert row["status"] == "online"
    assert "nextRetryAt" not in row and "retryAttempt" not in row
    authority = runtime._authority
    assert not authority.quarantined
    assert not authority.failures
    assert not authority.failed_attempts
    assert not authority.retry_after
    events = [event["event"] for event in runtime.drain_health_events()]
    assert events == ["lark.adapter.quarantined", "lark.adapter.recovered"]
    assert runtime.drain_health_events() == ()


def test_persistent_failure_grows_and_caps_without_probe_rescheduling(system):
    runtime, launcher, now = system
    quarantine(runtime, launcher)
    for attempt in range(1, 10):
        row = runtime.status("one")[0]
        delay = row["nextRetryAt"] - now[0]
        base = min(600, 30 * 2 ** (attempt - 1))
        assert base <= delay <= min(600, base * 1.2)
        assert row["retryAttempt"] == attempt
        calls = launcher.calls
        for _ in range(5):
            assert runtime.reconcile() == 0
        assert launcher.calls == calls
        assert runtime.status("one")[0]["nextRetryAt"] == row["nextRetryAt"]
        now[0] = row["nextRetryAt"]
        assert runtime.reconcile() == 0
        assert launcher.calls == calls + 1
    assert [event["event"] for event in runtime.drain_health_events()] == [
        "lark.adapter.quarantined",
    ]


@pytest.mark.parametrize("stop", [True, False])
def test_undesired_adapter_never_retries(system, stop):
    runtime, launcher, now = system
    quarantine(runtime, launcher)
    if stop:
        assert runtime.remove("one")
    else:
        runtime._authority.desired.discard("one")
    calls = launcher.calls
    now[0] += 10000
    launcher.outage = False
    assert runtime.reconcile() == 0
    assert launcher.calls == calls
    row = runtime.status("one")[0]
    assert row["desired"] is False
    assert "nextRetryAt" not in row and "retryAttempt" not in row


def test_explicit_start_bypasses_quarantine_delay(system):
    runtime, launcher, now = system
    quarantine(runtime, launcher)
    launcher.outage = False
    assert runtime.start("one")
    assert runtime.status("one")[0]["status"] == "online"
    assert not runtime._authority.retry_after


def test_stop_cancels_an_inflight_quarantine_retry(system):
    runtime, launcher, now = system
    quarantine(runtime, launcher)
    now[0] = runtime.status("one")[0]["nextRetryAt"]
    launcher.outage = False
    entered, release = Event(), Event()
    spawn = launcher.spawn

    def held_spawn(gateway):
        entered.set()
        assert release.wait(5)
        return spawn(gateway)

    launcher.spawn = held_spawn
    with ThreadPoolExecutor(max_workers=1) as pool:
        retry = pool.submit(runtime.reconcile)
        try:
            assert entered.wait(5)
            assert runtime.remove("one")
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="superseded"):
            retry.result(timeout=5)
    calls = launcher.calls
    now[0] += 10000
    assert runtime.reconcile() == 0
    assert launcher.calls == calls
    assert not launcher.processes[-1].running
    assert runtime.status("one")[0]["desired"] is False


@pytest.mark.parametrize("names", [("one", "two"), ("one", "two", "three")])
@pytest.mark.parametrize("reverse", [False, True])
def test_each_retry_remains_stoppable_after_batch_is_superseded(system, monkeypatch, names, reverse):
    runtime, launcher, now = system
    quarantine(runtime, launcher, names)
    now[0] = max(row["nextRetryAt"] for row in runtime.status() if row["desired"])
    launcher.outage = False
    entered = {name: Event() for name in names}
    release = {name: Event() for name in names}
    completed = {name: Event() for name in names}
    spawn = launcher.spawn
    complete = _AdapterHandler._complete

    def held_spawn(gateway):
        entered[gateway.name].set()
        assert release[gateway.name].wait(10)
        return spawn(gateway)

    def track_completion(handler, result):
        complete(handler, result)
        if result.operation == "spawn" and result.name in completed:
            completed[result.name].set()

    monkeypatch.setattr(launcher, "spawn", held_spawn)
    monkeypatch.setattr(_AdapterHandler, "_complete", track_completion)
    order = tuple(reversed(names)) if reverse else names
    with ThreadPoolExecutor(max_workers=1) as pool:
        retry = pool.submit(runtime.reconcile)
        try:
            assert all(event.wait(5) for event in entered.values())
            for index, name in enumerate(order):
                # Explicit starts must still respect each pending retry's fence.
                with pytest.raises(RuntimeError, match="already in progress"):
                    runtime.start(name)
                assert runtime.remove(name)
                assert runtime.status(name)[0]["desired"] is False
                release[name].set()
                assert completed[name].wait(5)
                if index == 0:
                    with pytest.raises(RuntimeError, match="superseded"):
                        retry.result(timeout=5)
                    # The caller has failed, but other members still own transitions.
                    assert not runtime._authority.aggregates
        finally:
            for event in release.values():
                event.set()
    calls = launcher.calls
    now[0] += 10000
    assert runtime.reconcile() == 0
    assert launcher.calls == calls
    assert not any(process.running for process in launcher.processes)
    assert not runtime._authority.transitions
    assert not runtime._authority.background_spawns
    assert not runtime._authority.transition_since
    assert not runtime._authority.desired


def test_all_adapters_recover_after_shared_outage(system):
    runtime, launcher, now = system
    names = ("one", "two", "three")
    quarantine(runtime, launcher, names)
    now[0] = max(row["nextRetryAt"] for row in runtime.status())
    launcher.outage = False
    assert runtime.reconcile() == 3
    assert all(row["status"] == "online" for row in runtime.status())
    events = runtime.drain_health_events()
    assert sum(e["event"] == "lark.adapter.quarantined" for e in events) == 3
    assert sum(e["event"] == "lark.adapter.recovered" for e in events) == 3


def test_failure_window_uses_injected_clock(system):
    runtime, launcher, now = system
    assert runtime.start("one")
    launcher.outage = True
    launcher.processes[0].running = False
    assert runtime.reconcile() == 0
    now[0] += 121
    assert runtime.reconcile() == 0
    assert runtime.status("one")[0]["status"] != "quarantined"


def test_starting_retry_stays_quarantined_until_healthy_online(system):
    runtime, launcher, now = system
    quarantine(runtime, launcher)
    now[0] = runtime.status("one")[0]["nextRetryAt"]
    launcher.outage = False
    spawn = launcher.spawn

    def starting(gateway):
        process = spawn(gateway)
        process.readiness = "starting"
        return process

    launcher.spawn = starting
    assert runtime.reconcile() == 1
    row = runtime.status("one")[0]
    assert row["status"] == "quarantined"
    assert "nextRetryAt" not in row
    assert row["retryAttempt"] == 1
    process = launcher.processes[-1]
    process.readiness = "online"
    process.health = {"streamHealth": "checking"}
    assert runtime.status("one")[0]["status"] == "quarantined"
    process.health = {"streamHealth": "healthy"}
    assert runtime.status("one")[0]["status"] == "online"
    assert runtime.read_adapter("one").next_retry_at is None
