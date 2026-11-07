"""Crash probes for the durable message.status budget.

This is the reviewer's poll-thread subprocess probe, kept as a regression:
every child uses the real carrier poll loop and the real SQLite store; the
parent judges only the number of status queries after crashed children.
The late-deferred write ordering is pinned deterministically by
``test_settlement_attempt_writes_are_monotonic``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses.carrier.codex_carrier_store import CodexCarrierStore
from tests.daemon.harnesses.codex.test_codex_carrier_settlement import (
    ACTOR,
    THREAD_ID,
    _store_rows,
)


def _seed(path: Path, message_id: str) -> None:
    store = CodexCarrierStore(path)
    delivery = HarnessDelivery(
        delivery_id=message_id,
        conversation_id=f"c-{message_id}",
        sender="requester",
        recipient=ACTOR,
        message=f"prompt-{message_id}",
    )
    store.record_fetched(ACTOR, THREAD_ID, delivery, "reply")
    store.record_turn_started(ACTOR, message_id, f"turn-{message_id}")
    store.record_final(ACTOR, message_id, f"final-{message_id}")
    store.close()


def test_poll_thread_delayed_deferred_cannot_refund_status_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "carrier.sqlite3"
    queries = tmp_path / "queries"
    _seed(path, "m-live")
    code = r'''
import os
import threading
from pathlib import Path
from tests.daemon.harnesses.codex.test_codex_carrier_settlement import (
    _carrier, _hint, RecordingLogger, TurnServer,
)
entered = threading.Event()
release = threading.Event()
written = threading.Event()
calls = 0

def request(method, params):
    global calls
    hinted = _hint(method, params)
    if hinted is not None:
        return hinted
    if method in {"session.refresh", "session.heartbeat", "session.register"}:
        return {"ok": True}
    if method == "message.pending.list":
        return {"ok": True, "messages": []}
    if method == "message.ack":
        return {"ok": False, "acknowledged": False, "code": "MESSAGE_ACK_UNAVAILABLE"}
    assert method == "message.status", method
    calls += 1
    with open(os.environ["REVIEW_QUERIES"], "a") as output:
        output.write("query\n")
        output.flush()
        os.fsync(output.fileno())
    if calls == 2:
        assert entered.wait(3)
        release.set()
        assert written.wait(3)
        os._exit(42)
    return {"ok": True, "state": "unknown"}

carrier = _carrier(TurnServer(), request, Path(os.environ["REVIEW_STATE"]), RecordingLogger())
original = carrier._store.record_settlement_attempts

def delayed(actor, message_id, attempts):
    if threading.current_thread() is not carrier._thread:
        entered.set()
        assert release.wait(5)
        original(actor, message_id, attempts)
        written.set()
    else:
        original(actor, message_id, attempts)

carrier._store.record_settlement_attempts = delayed
carrier._settle_status_unknown = lambda *args, **kwargs: os._exit(43)
carrier.start()
carrier._thread.join(8)
os._exit(44)
'''
    exits = []
    for _ in range(6):
        child = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "REVIEW_STATE": str(path), "REVIEW_QUERIES": str(queries)},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert child.returncode in {42, 43}, (child.stdout, child.stderr)
        exits.append(child.returncode)
        if child.returncode == 43:
            break
    count = len(queries.read_text().splitlines())
    assert count <= 9, f"queries={count}; exits={exits}; rows={_store_rows(path)}"
