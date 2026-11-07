import pytest
from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.daemon.impl.pac.workflows.outputs import MAX_WORKFLOW_OUTPUT_TEXT_BYTES

from ..support import (
    AGENT,
    USER,
    bound_message,
    complete,
    service as service,
    start,
)


def test_failure_inline_output_survives_reopen_and_reaches_graph_sender(service):
    from types import SimpleNamespace

    from hyprial.daemon.impl.dispatch.workflow_observation import observe_node

    svc, sender, _ = service
    graph = start(
        svc,
        [{"id": "review", "task": "review it", "owner": AGENT}],
        policy="hold",
    )
    svc._tick()
    request = svc.status(run_id=graph)["nodes"][0]["requestId"]
    verdict = "NEEDS-CHANGES\n1. The durable result is missing."

    svc.fail(
        graph_id=graph,
        node_id="review",
        actor=AGENT,
        request_id=request,
        reason_ref="output:review",
        output_text=verdict,
    )

    reopened = GraphWorkflowService(
        state_dir=svc.database.parent,
        owner="test",
        machine="machine",
        sender=sender,
        clock_ms=svc.clock,
        start_thread=False,
    )
    try:
        node = reopened.status(run_id=graph)["nodes"][0]
        assert node["outputText"] == verdict
        inspected = observe_node(
            reopened.database,
            reopened.status(run_id=graph),
            node,
            SimpleNamespace(list_progress_events=lambda *args, **kwargs: ()),
            recipient="agent:test:machine:dispatch-coordinator",
            at=2000,
            epoch="test",
        )
        assert inspected["results"]["outputText"] == verdict
    finally:
        reopened.close()
    assert any(
        item["recipient"] == USER and verdict in item["text"]
        for item in sender.messages.values()
    )


def test_interactive_agent_creator_receives_completion_output_not_machine_owner(service):
    svc, sender, _ = service
    creator = "agent:test:machine:interactive-creator"
    graph = svc.start(
        yaml_text=(
            "version: 2\n"
            "name: interactive-creator\n"
            f"nodes: [{{id: work, task: do it, owner: {AGENT}}}]\n"
        ),
        sender=creator,
        operation_key="interactive-creator",
    )["graphId"]
    svc._tick()
    node = svc.status(run_id=graph)["nodes"][0]

    store = PacGraphStore(svc.database)
    try:
        PacReactor(store, clock=svc.clock).set_flag(
            graph,
            "work",
            actor=AGENT,
            expected_request=node["requestId"],
            reason_ref="output:interactive",
            output_text="interactive result",
        )
    finally:
        store.close()
    svc._tick()

    assert svc.status(run_id=graph)["sender"] == creator
    completion = next(
        item for item in sender.messages.values() if "interactive result" in item["text"]
    )
    assert completion["recipient"] == creator
    assert completion["recipient"] != USER


def test_completion_inline_output_is_request_bound_and_not_truncated(service):
    svc, sender, _ = service
    graph = start(svc, [{"id": "work", "task": "do it", "owner": AGENT}])
    svc._tick()
    node = svc.status(run_id=graph)["nodes"][0]
    output = "completed output\n" + ("x" * 4096)
    store = PacGraphStore(svc.database)
    try:
        PacReactor(store, clock=svc.clock).set_flag(
            graph,
            "work",
            actor=AGENT,
            expected_request=node["requestId"],
            reason_ref="output:work",
            output_text=output,
        )
    finally:
        store.close()
    svc._tick()

    assert svc.status(run_id=graph)["nodes"][0]["outputText"] == output
    assert any(
        item["recipient"] == USER and output in item["text"]
        for item in sender.messages.values()
    )


def test_oversized_inline_output_is_refused_without_mutation(service):
    svc, _, _ = service
    graph = start(svc, [{"id": "work", "task": "do it", "owner": AGENT}])
    svc._tick()
    node = svc.status(run_id=graph)["nodes"][0]
    store = PacGraphStore(svc.database)
    try:
        with pytest.raises(PacError, match="maximum.*65536"):
            PacReactor(store, clock=svc.clock).set_flag(
                graph,
                "work",
                actor=AGENT,
                expected_request=node["requestId"],
                reason_ref="output:work",
                output_text="x" * (MAX_WORKFLOW_OUTPUT_TEXT_BYTES + 1),
            )
    finally:
        store.close()
    unchanged = svc.status(run_id=graph)["nodes"][0]
    assert unchanged["flag"] is False
    assert "outputText" not in unchanged


