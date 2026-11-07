"""Contract tests for the App-facing chat adapter v0 (node side).

The adapter binds the browser Core's named port operations
(``capabilities.get`` / ``sessions.send`` / ``sessions.events`` /
``sessions.list`` / ``sessions.history`` / ``sessions.create`` /
``targets.list``) onto the daemon's existing ``message.send``,
``progress.list``, ``hosts`` and ``targets`` surfaces.  These tests pin the
v0 contract: what is passed through, what is refused, and which failures are
definitive rejections vs unknown outcomes.  Directory and creation tests live
in ``directory/test_app_directory_adapter.py`` (module size).
"""

from __future__ import annotations

import pytest

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.gui.adapter import (
    AppAdapterError,
    AppAdapterUnavailable,
    AppChatAdapter,
    SessionBinding,
)

SENDER = "agent:chloe.martin@example.com:dev000000001.local:squire"


class Recorder:
    """Fake daemon request port: records calls, replays a scripted answer."""

    def __init__(self, *answers: object) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._answers = list(answers)

    def __call__(self, method: str, params: dict) -> object:
        self.calls.append((method, dict(params)))
        answer = self._answers.pop(0) if self._answers else {}
        if isinstance(answer, Exception):
            raise answer
        return answer


def make_adapter(recorder: Recorder, **overrides: object) -> AppChatAdapter:
    kwargs: dict = {
        "request": recorder,
        "sender": SENDER,
        "sessions": {
            "s-1": SessionBinding(
                targets=("agent:chloe.martin@example.com:dev000000001.local:mini-gui-worker",),
                conversation_id="conv-1",
            )
        },
    }
    kwargs.update(overrides)
    return AppChatAdapter(**kwargs)


def send_ack(**delivery_overrides: object) -> dict:
    delivery = {
        "target": "agent:target",
        "messageId": "m-1",
        "accepted": True,
        "queued": False,
    }
    delivery.update(delivery_overrides)
    return {
        "ok": all(item["accepted"] for item in [delivery]),
        "operationId": "op-1",
        "conversationId": "conv-1",
        "deliveries": [delivery],
        "messageId": "m-1",
    }


def progress_events(*events: dict) -> dict:
    base = {
        "messageId": "e-1",
        "conversationId": "conv-1",
        "phase": "running",
        "seq": 1,
        "emittedAtMs": 1000,
        "summary": "working",
    }
    rows = [{**base, **overrides} for overrides in events]
    return {"ok": True, "events": rows, "daemonEpoch": "epoch-1"}


def history_entry(**overrides: object) -> dict:
    base = {
        "kind": "message",
        "messageId": "m-1",
        "conversationId": "conv-1",
        "from": "agent:peer",
        "to": SENDER,
        "intent": "request",
        "createdAtMs": 1000,
        "message": "inbound",
        "origin": {"via": "daemon"},
    }
    base.update(overrides)
    return base


def history_answer(*rows: dict, next_cursor: object = None) -> dict:
    return {
        "ok": True,
        "conversationId": "conv-1",
        "entries": list(rows),
        "nextCursor": next_cursor,
    }


# --- capabilities -----------------------------------------------------------


def test_capabilities_report_the_v0_split() -> None:
    adapter = make_adapter(Recorder())

    capabilities = adapter.capabilities()

    operations = capabilities["operations"]
    assert operations["capabilities.get"] == {"supported": True}
    assert operations["sessions.send"] == {"supported": True}
    assert operations["sessions.events"] == {"supported": True}
    assert operations["sessions.list"] == {"supported": True}
    assert operations["sessions.history"] == {"supported": True}
    assert operations["sessions.create"] == {"supported": True}
    assert operations["targets.list"] == {"supported": True}


# --- send: happy path -------------------------------------------------------


def test_send_passes_exact_daemon_parameters_and_projects_ack() -> None:
    recorder = Recorder(send_ack())
    adapter = make_adapter(recorder)

    ack = adapter.send("s-1", {"text": "hello", "idempotencyKey": "k-1"})

    assert ack == {
        "status": "accepted",
        "commandId": "op-1",
        "conversationId": "conv-1",
        "messageId": "m-1",
    }
    assert recorder.calls == [
        (
            "message.send",
            {
                "from": SENDER,
                "to": ["agent:chloe.martin@example.com:dev000000001.local:mini-gui-worker"],
                "message": "hello",
                "summary": "hello",
                "conversationId": "conv-1",
                "idempotencyKey": "k-1",
            },
        )
    ]


