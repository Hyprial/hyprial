"""Real daemon/Session/Agent contracts for a Codex carrier with a fake clock."""
from __future__ import annotations

from collections import Counter
import threading
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.state.liveness import AGENT_HEARTBEAT_TTL_SECONDS
from hyprial.kernel.impl.contracts.channels.channel import CHANNEL_HEARTBEAT_INTERVAL_SECONDS
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError, _LocalPresence
from hyprial.daemon.impl.harnesses.codex import CodexInteractiveCarrier
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.shell.impl.daemon_wiring import daemon_dependencies

ACTOR = "agent:probe:isolated:codex-probe"
REF = "isolated-codex-thread"


class _Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class _Directory:
    def actor_online(self, actor):
        return actor == ACTOR

    def online_actors(self):
        return (ACTOR,)

    def online_mailboxes(self):
        return ()


class _NoWorkers:
    def status(self):
        return ()


class _FakeServer:
    # Only a fake liveness value; never start, inspect, or signal a process.
    pid = 424242

    def start_turn(self, _prompt):
        raise AssertionError("an empty-inbox liveness test must not run an AI turn")


class _Rig:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.clock = _Clock()
        self.calls = []
        self.errors = []
        self.error_calls = []
        self.carrier = None
        self.app = self._application()
        self.register(REF)
        self.carrier = CodexInteractiveCarrier(
            _FakeServer(), actor=ACTOR, session_ref=REF, cwd=root,
            command=["codex", "--remote", "fake-only"],
            daemon_request=self.request, state_path=root / "carrier.sqlite3",
            clock=self.clock,
        )

    def _application(self):
        app = DaemonApplication(
            state_dir=self.home / "state", socket_path=self.home / "state/daemon.sock",
            node_id="isolated", owner="probe", hyprial_home=self.home,
            zenoh_listen=(), zenoh_connect=(),
            **daemon_dependencies(),
        )
        app._clock = self.clock
        app._inbox = InboxService(self.home / "state/inbox.sqlite3", MemoryDeliveryTransport())
        app._harnesses = _NoWorkers()
        app._presence = _LocalPresence(
            _Directory(), app.node_id, interactive_liveness=app._interactive_actor_liveness,
        )
        return app

    def register(self, session_ref):
        return self.app.handle("session.register", {
            "actor": ACTOR, "sessionRef": session_ref, "cwd": str(self.root),
            "command": ["codex", "--remote", "fake-only"],
            "source": "codex-app-server", "runtime": "codex_interactive",
        })

    def request(self, method, params):
        self.calls.append((self.clock.now, method, dict(params)))
        if method == "message.pending.wait":
            # Liveness tests isolate the polling fallback; the dedicated
            # inbox-watch tests exercise a doorbell-capable daemon.
            raise RuntimeError("METHOD_NOT_FOUND: isolated liveness daemon")
        try:
            result = self.app.handle(method, params)
        except DaemonRequestError as error:
            self.errors.append(error.code)
            self.error_calls.append((method, error.code))
            # Match the production daemon-request transport's code-prefixed error.
            raise RuntimeError(f"{error.code}: {error}") from error
        if method == "message.pending.list":
            assert result["messages"] == []
        return result

    def methods(self):
        return [method for _time, method, _params in self.calls]

    def assert_online(self):
        runtime = self.app._agent_session_domains.agent.read_runtime(ACTOR)
        assert runtime.online is True, runtime.to_payload()
        assert self.app._presence.actor_online(ACTOR)
        ps = self.app.handle("ps", {})
        row = next(item for item in ps["interactiveSessions"] if item["actor"] == ACTOR)
        assert row["online"] is True

    def restart_daemon(self):
        epoch = self.app.epoch
        self.app._close()
        self.app._close_home_lease()
        self.app = self._application()
        assert self.app.epoch != epoch

    def stop_carrier(self):
        if self.carrier is None:
            return
        # Tests invoke the real _poll_once synchronously. Start the stopped
        # outer loop only so public stop() can join it and drain real actors.
        self.carrier._stop.set()
        self.carrier.start()
        self.carrier.stop()

    def close(self):
        try:
            self.stop_carrier()
        finally:
            self.app._close()
            self.app._close_home_lease()


