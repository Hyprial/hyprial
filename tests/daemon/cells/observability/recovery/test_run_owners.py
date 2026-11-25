"""Owner reads bypass lossy hook/progress paths and preserve receipt fences."""
import time

from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_runtime import TurnRuntime
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import CloseTurnPumpCommand
from tests.daemon.turns.test_turn_actor_runtime import _enqueue, _wait_until
from tests.daemon.cells.observability.mount.test_mount import composed as composed
from hyprial.daemon.impl.operations.session_ports import RegisterSessionCommand, SessionMutationCompleted
from hyprial.kernel import CHANNEL_PROTOCOL_VERSION


def test_managed_turn_read_is_owner_current_and_has_monotonic_age():
    runtime = TurnRuntime(name="cell-test", io_submit=lambda *args: None,
                          io_interrupt=lambda *args: None, io_close=lambda *args: None,
                          io_abort=lambda *args: None, io_failure_decision=lambda *args: None)
    try:
        assert runtime.read_run_facts().known
        runtime.submit(_enqueue("one"))
        _wait_until(lambda: runtime.read_in_flight() is not None)
        first = runtime.read_run_facts()
        assert first.open_count == 1 and first.since_ms is not None
        assert first.since_monotonic_ms <= time.monotonic_ns() // 1_000_000
        assert runtime.read_run_facts() == first
    finally:
        runtime.submit(CloseTurnPumpCommand("close-cell-test", deadline_ms=100))
        assert runtime.drain(1)


def test_session_facts_are_fenced_and_never_cross_same_session_rebind(composed):
    app, *_ = composed
    actor = app.agents.create("terminal").uri
    domains = app._agent_session_domains
    domains.call_session(RegisterSessionCommand(
        "register-c", actor, "/tmp", ("claude",), "claude-channel", "c-session",
        channel_confirmed=True, channel_protocol_version=CHANNEL_PROTOCOL_VERSION,
        channel_lease_token="c-lease-01234567890123456789012345",
    ), SessionMutationCompleted)
    app._record_carrier_turn_started(actor, "c-session", "run")
    facts = app._cell_interactive_facts()
    assert next(iter(facts.values())).open_count == 1
    domains.liveness.release(actor)
    domains.liveness.bind(actor, harness="claude", runtime="interactive", session_id="c-session")
    assert all(not row.known for row in app._cell_interactive_facts().values())
    app._record_carrier_turn_ended(actor, "c-session", "end-first", 100)
    app._record_carrier_turn_started(actor, "c-session", "end-first")
    assert next(iter(app._cell_interactive_facts().values())).open_count == 0