def test_send_passes_through_the_daemon_dedup_marker() -> None:
    recorder = Recorder(send_ack(duplicate=True))
    adapter = make_adapter(recorder)

    ack = adapter.send("s-1", {"text": "hello", "idempotencyKey": "k-1"})

    # The daemon marks a replayed idempotent delivery; the adapter reports the
    # node's own verdict instead of guessing.  acceptedAt has no daemon source
    # and must not appear.
    assert ack["deduplicated"] is True
    assert "acceptedAt" not in ack


def test_send_partial_acceptance_names_the_accepted_targets() -> None:
    answer = {
        "ok": False,
        "operationId": "op-9",
        "conversationId": "conv-1",
        "deliveries": [
            {"target": "agent:a", "messageId": "m-a", "accepted": True, "queued": False},
            {
                "target": "agent:b",
                "messageId": "m-b",
                "accepted": False,
                "queued": False,
                "code": "TARGET_UNRESOLVED",
            },
        ],
    }
    recorder = Recorder(answer)
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.send("s-1", {"text": "hi", "idempotencyKey": "k-1"})

    assert info.value.code == "send-rejected"
    assert info.value.details["acceptedTargets"] == ["agent:a"]
    assert "TARGET_UNRESOLVED" in str(info.value.details)


def test_send_includes_session_ref_only_when_configured() -> None:
    recorder = Recorder(send_ack(), send_ack())
    plain = make_adapter(recorder)
    fenced = make_adapter(recorder, session_ref="fence-1")

    plain.send("s-1", {"text": "a", "idempotencyKey": "k-1"})
    fenced.send("s-1", {"text": "b", "idempotencyKey": "k-2"})

    assert "sessionRef" not in recorder.calls[0][1]
    assert recorder.calls[1][1]["sessionRef"] == "fence-1"


# --- send: refusals before any daemon call ----------------------------------


def test_send_rejects_unknown_session_without_calling_the_daemon() -> None:
    recorder = Recorder(send_ack())
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.send("s-missing", {"text": "hi", "idempotencyKey": "k-1"})

    assert info.value.code == "unknown-session"
    assert recorder.calls == []


@pytest.mark.parametrize("identity_key", ["from", "sender", "actor", "onBehalfOf", "sessionRef"])
def test_send_rejects_caller_supplied_identity_fields(identity_key: str) -> None:
    recorder = Recorder(send_ack())
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.send(
            "s-1",
            {"text": "hi", "idempotencyKey": "k-1", identity_key: "someone-else"},
        )

    assert info.value.code == "identity-rejected"
    assert identity_key in str(info.value.details)
    assert recorder.calls == []


def test_send_rejects_unknown_request_fields() -> None:
    recorder = Recorder(send_ack())
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.send("s-1", {"text": "hi", "idempotencyKey": "k-1", "extra": 1})

    assert info.value.code == "invalid-argument"
    assert recorder.calls == []


@pytest.mark.parametrize(
    "request_body",
    [
        {},
        {"text": "", "idempotencyKey": "k-1"},
        {"text": "   ", "idempotencyKey": "k-1"},
        {"text": 7, "idempotencyKey": "k-1"},
        {"text": "hi"},
        {"text": "hi", "idempotencyKey": ""},
        {"text": "hi", "idempotencyKey": 7},
    ],
)
def test_send_requires_non_empty_text_and_idempotency_key(request_body: dict) -> None:
    recorder = Recorder(send_ack())
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.send("s-1", request_body)

    assert info.value.code == "invalid-argument"
    assert recorder.calls == []


# --- send: daemon outcomes --------------------------------------------------


def test_send_definitive_rejection_maps_to_send_rejected() -> None:
    recorder = Recorder(send_ack(accepted=False, queued=False, code="TARGET_UNRESOLVED"))
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.send("s-1", {"text": "hi", "idempotencyKey": "k-1"})

    assert info.value.code == "send-rejected"
    assert "TARGET_UNRESOLVED" in str(info.value.details)
    assert len(recorder.calls) == 1


def test_send_unknown_outcome_is_not_reported_as_failure() -> None:
    recorder = Recorder(
        send_ack(
            accepted=False,
            queued=True,
            outcomeKnown=False,
            code=ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
        )
    )
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterUnavailable):
        adapter.send("s-1", {"text": "hi", "idempotencyKey": "k-1"})


def test_send_transient_daemon_error_is_unavailable() -> None:
    recorder = Recorder(ipc_errors.DaemonUnavailableError("daemon is starting"))
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterUnavailable):
        adapter.send("s-1", {"text": "hi", "idempotencyKey": "k-1"})


