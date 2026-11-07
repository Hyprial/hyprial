"""Real fake-provider process: dropping every progress event preserves facts."""
import sys
import time

from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerProcess
from hyprial.daemon.impl.cells.observability.policy import RunPolicy
from hyprial.daemon.impl.cells.observability.reducer import derive_run_state
from tests.daemon.turns.test_turn_stall_watch import FAKE_CODEX, codex_spec, delivery, wire_methods


def test_quiet_owner_observation_survives_total_progress_loss(tmp_path):
    log = tmp_path / "wire.jsonl"
    process = CodexAppServerProcess(
        codex_spec(idle_timeout_seconds=0.8), command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_MODE": "quiet-then-complete", "FAKE_CODEX_LOG": str(log)},
        reconnect_delay_seconds=0.01, max_delivery_attempts=1,
    )
    quiet = None
    try:
        assert process.wait_ready(timeout=5)
        assert process.enqueue(delivery())
        deadline = time.monotonic() + 10
        results = []
        while time.monotonic() < deadline and not results:
            process.drain_progress()  # Deliberately discard all route-C events.
            facts = process.read_run_facts()
            if facts.quiet:
                quiet = derive_run_state(facts, RunPolicy(turn_long_after_ms=1),
                                         now_ms=time.time_ns() // 1_000_000,
                                         monotonic_ms=time.monotonic_ns() // 1_000_000)
            results.extend(process.drain_results())
            time.sleep(0.01)
        assert results and quiet is not None
        assert quiet["suspicions"] == ["turn-over-budget", "producer-quiet"]
        assert quiet["coverage"]["producerActivity"]
        assert quiet["thresholds"]["quietAfterMs"] == 800
        assert process.read_run_facts().open_count == 0
    finally:
        process.stop()
    assert "turn/interrupt" not in wire_methods(log)