@pytest.fixture
def rig(tmp_path):
    value = _Rig(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_codex_frequent_successful_poll_stays_online_across_ttl(rig):
    rig.carrier._poll_once()
    rig.assert_online()
    # Drive the loop at 20 Hz over 26 fake seconds.  Once the legacy-daemon
    # wait probe is refused, the shared core paces lists at the 0.5s fallback.
    for tick in range(1, 521):
        rig.clock.now = 1000.0 + tick / 20
        rig.carrier._poll_once()
        if tick in (240, 250, 520):
            rig.assert_online()  # RED on the original carrier at t=12.5s.
    assert rig.clock.now - 1000 > AGENT_HEARTBEAT_TTL_SECONDS
    counts = Counter(rig.methods())
    assert 50 <= counts["message.pending.list"] <= 53
    assert counts["message.pending.wait"] == 1
    assert counts["session.refresh"] == 1
    assert 1 <= counts["session.heartbeat"] <= 26
    assert counts["session.register"] == 0
    heartbeats = [time for time, method, _params in rig.calls if method == "session.heartbeat"]
    assert all(later - earlier >= CHANNEL_HEARTBEAT_INTERVAL_SECONDS
               for earlier, later in zip(heartbeats, heartbeats[1:]))
    assert rig.errors == []


def test_refresh_schedules_first_heartbeat_one_interval_after_success(rig):
    rig.carrier._poll_once()
    assert rig.methods() == ["session.refresh", "message.pending.list"]
    rig.clock.now += CHANNEL_HEARTBEAT_INTERVAL_SECONDS - .001
    rig.carrier._poll_once()
    assert rig.methods()[-1:] == ["message.pending.wait"]
    assert "session.heartbeat" not in rig.methods()
    rig.clock.now = 1000 + CHANNEL_HEARTBEAT_INTERVAL_SECONDS
    rig.carrier._poll_once()
    assert rig.methods()[-1:] == ["session.heartbeat"]
    rig.assert_online()


def test_daemon_replacement_refreshes_existing_session_without_reregistering(rig):
    rig.carrier._poll_once()
    rig.restart_daemon()
    rig.calls.clear()
    rig.clock.now += CHANNEL_HEARTBEAT_INTERVAL_SECONDS
    rig.carrier._poll_once()
    assert rig.errors == ["STALE_DAEMON_GENERATION"]
    assert rig.methods() == ["session.heartbeat", "session.refresh"]
    rig.carrier._poll_once()
    assert rig.methods() == ["session.heartbeat", "session.refresh", "message.pending.list"]
    assert rig.app._agent_session_domains.session.read_session(ACTOR).session_ref == REF
    rig.assert_online()
    rig.calls.clear()
    rig.clock.now += CHANNEL_HEARTBEAT_INTERVAL_SECONDS - .001
    rig.carrier._poll_once()
    assert rig.methods() == ["message.pending.wait"]
    rig.clock.now += .002
    rig.carrier._poll_once()
    assert rig.methods()[-1:] == ["session.heartbeat"]
    rig.assert_online()


@pytest.mark.parametrize("already_refreshed", (False, True))
def test_superseded_codex_session_stops_without_registering_over_new_owner(rig, already_refreshed):
    if already_refreshed:
        rig.carrier._poll_once()
        rig.clock.now += CHANNEL_HEARTBEAT_INTERVAL_SECONDS
    rig.register("replacement-thread")
    rig.calls.clear()
    rig.carrier._poll_main()
    assert rig.errors == ["SESSION_SUPERSEDED"]
    assert rig.methods() == ["session.heartbeat" if already_refreshed else "session.refresh"]
    assert rig.app._agent_session_domains.session.read_session(ACTOR).session_ref == "replacement-thread"


def test_stopped_codex_carrier_sends_no_more_heartbeats_or_polls(rig):
    rig.carrier._poll_once()
    carrier = rig.carrier
    rig.stop_carrier()
    rig.carrier = None
    before = list(rig.calls)
    rig.clock.now += AGENT_HEARTBEAT_TTL_SECONDS * 2
    carrier._poll_once()
    assert rig.calls == before


class _AdvancingStop:
    """Drive the actual poll loop and its backoff without wall-clock sleep."""

    def __init__(self, clock, until):
        self.clock = clock
        self.until = until
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.clock.now = round(self.clock.now + seconds, 6)
        if self.clock.now >= self.until:
            self.set()
        return self.stopped


@pytest.mark.parametrize("phase", ("heartbeat", "initial-refresh", "restart-refresh"))
def test_failed_liveness_attempts_are_throttled_and_recover_in_real_poll_loop(rig, phase):
    carrier = rig.carrier
    if phase != "initial-refresh":
        carrier._poll_once()
        rig.clock.now += CHANNEL_HEARTBEAT_INTERVAL_SECONDS
    if phase == "restart-refresh":
        rig.restart_daemon()
    rig.calls.clear()
    started = rig.clock.now
    target = "session.heartbeat" if phase == "heartbeat" else "session.refresh"
    attempts, failures, successes, logs = [], [], [], []
    original_request = carrier.daemon_request

    def request(method, params):
        if method == target:
            attempts.append(rig.clock.now)
            if rig.clock.now < started + 2 * CHANNEL_HEARTBEAT_INTERVAL_SECONDS:
                failures.append(rig.clock.now)
                raise RuntimeError("DAEMON_RESTORING: controlled fast refusal")
            successes.append(rig.clock.now)
        return original_request(method, params)

    carrier.daemon_request = request
    carrier._log = lambda level, event, **fields: logs.append((level, event, fields))
    carrier.poll_seconds = .05
    carrier._stop = _AdvancingStop(rig.clock, started + 3.25)
    carrier._poll_main()

    assert len(attempts) >= 3  # Failure, retry failure, then real daemon success.
    if phase == "heartbeat":
        assert all(later - earlier >= CHANNEL_HEARTBEAT_INTERVAL_SECONDS
                   for earlier, later in zip(attempts, attempts[1:])), attempts
        assert failures == [started, started + CHANNEL_HEARTBEAT_INTERVAL_SECONDS]
        assert successes[0] == started + 2 * CHANNEL_HEARTBEAT_INTERVAL_SECONDS
    else:
        # Shared-core refresh retry uses the poll-lane capped exponential
        # backoff, while listing/delivery continues (G9).
        assert all(later - earlier >= carrier.poll_seconds - 1e-6
                   for earlier, later in zip(attempts, attempts[1:])), attempts
        assert failures == [attempt for attempt in attempts if attempt < started + 2]
        assert successes[0] >= started + 2
    visible_failures = [fields for level, _event, fields in logs
                        if level == "error" and "DAEMON_RESTORING" in fields.get("error", "")]
    assert len(visible_failures) == len(failures)
    assert "session.register" not in rig.methods()
    # A refused refresh stays visible but never blocks listing/delivery (G9).
    assert any(time < successes[0] for time, method, _params in rig.calls
               if method == "message.pending.list")
    if phase == "restart-refresh":
        assert rig.errors
        # #1753 fences inbox reads until refresh restores the live binding.
        # Neither refusal permits a re-register over the surviving session.
        assert set(rig.error_calls) == {
            ("session.heartbeat", "STALE_DAEMON_GENERATION"),
            ("message.pending.list", "STALE_SESSION"),
        }
        assert rig.calls[0][1] == "session.heartbeat"
        assert attempts[0] == started  # Immediate refresh handshake is preserved.
    rig.assert_online()



def test_public_stop_during_stale_heartbeat_does_not_reregister(rig):
    carrier = rig.carrier
    # This test owns the real started loop and public stop lifecycle.
    rig.carrier = None
    entered = threading.Event()
    release = threading.Event()
    stop_errors = []
    closer = None
    try:
        carrier._poll_once()
        rig.clock.now += CHANNEL_HEARTBEAT_INTERVAL_SECONDS
        rig.calls.clear()
        original_request = carrier.daemon_request

        def request(method, params):
            if method == "session.heartbeat":
                rig.calls.append((rig.clock.now, method, dict(params)))
                entered.set()
                assert release.wait(5.0), "test did not release in-flight heartbeat"
                raise RuntimeError("STALE_SESSION: removed during shutdown")
            return original_request(method, params)

        def stop():
            try:
                carrier.stop()
            except BaseException as error:
                stop_errors.append(error)

        carrier.daemon_request = request
        carrier.start()
        assert entered.wait(5.0)
        closer = threading.Thread(target=stop, name="codex-public-stop-probe")
        closer.start()
        assert carrier._stop.wait(5.0)
        # The failure arrives only after public stop has set the real event.
        release.set()
        closer.join(5.0)
        assert not closer.is_alive()
        assert not stop_errors
        assert not carrier._thread.is_alive()
        assert rig.methods() == ["session.heartbeat"]
    finally:
        release.set()
        if closer is not None:
            closer.join(5.0)
        if carrier._store is not None:
            if carrier._thread.ident is None:
                carrier._stop.set()
                carrier.start()
            carrier.stop()
