"""UA2b items 5 and 6: the recorder's observer retries without rescanning.

Item 5: a failed acknowledgement (a refusal, or the daemon still restoring)
is remembered by row and retried on later passes; no pass rescans the inbox
feed from position 0, and the failures count toward the recorder's backoff.

Item 6: while the observer cannot know what it awaits yet (no user agent on
this node at the first pass), the replay of already recorded rows is not
marked done; it runs once the principal is known.
"""

from __future__ import annotations

from hyprial.kernel import ipc_errors
from tests.daemon.conversations.user_target.user_target_support import (
    boot_with_user_agent,
    operator_send,
)
from tests.support.golden.app import ALPHA, OWNER_USER, golden_daemon as golden_daemon


def _reply(app, message_id: str) -> str:
    from tests.daemon.acceptance.acceptance_support import session

    return app.handle("message.reply", {
        **session(ALPHA), "messageId": message_id, "message": "answer", "summary": "answer",
    })["replyMessageId"]


def _pending(app) -> list[str]:
    return [m.message_id for m in app._inbox.pending_messages(OWNER_USER)]


def _drain(recorder) -> None:
    for _ in range(16):
        if not recorder.run_once().more:
            return


def _feed_reads(monkeypatch, recorder) -> list[tuple[str, int]]:
    reads: list[tuple[str, int]] = []
    feed = recorder._feed
    real = feed.interaction_rows_after

    def spy(source, after, **kwargs):
        reads.append((source, after))
        return real(source, after, **kwargs)

    monkeypatch.setattr(feed, "interaction_rows_after", spy)
    return reads


def test_a_failing_ack_is_retried_by_row_with_backoff_and_no_rescan(
    golden_daemon, monkeypatch
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app, recorder = booted.app, booted.recorder
    _drain(recorder)
    reply_id = _reply(app, operator_send(app, ALPHA, "restoring-ack")["messageId"])
    real_handle = app.handle
    refusals: list[str] = []

    def restoring_twice(method, params, **kwargs):
        if method == "message.ack" and len(refusals) < 2:
            refusals.append(params["messageId"])
            raise ipc_errors.DaemonRestoringError("restoring")
        return real_handle(method, params, **kwargs)

    monkeypatch.setattr(app, "handle", restoring_twice)

    first = recorder.run_once()
    assert refusals == [reply_id]
    assert first.failed is True and recorder.failures == 1
    reads = _feed_reads(monkeypatch, recorder)
    second = recorder.run_once()
    assert refusals == [reply_id, reply_id]
    assert second.failed is True and recorder.failures == 2
    third = recorder.run_once()

    assert third.failed is False and recorder.failures == 0
    assert _pending(app) == []
    assert all(after > 0 for _, after in reads), reads


def test_a_row_acknowledged_elsewhere_is_dropped_from_the_retry(
    golden_daemon, monkeypatch
) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app, recorder = booted.app, booted.recorder
    _drain(recorder)
    reply_id = _reply(app, operator_send(app, ALPHA, "acked-elsewhere")["messageId"])
    real_handle = app.handle
    acks: list[str] = []

    def refuse(method, params, **kwargs):
        if method == "message.ack":
            acks.append(params["messageId"])
            return {"ok": False, "messageId": params["messageId"], "acknowledged": False}
        return real_handle(method, params, **kwargs)

    monkeypatch.setattr(app, "handle", refuse)
    assert recorder.run_once().failed is True
    real_handle("message.ack", {"from": OWNER_USER, "messageId": reply_id},
                _trusted_message_origin="daemon")

    assert recorder.run_once().failed is False
    assert recorder.run_once().failed is False
    assert acks == [reply_id]


def test_replay_waits_until_the_principal_is_known(golden_daemon, monkeypatch) -> None:
    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app, recorder = booted.app, booted.recorder
    observer = recorder._observer
    monkeypatch.setattr(recorder, "_observer", None)
    reply_id = _reply(app, operator_send(app, ALPHA, "early-replay")["messageId"])
    _drain(recorder)
    assert _pending(app) == [reply_id]
    monkeypatch.setattr(recorder, "_observer", observer)
    monkeypatch.setattr(recorder, "_replayed", False)
    principal = observer._principal
    monkeypatch.setattr(observer, "_principal", lambda: None)

    recorder.run_once()
    assert _pending(app) == [reply_id]
    monkeypatch.setattr(observer, "_principal", principal)
    recorder.run_once()

    assert _pending(app) == []


def _log_events(monkeypatch, recorder) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        recorder, "_log", lambda level, event, **fields: events.append((event, fields))
    )
    return events


def test_a_permanently_refused_ack_gives_up_after_the_attempt_cap(
    golden_daemon, monkeypatch
) -> None:
    from hyprial.daemon.impl.conversations.record import recorder as recorder_module

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app, recorder = booted.app, booted.recorder
    _drain(recorder)
    reply_id = _reply(app, operator_send(app, ALPHA, "never-acked")["messageId"])
    real_handle = app.handle
    acks: list[str] = []

    def refuse(method, params, **kwargs):
        if method == "message.ack":
            acks.append(params["messageId"])
            return {"ok": False, "messageId": params["messageId"], "acknowledged": False}
        return real_handle(method, params, **kwargs)

    monkeypatch.setattr(app, "handle", refuse)
    events = _log_events(monkeypatch, recorder)
    cap = recorder_module.OBSERVER_ATTEMPT_MAX

    for _ in range(cap + 3):
        recorder.run_once()

    assert acks == [reply_id] * cap
    assert not recorder._observe_retry
    [(_, fields)] = [item for item in events if item[0] == "conversation.observer_gave_up"]
    assert fields["attempts"] == cap
    assert _pending(app) == [reply_id]
    assert recorder.run_once().failed is False


def test_a_long_restore_does_not_use_up_the_attempt_cap(golden_daemon, monkeypatch) -> None:
    from hyprial.daemon.impl.conversations.record import recorder as recorder_module

    booted = boot_with_user_agent(golden_daemon, monkeypatch)
    app, recorder = booted.app, booted.recorder
    _drain(recorder)
    reply_id = _reply(app, operator_send(app, ALPHA, "long-restore")["messageId"])
    real_handle = app.handle
    restoring = recorder_module.OBSERVER_ATTEMPT_MAX + 3
    refusals: list[str] = []

    def restoring_for_a_while(method, params, **kwargs):
        if method == "message.ack" and len(refusals) < restoring:
            refusals.append(params["messageId"])
            raise ipc_errors.DaemonRestoringError("restoring")
        return real_handle(method, params, **kwargs)

    monkeypatch.setattr(app, "handle", restoring_for_a_while)

    for _ in range(restoring + 1):
        recorder.run_once()

    assert refusals == [reply_id] * restoring
    assert _pending(app) == []