def test_send_daemon_rejection_is_definitive() -> None:
    recorder = Recorder(DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "bad target"))
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.send("s-1", {"text": "hi", "idempotencyKey": "k-1"})

    assert info.value.code == "daemon-rejected"
    assert ipc_errors.INVALID_ARGUMENT in str(info.value.details)


@pytest.mark.parametrize(
    "answer",
    [
        "not-a-mapping",
        {},
        {"ok": "yes", "operationId": "op-1", "conversationId": "conv-1", "deliveries": []},
        {"ok": True, "conversationId": "conv-1", "deliveries": [{"accepted": True}]},
        {"ok": True, "operationId": "op-1", "deliveries": [{"accepted": True}]},
    ],
)
def test_send_malformed_daemon_answer_is_unavailable(answer: object) -> None:
    recorder = Recorder(answer)
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterUnavailable):
        adapter.send("s-1", {"text": "hi", "idempotencyKey": "k-1"})


# --- events -----------------------------------------------------------------


def test_events_projects_session_progress_with_numeric_seq() -> None:
    recorder = Recorder(
        progress_events(
            {
                "messageId": "e-1",
                "seq": 0,
                "phase": "accepted",
                "emittedAtMs": 10,
                "summary": "queued",
            },
            {
                "messageId": "e-2",
                "seq": 1,
                "phase": "running",
                "emittedAtMs": 20,
                "summary": "thinking",
            },
        )
    )
    adapter = make_adapter(recorder)

    events = adapter.events("s-1")

    assert events == {
        "entries": [
            {"id": "e-1", "seq": 0, "state": "accepted", "at": 10, "summary": "queued"},
            {"id": "e-2", "seq": 1, "state": "running", "at": 20, "summary": "thinking"},
        ],
        "terminal": None,
    }
    assert recorder.calls == [("progress.list", {"from": SENDER})]


def test_events_none_and_zero_both_mean_from_the_beginning() -> None:
    without = Recorder(progress_events())
    with_zero = Recorder(progress_events())

    make_adapter(without).events("s-1")
    make_adapter(with_zero).events("s-1", since=0)

    expected = ("progress.list", {"from": SENDER})
    assert without.calls == [expected]
    assert with_zero.calls == [expected]


def test_events_positive_since_passes_the_numeric_cursor() -> None:
    recorder = Recorder(progress_events({"messageId": "e-3", "seq": 3}))
    adapter = make_adapter(recorder)

    events = adapter.events("s-1", since=2)

    assert recorder.calls == [("progress.list", {"from": SENDER, "sinceSeq": 2})]
    assert [entry["seq"] for entry in events["entries"]] == [3]


@pytest.mark.parametrize("cursor", [-1, "m-2", 1.5, True])
def test_events_rejects_a_non_integer_or_negative_cursor(cursor: object) -> None:
    recorder = Recorder(progress_events())
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.events("s-1", since=cursor)

    assert info.value.code == "invalid-argument"
    assert recorder.calls == []


def test_events_filters_entries_to_the_session_conversation() -> None:
    recorder = Recorder(
        progress_events(
            {"messageId": "e-1", "conversationId": "conv-1", "seq": 0},
            {
                "messageId": "e-2",
                "conversationId": "conv-other",
                "seq": 1,
                "phase": "completed",
                "emittedAtMs": 30,
                "terminal": True,
            },
            {"messageId": "e-3", "conversationId": "conv-1", "seq": 2},
        )
    )
    adapter = make_adapter(recorder)

    events = adapter.events("s-1")

    assert [entry["id"] for entry in events["entries"]] == ["e-1", "e-3"]
    # Another session's terminal marker must not leak into this session.
    assert events["terminal"] is None


def test_events_terminal_object_reports_the_daemon_marker() -> None:
    recorder = Recorder(
        progress_events(
            {"messageId": "e-1", "seq": 0},
            {
                "messageId": "e-2",
                "seq": 1,
                "phase": "completed",
                "emittedAtMs": 30,
                "terminal": True,
            },
        )
    )
    adapter = make_adapter(recorder)

    events = adapter.events("s-1")

    assert events["terminal"] == {"terminal": "completed", "eventId": "e-2", "at": 30}


def test_events_returns_no_entries_for_a_cursor_past_the_stream() -> None:
    recorder = Recorder(progress_events())
    adapter = make_adapter(recorder)

    events = adapter.events("s-1", since=99)

    assert events == {"entries": [], "terminal": None}
    assert recorder.calls == [("progress.list", {"from": SENDER, "sinceSeq": 99})]


