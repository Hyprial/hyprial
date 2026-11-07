"""Lark adapter lifecycle guards (card fd73140a; spec v2 2026-09-04 §2/§3).

G0  a completion that settles as failure (stale generation or version)
    still releases the lifecycle transition its correlation owns -- the
    production wedge held ``_transitions['infra-op-feishu']`` for >20 min
    (daemon.jsonl 09:21Z-09:43Z, pid 47800) because the stale early return
    failed custody without popping.
G2  one transition's write-to-pop age is bounded (measured N); on expiry a
    fail-loud ``lark.adapter.lifecycle.timeout`` event fires (name,
    correlation_id, age, last lark.sdk.output) and the lock is released.
G3  ``adapter status`` shows the locking correlation_id and its age;
    ``doctor`` diagnoses age > N and points at daemon.jsonl.

T0/T2/T3/T4/T5 map 1:1 to spec v2 §3.  The stale-completion manufacture is
the production mechanism: a reconcile tick's spawn phase walks every desired
name and bumps the version under an in-flight lifecycle command, so that
command's own completion arrives version-stale and takes the early return.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark.contracts.errors import AdapterStartError
from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.adapters.lark.contracts.lifecycle import START_DEADLINE_SECONDS
from hyprial.kernel import ChannelConfiguration, LarkGatewayConfig


# --------------------------------------------------------------------------- #
# Canonical stubs (same forms as tests/test_lark_actor_domain.py), extended
# with the last_sdk_output field the worker protocol grows for G2's event.
# --------------------------------------------------------------------------- #


def _gateway(name: str) -> LarkGatewayConfig:
    return LarkGatewayConfig(
        name=name,
        app_id=f"app-{name}",
        credential_ref=f"lark-{name}",
        routes=(),
    )


@dataclass
class _Process:
    pid: int
    readiness: str = "online"
    running: bool = True
    error: str | None = None
    health: dict[str, object] = field(default_factory=dict)
    stopped: bool = False
    last_sdk_output: str | None = None

    def wait_ready(self, timeout: float | None = None) -> str:
        del timeout
        return self.readiness

    def drain_health_events(self) -> tuple[dict[str, object], ...]:
        return ()

    def deliver_reply(self, delivery: object) -> bool:
        del delivery
        return True

    def deliver_alarm(
        self, correlation_id: str, text: str, *, idempotency_key: str
    ) -> bool:
        del correlation_id, text, idempotency_key
        return True

    def stop(self, timeout: float = 5.0) -> None:
        del timeout
        self.running = False
        self.stopped = True


class _Launcher:
    """Online-immediately stub launcher; counts spawns per adapter."""

    startup_timeout = 1.0

    def __init__(self, *, readiness: str = "online") -> None:
        self.calls = 0
        self.per_name: dict[str, int] = {}
        self.processes: list[_Process] = []
        self._readiness = readiness

    def spawn(self, gateway: LarkGatewayConfig) -> _Process:
        self.calls += 1
        self.per_name[gateway.name] = self.per_name.get(gateway.name, 0) + 1
        process = _Process(
            self.calls,
            readiness=self._readiness,
            last_sdk_output=f"sdk output line {self.calls}",
        )
        self.processes.append(process)
        return process


def _wait_status(
    runtime: AdapterRuntime, name: str, *, timeout: float = 5.0, **expect: Any
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        rows = runtime.status(name)
        if rows:
            last = rows[0]
            if all(last.get(key) is value for key, value in expect.items()):
                return last
        time.sleep(0.02)
    raise AssertionError(f"status for {name} never reached {expect}: {last}")


def _drain_events(runtime: AdapterRuntime) -> tuple[dict[str, object], ...]:
    try:
        return runtime.drain_health_events()
    except RuntimeError:
        # Draining races lifecycle churn: a probe that loses a version bump
        # fails its refresh aggregate (pre-existing behaviour).
        return ()


class _SpawnGatedLauncher(_Launcher):
    """Blocks the victim's spawns (from ``gate_from`` on) until released.

    This is the manufacture for a lifecycle command whose completion is
    late (T2) or version-stale (T0/T5): while the spawn sits gated, the
    reconcile tick's spawn phase bumps the victim's version.
    """

    def __init__(
        self, victim: str, *, readiness: str = "online", gate_from: int = 1
    ) -> None:
        super().__init__(readiness=readiness)
        self._victim = victim
        self._gate_from = gate_from
        self._victim_spawns = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def spawn(self, gateway: LarkGatewayConfig) -> _Process:
        process = super().spawn(gateway)
        if gateway.name == self._victim:
            self._victim_spawns += 1
            if self._victim_spawns >= self._gate_from:
                self.entered.set()
                assert self.release.wait(10.0)
        return process


def _start_tick(runtime: AdapterRuntime) -> threading.Thread:
    """Run one reconcile tick on a thread (its replacement spawn may gate)."""

    def _run() -> None:
        try:
            runtime.reconcile()
        except RuntimeError:
            pass  # a probe losing the version race fails its aggregate (pre-existing)

    thread = threading.Thread(target=_run)
    thread.start()
    return thread


# --------------------------------------------------------------------------- #
# T0 (G0): a stale completion releases the transition it owns.
# --------------------------------------------------------------------------- #


def test_t0_superseded_completion_leaves_nothing_held() -> None:
    """G0/G2 chain over an aged transition (review round 2 shape).

    The age gate keeps a YOUNG in-flight start safe from the tick, so the
    stale-completion early return is now manufactured on an AGED transition:
    the tick's expiry releases the lock and fails the caller, the spawn phase
    (now allowed) supersedes the original spawn, and its stale completion
    must leave nothing held -- stop and reload keep working.  The literal
    pre-fix readings (gate+pop removed: "already in progress" / "in progress
    during reload") are recorded in the PR body via the double mutation.
    """

    victim = "x"
    launcher = _SpawnGatedLauncher(victim)
    runtime = AdapterRuntime(
        launcher,
        ChannelConfiguration(gateways=(_gateway("y"), _gateway(victim))),
        start_deadline_seconds=0.2,
    )
    try:
        assert runtime.start("y") is True
        _wait_status(runtime, "y", online=True)

        started: list[object] = []
        start_errors: list[BaseException] = []

        def _start_victim() -> None:
            try:
                started.append(runtime.start(victim))
            except BaseException as error:  # noqa: BLE001 - recorded, asserted
                start_errors.append(error)

        starter = threading.Thread(target=_start_victim)
        starter.start()
        assert launcher.entered.wait(5.0)

        # Age past the deadline, then tick: expiry releases + fails the
        # caller, the spawn phase queues the replacement (bump), the original
        # spawn's completion arrives version-stale on the early return.
        time.sleep(0.25)
        tick = _start_tick(runtime)
        deadline = time.monotonic() + 5.0
        while launcher.per_name.get(victim, 0) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert launcher.per_name[victim] == 2, launcher.per_name
        launcher.release.set()
        starter.join(10.0)
        tick.join(10.0)
        deadline = time.monotonic() + 10.0
        while runtime._effects.pending() and time.monotonic() < deadline:
            time.sleep(0.05)

        assert started == []
        assert len(start_errors) == 1
        assert isinstance(start_errors[0], AdapterStartError)
        assert start_errors[0].code == "ADAPTER_START_TIMEOUT"  # type: ignore[attr-defined]
        # Nothing is held afterwards: stop and reload keep working.
        assert runtime.remove(victim) is True
        assert runtime.reload_gateways(
            ChannelConfiguration(gateways=(_gateway("y"), _gateway(victim)))
        ) is not None
    finally:
        launcher.release.set()
        runtime.stop()


# --------------------------------------------------------------------------- #
# T2 (G2): a transition whose completion never arrives times out at N.
# --------------------------------------------------------------------------- #


def test_t2_unfinished_transition_times_out_fails_loud_and_releases() -> None:
    victim = "x"
    launcher = _SpawnGatedLauncher(victim)
    runtime = AdapterRuntime(
        launcher,
        ChannelConfiguration(gateways=(_gateway(victim),)),
        start_deadline_seconds=0.2,
    )
    try:
        started: list[object] = []
        start_errors: list[BaseException] = []

        def _start_victim() -> None:
            try:
                started.append(runtime.start(victim))
            except BaseException as error:  # noqa: BLE001 - recorded, asserted
                start_errors.append(error)

        starter = threading.Thread(target=_start_victim)
        starter.start()
        assert launcher.entered.wait(5.0)

        # The lock is held and visible (G3) while the completion is missing.
        row = _wait_status(runtime, victim, desired=True)
        correlation = str(row.get("lifecycleCorrelationId", ""))
        assert correlation, row  # the facade mints a bare uuid; the port prefixes lark:start:
        assert isinstance(row.get("lifecycleAgeSeconds"), int)

        time.sleep(0.25)
        reconcile_thread = threading.Thread(target=lambda: runtime.reconcile())
        reconcile_thread.start()
        deadline = time.monotonic() + 5.0
        events: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            events = [
                e
                for e in _drain_events(runtime)
                if e.get("event") == "lark.adapter.lifecycle.timeout"
            ]
            if events:
                break
            time.sleep(0.02)
        timeouts = [
            e for e in events if e.get("event") == "lark.adapter.lifecycle.timeout"
        ]
        assert timeouts, f"expected a lifecycle timeout event, got {events}"
        event = timeouts[0]
        assert event["adapter"] == victim
        assert str(event["correlationId"]) == correlation
        assert float(event["ageSeconds"]) >= 0.2
        assert float(event["timeoutSeconds"]) == 0.2

        # Released: the pending start failed with the timeout code and the
        # next lifecycle command works instead of "already in progress".
        launcher.release.set()
        starter.join(10.0)
        reconcile_thread.join(10.0)
        assert started == []
        assert len(start_errors) == 1
        assert isinstance(start_errors[0], AdapterStartError)
        assert start_errors[0].code == "ADAPTER_START_TIMEOUT"  # type: ignore[attr-defined]
        # The same tick queued the backoff restart, so the stop value itself
        # may race a fresh registration; the wedge would raise.
        assert isinstance(runtime.remove(victim), bool)
    finally:
        launcher.release.set()
        runtime.stop()


# --------------------------------------------------------------------------- #
# T3 (G2): a completion within N leaves no timeout event; the shipped N
# stays above the measured worst case (no baseless constants).
# --------------------------------------------------------------------------- #


def test_t3_completion_within_deadline_has_no_timeout_event() -> None:
    victim = "x"
    launcher = _SpawnGatedLauncher(victim)
    runtime = AdapterRuntime(
        launcher,
        ChannelConfiguration(gateways=(_gateway(victim),)),
        start_deadline_seconds=5.0,
    )
    try:
        started: list[object] = []
        starter = threading.Thread(target=lambda: started.append(runtime.start(victim)))
        starter.start()
        assert launcher.entered.wait(5.0)
        launcher.release.set()
        starter.join(10.0)
        assert started == [True]
        _wait_status(runtime, victim, online=True)

        runtime.reconcile()
        events = _drain_events(runtime)
        assert not [
            e for e in events if e.get("event") == "lark.adapter.lifecycle.timeout"
        ], events
    finally:
        launcher.release.set()
        runtime.stop()


def test_t3_shipped_deadline_stays_above_the_measured_worst_case() -> None:
    """Spec v2 §2 + review: N bounds ONE transition's write-to-pop age, so
    it is drawn from the measured SINGLE-episode distribution (2026-09-04
    15:19 restart: single adapter ready usually <60s).  The fleet figures
    (2.3-10 min) are the multi-episode path to online and are deliberately
    NOT the basis -- see the lifecycle.py docstring.  The shipped default
    must stay >= the measured single-episode ceiling (60s); anything
    smaller is an unexplained constant and turns this red."""

    assert START_DEADLINE_SECONDS >= 60.0


# --------------------------------------------------------------------------- #
# T4 (G3): status/doctor carry the locking correlation_id and age.
# --------------------------------------------------------------------------- #


def test_t4_status_carries_locking_correlation_and_age() -> None:
    victim = "x"
    launcher = _SpawnGatedLauncher(victim)
    runtime = AdapterRuntime(
        launcher,
        ChannelConfiguration(gateways=(_gateway(victim),)),
        start_deadline_seconds=60.0,
    )
    starter = threading.Thread(target=lambda: runtime.start(victim))
    try:
        starter.start()
        assert launcher.entered.wait(5.0)

        row = _wait_status(runtime, victim, desired=True)
        correlation = str(row.get("lifecycleCorrelationId", ""))
        assert correlation, row
        assert isinstance(row.get("lifecycleAgeSeconds"), int)
        assert row["lifecycleAgeSeconds"] >= 0

        projection = runtime.read_adapter(victim)
        assert projection is not None
        payload = projection.to_payload()
        assert str(payload["lifecycleCorrelationId"]) == correlation
        assert payload["lifecycleAgeSeconds"] >= 0
    finally:
        launcher.release.set()
        starter.join(10.0)
        runtime.stop()


def test_t4_doctor_diagnoses_an_over_deadline_lock() -> None:
    from hyprial.shell.impl.cli.commands.doctor.commands import _lark_inbound_doctor_check

    def ps_payload(age: int) -> dict[str, object]:
        return {
            "adapters": [
                {
                    "provider": "lark",
                    "name": "slow-gw",
                    "desired": True,
                    "online": False,
                    "status": "starting",
                    "lifecycleCorrelationId": "lark:start:abc123",
                    "lifecycleAgeSeconds": age,
                }
            ]
        }

    diagnosed = _lark_inbound_doctor_check(ps_payload(1200))
    assert diagnosed is not None
    blob = repr(diagnosed)
    assert "lark:start:abc123" in blob
    assert "daemon.jsonl" in blob

    # Below the deadline the generic starting note carries no lock diagnosis.
    quiet = _lark_inbound_doctor_check(ps_payload(30))
    assert quiet is None or "daemon.jsonl" not in repr(quiet)


# --------------------------------------------------------------------------- #
# T5: §1's sequence (online -> start refused at the transitions guard ->
# one manufactured early-return completion).  Pre-fix reload answers
# "in progress"; post-fix the stale completion released the lock.
# --------------------------------------------------------------------------- #


def test_t5_today_sequence_stale_completion_leaves_reload_usable() -> None:
    """§1's sequence against the gated world (review round 2).

    上线 -> a second start is refused while a lifecycle command holds the
    lock (:1147 semantics) -> one manufactured early-return completion ->
    reload stays usable.  Pre-fix readings on the untouched base tree
    (recorded in the PR body): the refusal plus "adapter lifecycle is in
    progress during reload" for 22 minutes.  With the age gate the tick
    manufactures the supersede only after the deadline (G2 has released by
    then), which is exactly the aged shape used here.
    """

    victim = "x"
    launcher = _SpawnGatedLauncher(victim, gate_from=2)
    channels = ChannelConfiguration(gateways=(_gateway("y"), _gateway(victim)))
    runtime = AdapterRuntime(launcher, channels, start_deadline_seconds=0.2)
    try:
        # The healthy peer is desired so the tick's probe phase has work.
        assert runtime.start("y") is True
        _wait_status(runtime, "y", online=True)
        # 上线: the adapter is online and desired.
        assert runtime.start(victim) is True
        _wait_status(runtime, victim, online=True)

        # The worker later reads dead, so a re-start is admitted and held.
        runtime._processes[victim].running = False  # noqa: SLF001
        runtime.status(victim)
        _wait_status(runtime, victim, processRunning=False)

        started: list[object] = []
        start_errors: list[BaseException] = []

        def _start_victim() -> None:
            try:
                started.append(runtime.start(victim))
            except BaseException as error:  # noqa: BLE001 - recorded, asserted
                start_errors.append(error)

        starter = threading.Thread(target=_start_victim)
        starter.start()
        assert launcher.entered.wait(5.0)

        # 再 start: refused by the transitions guard while the lock is held.
        with pytest.raises(RuntimeError, match="already in progress"):
            runtime.start(victim)

        # Age past the deadline, then the tick: expiry releases + fails the
        # holder, the spawn phase supersedes the held spawn, and its stale
        # completion takes the early return.
        time.sleep(0.25)
        tick = _start_tick(runtime)
        deadline = time.monotonic() + 5.0
        while launcher.per_name.get(victim, 0) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert launcher.per_name[victim] == 3, launcher.per_name
        launcher.release.set()
        starter.join(10.0)
        tick.join(10.0)
        deadline = time.monotonic() + 10.0
        while runtime._effects.pending() and time.monotonic() < deadline:
            time.sleep(0.05)

        assert started == []
        assert len(start_errors) == 1
        # The lifecycle stayed usable throughout: reload and stop work.
        assert runtime.reload_gateways(channels) is not None
        assert runtime.remove(victim) is True
    finally:
        launcher.release.set()
        runtime.stop()


# --------------------------------------------------------------------------- #
# t5b (review round 2, BLOCK ①): the reconcile spawn phase must not supersede
# a YOUNG in-flight start.  Pre-gate behaviour (and v1's literal production
# mechanism): the tick queued a replacement spawn under the held start,
# bumping the version and killing the user's worker via previous.stop()
# (daemon.jsonl 09:21:04.089Z, pid 43014 rc=-15).
# --------------------------------------------------------------------------- #


def test_t5b_reconcile_spawn_phase_respects_a_young_in_flight_start() -> None:
    victim = "x"
    launcher = _SpawnGatedLauncher(victim)
    channels = ChannelConfiguration(gateways=(_gateway("y"), _gateway(victim)))
    runtime = AdapterRuntime(launcher, channels, start_deadline_seconds=60.0)
    try:
        assert runtime.start("y") is True
        _wait_status(runtime, "y", online=True)

        started: list[object] = []
        starter = threading.Thread(target=lambda: started.append(runtime.start(victim)))
        starter.start()
        assert launcher.entered.wait(5.0)

        # One tick while the start's transition is young: the probe phase
        # covers the healthy peer and the spawn phase walks every desired
        # name -- the victim must count as in progress and be skipped.
        tick = _start_tick(runtime)
        deadline = time.monotonic() + 5.0
        while (
            tick.is_alive()
            and launcher.per_name.get(victim, 0) < 2
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        # Exactly one spawn for the victim: no replacement was queued, so no
        # version bump and no previous.stop() on the user's worker.
        assert launcher.per_name[victim] == 1, launcher.per_name
        assert launcher.processes[0].stopped is False

        # The in-flight start concludes normally once released.
        launcher.release.set()
        starter.join(10.0)
        tick.join(10.0)
        assert started == [True]
        _wait_status(runtime, victim, online=True)
        assert runtime.reload_gateways(channels) is not None
        assert runtime.remove(victim) is True
    finally:
        launcher.release.set()
        runtime.stop()


# --------------------------------------------------------------------------- #
# Round 4 (window): the spawn-phase gate must not carry its own clock.  The
# tick-start expiry owns the age arithmetic (it releases every over-age
# transition before the probes run), so by the time the spawn phase walks
# the desired names, anything still held is in flight by construction.
# A gate that re-derives age with its own now₂ opens a window the width of
# the probe phase (and an operator mismatch at age == deadline): the expiry
# kept the transition (age <= deadline at now₁) while the gate released it
# (age >= deadline at now₂) -- supersede without the G2 fail-loud promise.
# --------------------------------------------------------------------------- #


def test_round4_spawn_gate_holds_even_for_an_overdue_looking_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate keys on possession only; expiry owns the age arithmetic.

    Manufacturing: hold a real start transition (gated spawn), then forge
    its age past the deadline and run the spawn phase WITHOUT the expiry
    (monkeypatched out) -- exactly the window where a clocked gate would
    release.  Reading sequence pinned by review round 4: red on the clocked
    gate (a replacement spawn appears), green on the possession gate, red
    again with the gate removed.
    """

    from hyprial.daemon.impl.adapters.lark.runtime.handler import _AdapterHandler

    victim = "x"
    launcher = _SpawnGatedLauncher(victim)
    channels = ChannelConfiguration(gateways=(_gateway("y"), _gateway(victim)))
    runtime = AdapterRuntime(launcher, channels, start_deadline_seconds=60.0)
    try:
        assert runtime.start("y") is True
        _wait_status(runtime, "y", online=True)

        started: list[object] = []
        starter = threading.Thread(target=lambda: started.append(runtime.start(victim)))
        starter.start()
        assert launcher.entered.wait(5.0)

        # Forge the transition's age past the deadline (deadline + epsilon),
        # then run the tick with the expiry disabled: only the probe phase
        # and the spawn phase execute.
        forged = time.monotonic() - (60.0 + 0.5)
        runtime._authority.transition_since[victim] = forged  # noqa: SLF001
        monkeypatch.setattr(
            _AdapterHandler, "_expire_transition_deadlines", lambda self: None
        )
        try:
            tick = _start_tick(runtime)
            deadline = time.monotonic() + 5.0
            while (
                tick.is_alive()
                and launcher.per_name.get(victim, 0) < 2
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            tick.join(10.0)

            # Possession alone must hold: no replacement spawn, no stop of
            # the user's worker, the transition still owned by its start.
            assert launcher.per_name[victim] == 1, launcher.per_name
            assert launcher.processes[0].stopped is False
            assert victim in runtime._authority.transitions  # noqa: SLF001
        finally:
            monkeypatch.undo()

        # The start still concludes normally once released.
        launcher.release.set()
        starter.join(10.0)
        assert started == [True]
    finally:
        launcher.release.set()
        runtime.stop()
