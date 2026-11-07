"""Contract tests for the adapter's bounded directory and session creation.

Split from ``test_app_chat_adapter`` only to keep each test module inside the
1000-code-line structural limit.  The helpers are the same fake daemon request
port and adapter factory used by the parent module.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import pytest

from hyprial.shell.impl.gui.adapter import AppAdapterError, AppAdapterUnavailable
from tests.shell.gui.adapter.test_app_chat_adapter import (
    SENDER,
    Recorder,
    history_entry,
    make_adapter,
)

# --- targets.list -----------------------------------------------------------

MINI_TARGET = "agent:chloe.martin@example.com:dev000000001.local:mini-gui-worker"
ECHO_TARGET = "agent:chloe.martin@example.com:dev000000001.local:echo"


def node_row(node_id: str = "node-a", status: str = "online") -> dict:
    return {"nodeId": node_id, "status": status}


def agent_row(target_uri: str = ECHO_TARGET, **overrides: object) -> dict:
    row = {
        "targetKind": "agent",
        "targetUri": target_uri,
        "actor": "agent:someone",
        "status": "online",
        "deliverable": True,
    }
    row.update(overrides)
    return row


def hosts_answer(*rows: dict) -> dict:
    return {"hosts": list(rows)}


def targets_answer(*rows: dict) -> dict:
    return {"targets": list(rows)}


def deterministic_ids(target_uri: str) -> tuple[str, str]:
    session_id = str(uuid5(NAMESPACE_URL, f"hyprial:gui:session:{SENDER}:{target_uri}"))
    conversation_id = str(
        uuid5(NAMESPACE_URL, f"hyprial:gui:conversation:{SENDER}:{target_uri}")
    )
    return session_id, conversation_id


def test_targets_list_reads_hosts_then_agent_targets() -> None:
    recorder = Recorder(
        hosts_answer(node_row("node-a"), node_row("node-b", "offline")),
        targets_answer(agent_row(hosted=True, hostedBy="node-a")),
    )
    adapter = make_adapter(recorder)

    directory = adapter.targets_list()

    assert directory == {
        "nodes": [
            {"nodeId": "node-a", "status": "online"},
            {"nodeId": "node-b", "status": "offline"},
        ],
        "targets": [
            {
                "targetUri": ECHO_TARGET,
                "targetKind": "agent",
                "status": "online",
                "hosted": True,
                "hostedBy": "node-a",
            }
        ],
        "counts": {"nodes": 2, "targets": 1},
        "truncated": {"nodes": False, "targets": False},
    }
    assert recorder.calls == [
        ("hosts", {}),
        ("targets", {"kind": "agent"}),
    ]


def test_targets_list_caps_rows_and_names_the_truncation() -> None:
    recorder = Recorder(
        hosts_answer(*(node_row(f"node-{index}") for index in range(205))),
        targets_answer(
            *(agent_row(f"agent:example/agent-{index}") for index in range(201))
        ),
    )
    adapter = make_adapter(recorder)

    directory = adapter.targets_list()

    assert len(directory["nodes"]) == 200
    assert len(directory["targets"]) == 200
    assert directory["counts"] == {"nodes": 205, "targets": 201}
    assert directory["truncated"] == {"nodes": True, "targets": True}
    assert directory["targets"][0]["targetUri"] == "agent:example/agent-0"
    assert directory["targets"][-1]["targetUri"] == "agent:example/agent-199"


@pytest.mark.parametrize(
    "answer",
    [
        "not-a-mapping",
        {},
        {"hosts": {}},
        {"hosts": [7]},
        {"hosts": [{"status": "online"}]},
        {"hosts": [{"nodeId": "", "status": "online"}]},
        {"hosts": [{"nodeId": "a", "status": ""}]},
        {"hosts": [{"nodeId": "a" * 257, "status": "online"}]},
        {"hosts": [node_row(), node_row()]},
    ],
)
def test_targets_list_malformed_hosts_answers_are_unavailable(answer: object) -> None:
    recorder = Recorder(answer)
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterUnavailable):
        adapter.targets_list()

    assert recorder.calls == [("hosts", {})]


@pytest.mark.parametrize(
    "answer",
    [
        "not-a-mapping",
        {},
        {"targets": {}},
        {"targets": [7]},
        targets_answer(agent_row(targetKind="user")),
        targets_answer(agent_row(deliverable=False)),
        targets_answer(agent_row(deliverable="yes")),
        targets_answer(agent_row(status="")),
        targets_answer(agent_row(targetUri="")),
        targets_answer(agent_row(targetUri="a" * 513)),
        targets_answer(agent_row(), agent_row()),
        targets_answer(agent_row(hosted="yes")),
        targets_answer(agent_row(hostedBy=7)),
        targets_answer(agent_row(hostedBy="b" * 513)),
    ],
)
def test_targets_list_malformed_or_non_agent_answers_are_unavailable(
    answer: object,
) -> None:
    recorder = Recorder(hosts_answer(node_row()), answer)
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterUnavailable):
        adapter.targets_list()

    assert [method for method, _ in recorder.calls] == ["hosts", "targets"]


# --- sessions.create --------------------------------------------------------


def test_create_session_derives_stable_ids_and_reuses_them_without_a_second_authority() -> None:
    recorder = Recorder(
        targets_answer(agent_row()),
        targets_answer(agent_row()),
        targets_answer(agent_row()),
    )
    adapter = make_adapter(recorder)
    expected_session, expected_conversation = deterministic_ids(ECHO_TARGET)

    first = adapter.create_session(ECHO_TARGET)
    second = adapter.create_session(ECHO_TARGET)

    assert first == {
        "sessionId": expected_session,
        "targetUri": ECHO_TARGET,
        "status": "online",
        "created": True,
    }
    assert second == {**first, "created": False}
    assert adapter.sessions_list() == [
        {"sessionId": "s-1"},
        {
            "sessionId": expected_session,
            "title": ECHO_TARGET,
            "targetUri": ECHO_TARGET,
        },
    ]
    assert adapter.session_binding(expected_session).targets == (ECHO_TARGET,)
    assert (
        adapter.session_binding(expected_session).conversation_id
        == expected_conversation
    )

    # A restart re-derives the same binding from the fixed sender + target:
    # history survives without persisting a second identity authority.
    restarted = make_adapter(Recorder(targets_answer(agent_row())), sessions={})
    assert restarted.create_session(ECHO_TARGET)["sessionId"] == expected_session
    assert (
        restarted.session_binding(expected_session).conversation_id
        == expected_conversation
    )


def test_create_session_reuses_a_configured_single_target_binding() -> None:
    recorder = Recorder(targets_answer(agent_row(MINI_TARGET)))
    adapter = make_adapter(recorder)

    created = adapter.create_session(MINI_TARGET)

    assert created == {
        "sessionId": "s-1",
        "targetUri": MINI_TARGET,
        "status": "online",
        "created": False,
    }
    assert adapter.sessions_list() == [{"sessionId": "s-1"}]


def test_create_session_refuses_an_unlisted_target_without_mutating_bindings() -> None:
    recorder = Recorder(targets_answer(agent_row(MINI_TARGET)))
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.create_session(ECHO_TARGET)

    assert info.value.code == "unknown-target"
    assert info.value.details == {"targetUri": ECHO_TARGET}
    assert adapter.sessions_list() == [{"sessionId": "s-1"}]
    assert recorder.calls == [("targets", {"kind": "agent"})]


@pytest.mark.parametrize(
    "target_uri",
    ["", "   ", 7, None, ["agent:x"], "a" * 513],
)
def test_create_session_rejects_a_bad_target_uri_before_any_daemon_call(
    target_uri: object,
) -> None:
    recorder = Recorder(targets_answer(agent_row()))
    adapter = make_adapter(recorder)

    with pytest.raises(AppAdapterError) as info:
        adapter.create_session(target_uri)

    assert info.value.code == "invalid-argument"
    assert recorder.calls == []


def test_an_ensured_session_sends_and_reads_history_on_its_derived_conversation() -> None:
    expected_session, expected_conversation = deterministic_ids(ECHO_TARGET)
    recorder = Recorder(
        targets_answer(agent_row()),
        {
            "ok": True,
            "operationId": "op-ensured",
            "conversationId": expected_conversation,
            "deliveries": [
                {
                    "target": ECHO_TARGET,
                    "messageId": "m-ensured",
                    "accepted": True,
                    "queued": False,
                }
            ],
            "messageId": "m-ensured",
        },
        {
            "ok": True,
            "conversationId": expected_conversation,
            "entries": [
                history_entry(
                    messageId="m-ensured",
                    conversationId=expected_conversation,
                    **{"from": SENDER, "to": ECHO_TARGET},
                )
            ],
            "nextCursor": None,
        },
    )
    adapter = make_adapter(recorder)
    adapter.create_session(ECHO_TARGET)

    ack = adapter.send(expected_session, {"text": "hello", "idempotencyKey": "k-1"})
    page = adapter.history(expected_session)

    assert ack["status"] == "accepted"
    assert recorder.calls[1] == (
        "message.send",
        {
            "from": SENDER,
            "to": [ECHO_TARGET],
            "message": "hello",
            "summary": "hello",
            "conversationId": expected_conversation,
            "idempotencyKey": "k-1",
        },
    )
    assert recorder.calls[2] == (
        "message.history",
        {
            "from": SENDER,
            "conversationId": expected_conversation,
            "limit": 50,
        },
    )
    assert [event["eventId"] for event in page["events"]] == ["m-ensured"]
