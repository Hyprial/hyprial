from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
import sqlite3

import pytest

from hyprial.kernel import Logger
from hyprial.daemon.impl.adapters.lark.contracts.wire import HarnessSubmissionRejected

from ..support import NOW_MS, actor as actor, inbound_message, make_adapter
from .test_outage_backfill import restart


def frozen_backlog(tmp_path, actor, count, *, history=True, tied=False):
    before = NOW_MS - 59 * 60_000
    adapter, lark, harness, routes = make_adapter(
        tmp_path, actor, utcnow=lambda: datetime.fromtimestamp(before / 1000, UTC)
    )
    adapter.handle_inbound(inbound_message("before", chat_type="p2p", create_time=str(before)))
    original = harness.send_request

    def crash(_request, **_kwargs):
        raise KeyboardInterrupt("isolated freeze-before-IPC crash")

    harness.send_request = crash
    messages = []
    for index in range(count):
        created = before + (60_000 if index == count - 1 else 120_000 + index)
        if count > 32:
            created = before + 60_000 + ((index * 17) % count) * 1000
        message = inbound_message(
            f"a{index:02d}", chat_type="p2p",
            create_time=str(before + 60_000 if tied else created),
        )
        if tied and index < count - 1:
            message = replace(message, reply_to=f"a{count - 1:02d}")
        messages.append(message)
        assert adapter.handle_inbound(message).status == "error"
    assert adapter._state.pending_inbound_submission_count() == count
    frozen = {m.message_id: adapter._state.pending_inbound_submission(m.message_id) for m in messages}
    harness.send_request = original
    lark.history["oc_dm"] = tuple(messages) if history else ()
    logger = Logger(tmp_path / "logs", component="test", name="pending")
    adapter = restart(adapter, lark, harness, routes, NOW_MS, logger)
    return adapter, lark, harness, routes, frozen