def test_stale_completion_is_refused_after_upstream_aba_even_before_tick(service):
    svc, _, _ = service
    graph = start(
        svc,
        [
            {"id": "a", "task": "a", "owner": AGENT},
            {"id": "b", "task": "b", "owner": AGENT, "after": ["a"]},
        ],
        policy="hold",
    )
    svc._tick()
    complete(svc, graph, "a")
    old_request = next(
        n for n in svc.status(run_id=graph)["nodes"] if n["nodeId"] == "b"
    )["requestId"]
    store = PacGraphStore(svc.database)
    try:
        reactor = PacReactor(store, clock=svc.clock)
        reactor.reset_flag(graph, "a", actor=AGENT)
        with pytest.raises(PacError, match="currently requested"):
            reactor.set_flag(
                graph,
                "b",
                actor=AGENT,
                expected_request=old_request,
                output_text="stale output",
            )
        svc._tick()
        complete(svc, graph, "a")
        with pytest.raises(PacError, match="currently requested"):
            reactor.set_flag(graph, "b", actor=AGENT, expected_request=old_request)
        fresh = next(
            n for n in svc.status(run_id=graph)["nodes"] if n["nodeId"] == "b"
        )["requestId"]
        assert fresh != old_request
        assert "outputText" not in next(
            n for n in svc.status(run_id=graph)["nodes"] if n["nodeId"] == "b"
        )
    finally:
        store.close()


def test_flag_without_request_cannot_bypass_workflow_completion_fence(service):
    svc, _, _ = service
    graph = start(svc, [{"id": "a", "task": "a", "owner": AGENT}])
    svc._tick()
    store = PacGraphStore(svc.database)
    try:
        with pytest.raises(PacError) as error:
            PacReactor(store, clock=svc.clock).set_flag(graph, "a", actor=AGENT)
        assert error.value.code == "WORKFLOW_REQUEST_STALE"
        assert store.node(graph, "a").flag is False
    finally:
        store.close()


def test_transitive_stale_approval_cannot_authorize_downstream_completion(service):
    svc, _, _ = service
    graph = start(
        svc,
        [
            {"id": "a", "task": "a", "owner": AGENT},
            {"id": "b", "task": "review a", "owner": AGENT, "after": ["a"]},
            {"id": "c", "task": "accept b", "owner": AGENT, "after": ["b"]},
        ],
    )
    svc._tick()
    complete(svc, graph, "a")
    complete(svc, graph, "b")
    old = next(n for n in svc.status(run_id=graph)["nodes"] if n["nodeId"] == "c")[
        "requestId"
    ]
    store = PacGraphStore(svc.database)
    try:
        reactor = PacReactor(store, clock=svc.clock)
        reactor.reset_flag(graph, "a", actor=AGENT)
        with pytest.raises(PacError) as error:
            reactor.set_flag(graph, "c", actor=AGENT, expected_request=old)
        assert error.value.code == "WORKFLOW_REQUEST_STALE"
        svc._tick()
        complete(svc, graph, "a")
        snapshot = svc.status(run_id=graph)
        assert snapshot["state"] == "running"
        assert (
            next(n for n in snapshot["nodes"] if n["nodeId"] == "b")["state"] == "stale"
        )
        assert (
            next(n for n in snapshot["nodes"] if n["nodeId"] == "c")["state"]
            == "pending"
        )
        reactor.reset_flag(graph, "b", actor=AGENT)
        svc._tick()
        complete(svc, graph, "b")
        fresh = next(
            n for n in svc.status(run_id=graph)["nodes"] if n["nodeId"] == "c"
        )["requestId"]
        assert fresh != old
        complete(svc, graph, "c")
        assert svc.status(run_id=graph)["state"] == "completed"
    finally:
        store.close()


