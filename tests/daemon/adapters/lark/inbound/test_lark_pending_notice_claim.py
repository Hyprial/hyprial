"""Unknown-submission notices have one durable attempt, independent of audit."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.daemon.adapters.lark.outbound.test_lark_submission_outcome_unknown import _Routes
from tests.daemon.adapters.lark.outbound.test_lark_submission_outcome_unknown import make_rig as make_rig
from hyprial.daemon.impl.adapters.lark import LarkAdapter, LarkStateStore
from hyprial.daemon.impl.adapters.lark.state import store as state_module
from hyprial.daemon.impl.adapters.lark.worker.daemon_link import _Harness
from hyprial.kernel import ipc_errors


@pytest.mark.parametrize("native_failure", [False, True], ids=["delivered-notice", "failed-notice"])
def test_unknown_notice_attempt_survives_audit_eviction_clock_and_rebuild(make_rig, monkeypatch, native_failure):
    rig = make_rig(phase="before-commit")
    attempts = []
    original_reply = rig.platform.reply
    def reply(message_id, text, *, idempotency_key):
        attempts.append((message_id, text, idempotency_key))
        if native_failure:
            raise OSError("native notification response unavailable")
        return original_reply(message_id, text, idempotency_key=idempotency_key)
    monkeypatch.setattr(rig.platform, "reply", reply)
    assert rig.adapter.handle_inbound(rig.message).ack_error == "SubmitOutcomeUnknownError"
    assert len(attempts) == 1
    rig.release.set()
    assert rig.settled.wait(3)
    state = rig.adapter._state
    frozen = state.pending_inbound_submission(rig.message.message_id)
    [letter] = state.dead_letters()
    monkeypatch.setattr(state_module, "MAX_DEAD_LETTERS", 1)
    state.record_dead_letter(replace(letter, message_id="other-native", event_id="other-event", chat_id="other-chat", conversation_id="other-conversation"))
    assert all(row.message_id != rig.message.message_id for row in state.dead_letters())
    later = rig.adapter._utcnow() + timedelta(hours=2)
    rig.adapter.close()
    rig.adapter = LarkAdapter(state=LarkStateStore(rig.state_path), lark=rig.platform, harness=_Harness(rig.short_socket), routes=_Routes(), channel_actor_id="channel:lark:test", logger=rig.logs, org_context_path=rig.root / "no-context", transient_retry_budget=0, utcnow=lambda: later)
    original_handle = rig.app._timed_handle
    def still_unknown(_method, _params):
        raise ipc_errors.DaemonRequestError(ipc_errors.DAEMON_ERROR, "still unconfirmed")
    rig.app._timed_handle = still_unknown
    assert rig.adapter.recover_message(rig.message.message_id).ack_error == "SubmitOutcomeUnknownError"
    assert len(attempts) == 1
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id) == frozen
    assert not any(event == "terminal.failure" for event, _ in rig.logs.events)
    assert not rig.adapter._state.claim_pending_inbound_notice(rig.message.message_id)
    rig.app._timed_handle = original_handle
    assert rig.adapter.recover_message(rig.message.message_id).status == "forwarded"
    assert rig.adapter._state.pending_inbound_submission(rig.message.message_id) is None
    assert not rig.adapter._state.claim_pending_inbound_notice(rig.message.message_id)
    assert len(attempts) == 1


def test_notice_claim_is_atomic_adapter_scoped_and_does_not_change_payload_budget(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    stores = [LarkStateStore(path, adapter="one") for _ in range(4)]
    other = LarkStateStore(path, adapter="two")
    try:
        expected_bytes = sum(len(value.encode()) for value in ("native", "event", "frozen request"))
        monkeypatch.setattr(state_module, "MAX_PENDING_INBOUND_SUBMISSION_BYTES", expected_bytes)
        stores[0].record_pending_inbound_submission("native", "event", "frozen request")
        other.record_pending_inbound_submission("native", "event", "frozen request")
        def size():
            return stores[0]._db.execute("SELECT SUM(length(CAST(message_id AS BLOB)) + length(CAST(event_id AS BLOB)) + length(CAST(request AS BLOB))) FROM pending_inbound_submissions WHERE adapter = 'one'").fetchone()[0]
        assert size() == expected_bytes
        barrier = threading.Barrier(len(stores))
        results = []
        def claim(store):
            barrier.wait(timeout=3)
            results.append(store.claim_pending_inbound_notice("native"))
        threads = [threading.Thread(target=claim, args=(store,)) for store in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()
        assert sorted(results) == [False, False, False, True]
        assert other.claim_pending_inbound_notice("native")
        assert size() == expected_bytes
        assert stores[0].record_pending_inbound_submission("native", "other-event", "replacement") == ("frozen request", False)
        assert not stores[0].claim_pending_inbound_notice("native")
        with pytest.raises(state_module.PendingInboundSubmissionCapacityError):
            stores[0].record_pending_inbound_submission("another", "event", "new")
    finally:
        for store in [*stores, other]:
            store.close()


def test_old_pending_schema_migrates_serially_across_adapter_processes(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE pending_inbound_submissions(adapter TEXT NOT NULL, message_id TEXT NOT NULL, event_id TEXT NOT NULL, request TEXT NOT NULL, PRIMARY KEY(adapter,message_id))")
        db.execute("INSERT INTO pending_inbound_submissions VALUES ('one','old-native','old-event','old frozen request')")
    script = '''
import sys
from pathlib import Path
from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore
print('READY',flush=True)
sys.stdin.readline()
store=LarkStateStore(Path(sys.argv[1]),adapter=sys.argv[2])
try:
    assert store._db.execute("SELECT notice_claimed FROM pending_inbound_submissions WHERE adapter='one' AND message_id='old-native'").fetchone()[0]==1
    assert not store.claim_pending_inbound_notice('old-native')
    store.record_pending_inbound_submission('fresh-native','fresh-event','fresh frozen request')
    assert store.claim_pending_inbound_notice('fresh-native')
    assert not store.claim_pending_inbound_notice('fresh-native')
    print('OK',flush=True)
finally:store.close()
'''
    workers = [subprocess.Popen([sys.executable, "-c", script, str(path), adapter], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for adapter in ("one", "two", "three")]
    try:
        for worker in workers:
            assert worker.stdout.readline().strip() == "READY"
        for worker in workers:
            worker.stdin.write("start\n")
            worker.stdin.flush()
        for worker in workers:
            output, errors = worker.communicate(timeout=15)
            assert worker.returncode == 0, errors
            assert output.strip() == "OK"
        with sqlite3.connect(path) as db:
            assert len([row for row in db.execute("PRAGMA table_info(pending_inbound_submissions)") if row[1] == "notice_claimed"]) == 1
            assert db.execute("SELECT request,notice_claimed FROM pending_inbound_submissions WHERE adapter='one' AND message_id='old-native'").fetchone() == ("old frozen request", 1)
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=5)