@pytest.mark.parametrize("count,history,tied", [(17, True, False), (41, True, False), (41, False, False), (17, False, True)])
def test_pending_pages_make_bounded_chronological_progress(tmp_path, actor, count, history, tied):
    adapter, _lark, harness, _routes, frozen = frozen_backlog(
        tmp_path, actor, count, history=history, tied=tied
    )
    try:
        for _ in range((count + 15) // 16):
            report = adapter.reconcile_recent(now_ms=NOW_MS)
            assert 1 <= report.pending_attempted <= 16
            assert report.forwarded > 0
        assert adapter._state.pending_inbound_submission_count() == 0
        expected = [f"a{count - 1:02d}", *[f"a{i:02d}" for i in range(count - 1)]]
        if count > 32:
            expected = [f"a{i:02d}" for i in sorted(range(count), key=lambda i: (i * 17) % count)]
        assert [r.message_id for r in harness.requests] == ["before", *expected]
        for request in harness.requests[1:]:
            assert asdict(request) == json.loads(frozen[request.message_id])["request"]
        live = adapter.handle_inbound(inbound_message("live", chat_type="p2p", create_time=str(NOW_MS)))
        assert live.status == "forwarded"
        assert adapter.reconcile_recent(now_ms=NOW_MS).forwarded == 0
        assert [r.message_id for r in harness.requests] == ["before", *expected, "live"]
    finally:
        adapter.close()


def test_chat_scoped_live_gate_ignores_other_chats_backlog(tmp_path, actor):
    adapter, _lark, harness, _routes, _frozen = frozen_backlog(tmp_path, actor, 17)
    adapter._state.observe_chat("oc_other", NOW_MS - 59 * 60_000)
    _routes.pins["lark:oc_other:oc_other"] = actor
    try:
        outcome = adapter.handle_inbound(inbound_message(
            "other-live", chat_id="oc_other", chat_type="p2p", create_time=str(NOW_MS)
        ))
        assert outcome.status == "forwarded"
        assert adapter._state.pending_inbound_submission_count() == 17
        assert [r.message_id for r in harness.requests] == ["before", "other-live"]
    finally:
        adapter.close()


@pytest.mark.parametrize("error_type", [RuntimeError, HarnessSubmissionRejected])
def test_poison_is_durably_dead_lettered_and_chat_ungates(tmp_path, actor, error_type):
    adapter, lark, harness, routes, frozen = frozen_backlog(tmp_path, actor, 17)
    original = harness.send_request
    attempts = []

    def poison(request, **kwargs):
        if request.message_id == "a16":
            attempts.append(request)
            raise error_type("isolated persistent sink failure")
        return original(request, **kwargs)

    harness.send_request = poison
    try:
        for index in range(5):
            report = adapter.reconcile_recent(now_ms=NOW_MS)
            assert report.forwarded or report.retryable_errors or report.dead_lettered
            if index < 2:
                assert report.retryable_errors
                adapter = restart(adapter, lark, harness, routes, NOW_MS, adapter._logger)
            if adapter._state.pending_inbound_submission_count() == 0:
                break
        assert adapter._state.pending_inbound_submission_count() == 0
        assert len(attempts) == 3
        assert all(asdict(r) == json.loads(frozen["a16"])["request"] for r in attempts)
        letters = adapter.dead_letters()
        assert any(d.message_id == "a16" and d.reason == "submission-retry-exhausted" for d in letters)
        assert adapter._state.pending_inbound_submission("a16") == frozen["a16"]
        events = [json.loads(line) for line in adapter._logger.path.read_text().splitlines()]
        terminal = [e for e in events if e.get("event") == "lark.backfill.dead-lettered"]
        assert len(terminal) == 1
        assert terminal[0]["reason"] == "submission-retry-exhausted"
        assert terminal[0]["attempts"] == 3
        assert "oc_dm" not in json.dumps(terminal)
        assert "isolated persistent" not in json.dumps(terminal)
        adapter = restart(adapter, lark, harness, routes, NOW_MS, adapter._logger)
        assert adapter.reconcile_recent(now_ms=NOW_MS).forwarded == 0
        assert len(attempts) == 3
        assert adapter.handle_inbound(inbound_message("live", chat_type="p2p", create_time=str(NOW_MS))).status == "forwarded"
        assert [r.message_id for r in harness.requests] == ["before", *[f"a{i:02d}" for i in range(16)], "live"]
        harness.send_request = original
        assert adapter.recover_message("a16").status == "forwarded"
        assert adapter._state.pending_inbound_submission("a16") is None
    finally:
        adapter.close()


def test_terminal_custody_and_dead_letter_commit_atomically(tmp_path, actor):
    adapter, _lark, _harness, _routes, frozen = frozen_backlog(tmp_path, actor, 1)
    state = adapter._state
    pending = state.pending_inbound_submission_record("a00")
    try:
        for expected in (1, 2):
            assert state.fail_pending_inbound(pending, now=adapter._utcnow()) == expected
        state._db.execute("""CREATE TRIGGER reject_terminal BEFORE INSERT ON dead_letters
            BEGIN SELECT RAISE(ABORT, 'isolated audit failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="isolated audit failure"):
            state.fail_pending_inbound(pending, now=adapter._utcnow())
        assert not state.pending_inbound_dead_lettered("a00")
        assert state.pending_inbound_submission_count() == 1
        assert state.pending_inbound_submission("a00") == frozen["a00"]
        state._db.execute("DROP TRIGGER reject_terminal")
        assert state.fail_pending_inbound(pending, now=adapter._utcnow()) == 3
        assert state.pending_inbound_dead_lettered("a00")
        assert state.dead_letters()[0].reason == "submission-retry-exhausted"
        assert not state.seen_message("a00")
        assert state.pending_inbound_submission_count() == 0
        assert state.pending_inbound_submission("a00") == frozen["a00"]
    finally:
        adapter.close()