def test_back_edge_delivers_explicit_rework_without_resetting_other_owners(service):
    svc, sender, _ = service
    graph = start(
        svc,
        [
            {"id": "build", "task": "build", "owner": AGENT},
            {
                "id": "review",
                "task": "review",
                "owner": USER,
                "kind": "approval",
                "after": ["build"],
            },
            {
                "id": "reject",
                "task": "request rework",
                "owner": USER,
                "kind": "approval",
                "after": ["review"],
            },
            {
                "id": "accept",
                "task": "accept",
                "owner": USER,
                "kind": "end",
                "after": ["review"],
            },
        ],
        edges=[{"from": "reject", "to": "build", "kind": "back"}],
    )
    svc._tick()
    complete(svc, graph, "build")
    complete(svc, graph, "review")
    complete(svc, graph, "reject")
    assert any(
        "Explicit rework" in m["text"] and m["recipient"] == AGENT
        for m in sender.messages.values()
    )
    store = PacGraphStore(svc.database)
    try:
        assert store.node(graph, "build").flag is True
        assert store.graph(graph)["closed_at"] is None
    finally:
        store.close()
    count = len(sender.messages)
    svc._tick()
    assert len(sender.messages) == count


def test_native_completed_turn_is_not_completion_and_failed_turn_does_not_retry(
    service,
):
    svc, sender, _ = service
    graph = start(svc, [{"id": "work", "task": "work", "owner": AGENT}])
    svc._tick()
    message = bound_message(svc)
    assert svc.record_harness_outcome(message_id=message, recipient=AGENT, failed=False)
    assert svc.status(run_id=graph)["state"] == "running"
    assert not svc.status(run_id=graph)["nodes"][0]["flag"]
    assert svc.record_harness_outcome(
        message_id=message,
        recipient=AGENT,
        failed=True,
        failure_code="PROVIDER_AUTHENTICATION_FAILED",
    )
    svc._tick()
    assert svc.status(run_id=graph)["state"] == "failed"
    count = len(sender.messages)
    svc._tick()
    assert len(sender.messages) == count


def test_node_observation_reads_only_correlated_progress_and_never_completes_work(
    service,
):
    from types import SimpleNamespace
    from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent, encode_progress_event
    from hyprial.daemon.impl.dispatch.workflow_observation import observe_node

    svc, _, _ = service
    graph_id = start(svc, [{"id": "work", "owner": AGENT, "task": "work"}])
    svc._tick()
    snapshot = svc.status(run_id=graph_id)
    node = snapshot["nodes"][0]
    recipient = "agent:test:machine:dispatch-coordinator"
    event = ProgressEvent(
        node["messageId"],
        "pac-" + graph_id,
        AGENT,
        "codex",
        "tool",
        0,
        1000,
        "Read the specification",
    )
    # The fixture sender returns its effect key; the real durable binding is
    # the inbox UUID reserved before IO, so progress uses that binding.
    message_id = bound_message(svc)
    event = ProgressEvent(
        message_id,
        "pac-" + graph_id,
        AGENT,
        "codex",
        "tool",
        0,
        1000,
        "Read the specification",
    )
    valid = SimpleNamespace(
        payload=encode_progress_event(event),
        sender=AGENT,
        recipient=recipient,
        conversation_id="pac-" + graph_id,
    )
    forged = SimpleNamespace(
        payload=valid.payload,
        sender="agent:test:machine:other",
        recipient=recipient,
        conversation_id=valid.conversation_id,
    )
    inbox = SimpleNamespace(
        list_progress_events=lambda *args, **kwargs: (valid, forged)
    )
    result = observe_node(
        svc.database, snapshot, node, inbox, recipient=recipient, at=2000, epoch="test"
    )
    assert [e["summary"] for e in result["progress"]["events"]] == [
        "Read the specification"
    ]
    assert result["requestId"] == node["requestId"]
    assert result["results"]["evidenceRef"] is None
    assert svc.status(run_id=graph_id) == snapshot