def test_events_rejects_unknown_session_without_calling_the_daemon() -> None:
    recorder = Recorder(progress_events())
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.events("s-missing")

    assert info.value.code == "unknown-session"
    assert recorder.calls == []


@pytest.mark.parametrize(
    "answer",
    [
        "not-a-mapping",
        {},
        {"ok": "yes", "events": []},
        {"ok": True, "events": {"messageId": "e-1"}},
        {
            "ok": True,
            "events": [
                {"conversationId": "conv-1", "phase": "p", "seq": 0, "emittedAtMs": 1, "summary": "s"}
            ],
        },
        {
            "ok": True,
            "events": [
                {"messageId": "e-1", "phase": "p", "seq": 0, "emittedAtMs": 1, "summary": "s"}
            ],
        },
        {
            "ok": True,
            "events": [
                {"messageId": "e-1", "conversationId": "conv-1", "phase": "p", "seq": -1, "emittedAtMs": 1, "summary": "s"}
            ],
        },
        {
            "ok": True,
            "events": [
                {"messageId": "e-1", "conversationId": "conv-1", "phase": "p", "seq": 0, "emittedAtMs": 0, "summary": "s"}
            ],
        },
        {
            "ok": True,
            "events": [
                {"messageId": "e-1", "conversationId": "conv-1", "phase": "p", "seq": 0, "emittedAtMs": 1, "summary": 7}
            ],
        },
    ],
)
def test_events_malformed_progress_answers_are_unavailable(answer: object) -> None:
    recorder = Recorder(answer)
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterUnavailable):
        adapter.events("s-1")


# --- sessions.list and the deferred history ---------------------------------


def test_sessions_list_returns_the_configured_bindings_without_a_daemon_call() -> None:
    recorder = Recorder()
    adapter = make_adapter(recorder)

    assert adapter.sessions_list() == [{"sessionId": "s-1"}]
    assert recorder.calls == []


def test_sessions_list_is_empty_for_a_composition_root_without_sessions() -> None:
    adapter = make_adapter(Recorder(), sessions={})

    assert adapter.sessions_list() == []


# --- history ----------------------------------------------------------------


def test_history_reads_the_durable_conversation_in_one_daemon_call() -> None:
    recorder = Recorder(
        history_answer(
            history_entry(
                messageId="m-sent",
                createdAtMs=1000,
                message="hello",
                **{"from": SENDER, "to": "agent:peer"},
            ),
            history_entry(
                messageId="m-reply",
                intent="reply",
                createdAtMs=2000,
                message="the real reply",
                **{"from": "agent:peer", "to": SENDER},
            ),
        )
    )
    adapter = make_adapter(recorder)

    page = adapter.history("s-1")

    assert recorder.calls == [
        (
            "message.history",
            {"from": SENDER, "conversationId": "conv-1", "limit": 50},
        )
    ]
    assert [event["eventId"] for event in page["events"]] == ["m-sent", "m-reply"]
    # Direction is read off the row the daemon returned, not guessed from
    # whatever happened to be in a local cache: a reply or a row from another
    # sender is the assistant side.
    assert [event["kind"] for event in page["events"]] == ["user", "assistant"]
    assert [event["text"] for event in page["events"]] == [
        "hello",
        "the real reply",
    ]
    assert page["events"][0]["at"].endswith("Z")
    assert page["nextCursor"] is None


def test_history_carries_the_configured_session_fence() -> None:
    recorder = Recorder(history_answer())
    adapter = make_adapter(recorder, session_ref="session-worker-b")

    assert adapter.history("s-1") == {"events": [], "nextCursor": None}

    assert recorder.calls == [
        (
            "message.history",
            {
                "from": SENDER,
                "conversationId": "conv-1",
                "limit": 50,
                "sessionRef": "session-worker-b",
            },
        )
    ]


def test_history_passes_the_daemon_cursor_back_unchanged() -> None:
    recorder = Recorder(
        history_answer(
            history_entry(messageId="m-1", createdAtMs=1000),
            next_cursor="opaque-daemon-token",
        ),
        history_answer(history_entry(messageId="m-2", createdAtMs=2000)),
    )
    adapter = make_adapter(recorder)

    first = adapter.history("s-1", limit=1)
    assert [event["eventId"] for event in first["events"]] == ["m-1"]
    assert first["nextCursor"] == "opaque-daemon-token"

    rest = adapter.history("s-1", cursor=first["nextCursor"], limit=1)

    assert [event["eventId"] for event in rest["events"]] == ["m-2"]
    assert rest["nextCursor"] is None
    assert recorder.calls[1] == (
        "message.history",
        {
            "from": SENDER,
            "conversationId": "conv-1",
            "limit": 1,
            "cursor": "opaque-daemon-token",
        },
    )


@pytest.mark.parametrize("limit", [0, -1, 201, 1.5, "5", True])
def test_history_rejects_a_limit_outside_the_page_window(limit: object) -> None:
    recorder = Recorder()
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.history("s-1", limit=limit)

    assert info.value.code == "invalid-argument"
    assert recorder.calls == []


@pytest.mark.parametrize("cursor", ["", 7])
def test_history_rejects_a_non_string_cursor(cursor: object) -> None:
    recorder = Recorder()
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.history("s-1", cursor=cursor)

    assert info.value.code == "invalid-argument"
    assert recorder.calls == []


def test_history_rejects_unknown_session_without_calling_the_daemon() -> None:
    recorder = Recorder()
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.history("s-missing")

    assert info.value.code == "unknown-session"
    assert recorder.calls == []


def test_history_daemon_rejection_is_definitive_and_never_falls_back() -> None:
    recorder = Recorder(
        DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "bad cursor")
    )
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.history("s-1")

    assert info.value.code == "daemon-rejected"
    # The old two-way ``message.query`` projection is not a fallback: a daemon
    # that cannot answer this read leaves history unknown, not partial.
    assert recorder.calls == [
        (
            "message.history",
            {"from": SENDER, "conversationId": "conv-1", "limit": 50},
        )
    ]


@pytest.mark.parametrize(
    "answer",
    [
        "not-a-mapping",
        {},
        {"ok": False, "conversationId": "conv-1", "entries": []},
        {"ok": True, "conversationId": "conv-1", "entries": {}},
        {"ok": True, "conversationId": "conv-1", "entries": [7]},
        # A notice is never a conversation message; skipping it silently
        # would be a partial projection.
        history_answer(history_entry(kind="notice")),
        # Wrong conversation, incomplete row, unreadable timestamp.
        {"ok": True, "conversationId": "conv-2", "entries": []},
        history_answer({"messageId": "m-1"}),
        history_answer(history_entry(conversationId="conv-2")),
        history_answer(history_entry(createdAtMs=0)),
        # Duplicate and non-monotonic pages are malformed, not deduped here:
        # the daemon owns ordering and message-id deduplication.
        history_answer(
            history_entry(messageId="m-1", createdAtMs=1000),
            history_entry(messageId="m-1", createdAtMs=1000),
        ),
        history_answer(
            history_entry(messageId="m-2", createdAtMs=2000),
            history_entry(messageId="m-1", createdAtMs=1000),
        ),
        # A cursor without a page could loop forever; an empty token is unusable.
        history_answer(next_cursor="cursor-without-rows"),
        history_answer(history_entry(), next_cursor=""),
    ],
)
def test_history_malformed_answers_are_unavailable(answer: object) -> None:
    recorder = Recorder(answer)
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterUnavailable):
        adapter.history("s-1")


# --- construction validation ------------------------------------------------


def test_construction_requires_a_callable_request_port() -> None:
    with pytest.raises(TypeError):
        AppChatAdapter(request="not-callable", sender="a", sessions={})


@pytest.mark.parametrize("sender", ["", "   ", 7, None])
def test_construction_requires_a_non_empty_sender(sender: object) -> None:
    with pytest.raises(ValueError):
        make_adapter(Recorder(), sender=sender)


def test_construction_validates_session_bindings() -> None:
    with pytest.raises(ValueError):
        make_adapter(Recorder(), sessions={"": SessionBinding(targets=("t",), conversation_id="c")})
    with pytest.raises(ValueError):
        make_adapter(Recorder(), sessions={"s": SessionBinding(targets=(), conversation_id="c")})
    with pytest.raises(ValueError):
        make_adapter(Recorder(), sessions={"s": SessionBinding(targets=("t",), conversation_id="")})
    with pytest.raises(ValueError):
        make_adapter(Recorder(), sessions={"s": "not-a-binding"})


def test_send_summary_is_utf8_bounded_without_truncating_body() -> None:
    recorder = Recorder(send_ack())
    text = "测" * 200
    make_adapter(recorder).send("s-1", {"text": text, "idempotencyKey": "utf8"})
    params = recorder.calls[0][1]
    assert params["message"] == text
    assert params["summary"] == "测" * 170
    assert len(params["summary"].encode("utf-8")) <= 512
