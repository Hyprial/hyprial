"""The fenced PAC write surface (G1=A) as the daemon enforces it.

Design §5.1, matrix locked by Allen 2026-09-14: the ONLY permitted cell is
「verified caller principal URI == node.owner, byte for byte」.  The daemon
methods ``pac.flag.set/reset``, ``pac.graph.activate/close`` and
``pac.actor.stop`` authenticate the caller through the daemon's own session
binding (actor + sessionRef naming a live interactive/worker session) and
apply the owner-only comparison against THAT identity -- a presented actor
without the binding is a claim, never a credential (T12), and the matrix's
every rejection direction is asserted through the real handler (T13).
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.pac.graphs.edits import add_node, create_graph
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

OWNER = "acme"
MACHINE = "hyprial-node"
KANBAN = f"agent:{OWNER}:{MACHINE}:kanban"
KANBAN_ELSEWHERE = f"agent:{OWNER}:far-away.example:kanban"
SIBLING = f"agent:{OWNER}:{MACHINE}:other-bot"
HUMAN = f"user:{OWNER}"


class Harnesses:
    def status(self) -> tuple[()]:
        return ()


class Presence:
    def actor_online(self, _actor: str) -> bool:
        return True

    def online_actors(self) -> tuple[()]:
        return ()

    def online_mailboxes(self) -> tuple[()]:
        return ()


@pytest.fixture()
def app(tmp_path: Path) -> DaemonApplication:
    state = tmp_path / "daemon-state"
    application = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id=MACHINE,
        owner=OWNER,
        hyprial_home=tmp_path / "daemon-home",
    **_deep_module_daemon_dependencies())
    application._harnesses = Harnesses()  # type: ignore[assignment]
    application._presence = Presence()  # type: ignore[assignment]
    application._inbox = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport()
    )
    application._pac_notification_io = None  # sender failure is durable, never silent
    yield application
    # close the daemon-held stores so per-test fd hygiene sees a clean slate
    application._inbox.close()
    application._agent_session_domains.close()


def _register_session(app: DaemonApplication, actor: str, ref: str) -> None:
    registered = app.handle(
        "session.register",
        {
            "actor": actor,
            "sessionRef": ref,
            "cwd": "/work",
            "command": ["hyprial-test-carrier", "--actor", actor],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )
    assert isinstance(registered, dict) and registered.get("actor"), registered


def _call(app: DaemonApplication, method: str, **params: Any) -> Any:
    try:
        return app.handle(method, params)
    except Exception as error:  # DaemonRequestError carries code+message
        return error


def _code(outcome: Any) -> str:
    return getattr(outcome, "code", None) or ""


@pytest.fixture()
def graph(app: DaemonApplication) -> tuple[DaemonApplication, PacGraphStore, str]:
    """One active graph; nodes named per the matrix cell they serve."""

    store = PacGraphStore(default_database_path(app.state_dir))
    try:
        graph_id = create_graph(store, name="fence", created_by=KANBAN)["graphId"]
        version = 1
        for node_id, owner in (
            ("own", KANBAN),  # the one permitted cell
            ("sibling", SIBLING),  # same owner, different actor
            ("cross-machine", KANBAN_ELSEWHERE),  # same name, foreign machine
            ("human", HUMAN),  # a person's node
        ):
            version = add_node(
                store,
                app.state_dir,
                graph_id=graph_id,
                node_id=node_id,
                owner=owner,
                brief_ref=f"r#{node_id}",
                expect_version=version,
            )["version"]
        from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge

        add_edge(
            store,
            graph_id=graph_id,
            from_node="own",
            to_node="sibling",
            kind="forward",
            expect_version=version,
        )
        _register_session(app, KANBAN, "session-1")
        activate_graph(store, graph_id, actor=KANBAN)
        yield app, store, graph_id
    finally:
        store.close()


def _flag_facts(store: PacGraphStore, graph_id: str) -> list[tuple[str, str, bool]]:
    return sorted(
        (node.node_id, node.owner, bool(node.flag))
        for node in store.nodes(graph_id)
    )


# --------------------------------------------------------------------------- #
# T12: no binding, partial binding, claim-only
# --------------------------------------------------------------------------- #


def test_flag_methods_require_the_session_binding(app: DaemonApplication) -> None:
    _register_session(app, KANBAN, "session-1")
    # no actor/sessionRef at all
    denied = _call(app, "pac.flag.set", graphId="missing", nodeId="x")
    assert _code(denied) == "CALLER_NOT_AUTHORIZED"
    # actor without sessionRef is a claim, not a credential
    denied = _call(app, "pac.flag.set", graphId="missing", nodeId="x", actor=KANBAN)
    assert _code(denied) == "CALLER_NOT_AUTHORIZED"
    # sessionRef without actor
    denied = _call(app, "pac.flag.set", graphId="missing", nodeId="x", sessionRef="s")
    assert _code(denied) == "CALLER_NOT_AUTHORIZED"


@pytest.mark.parametrize(
    "actor,session_ref",
    [
        (SIBLING, "session-1"),
        (KANBAN, "dead-session"),
        (KANBAN, "session-for-sibling"),
    ],
)
def test_workflow_rejects_forged_or_stale_interactive_binding_as_unverified(
    app: DaemonApplication, actor: str, session_ref: str
) -> None:
    _register_session(app, KANBAN, "session-1")
    _register_session(app, SIBLING, "session-for-sibling")

    denied = _call(
        app,
        "workflow.plan",
        yaml="version: 2\nname: no-op\nnodes: []\n",
        actor=actor,
        sessionRef=session_ref,
    )

    assert _code(denied) == "PAC_PRINCIPAL_UNVERIFIED"


def test_workflow_rejects_registered_session_after_liveness_release(
    app: DaemonApplication,
) -> None:
    _register_session(app, KANBAN, "session-1")
    app._agent_liveness.release(KANBAN)

    denied = _call(
        app,
        "workflow.plan",
        yaml="version: 2\nname: no-op\nnodes: []\n",
        actor=KANBAN,
        sessionRef="session-1",
    )

    assert _code(denied) == "PAC_PRINCIPAL_UNVERIFIED"


def test_workflow_keeps_managed_worker_session_authority(
    app: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = f"agent:{OWNER}:{MACHINE}:managed"
    app.handle("agent.create", {"name": "managed"})
    monkeypatch.setattr(
        app,
        "_worker_session_ref",
        lambda actor: "worker-session" if actor == worker else None,
    )

    outcome = _call(
        app,
        "workflow.plan",
        yaml="version: 2\nname: no-op\nnodes: []\n",
        actor=worker,
        sessionRef="worker-session",
    )

    assert _code(outcome) == "WORKFLOW_SCHEMA_ERROR"


def test_unregistered_session_ref_is_not_authorized(graph) -> None:
    app, store, graph_id = graph
    denied = _call(
        app,
        "pac.flag.set",
        graphId=graph_id,
        nodeId="own",
        actor=KANBAN,
        sessionRef="never-registered",
    )
    assert _code(denied) == "CALLER_NOT_AUTHORIZED"
    assert store.node(graph_id, "own").flag is False


def test_old_generation_session_ref_is_rejected(graph) -> None:
    """hyprial-developer acceptance 1: the binding is checked against the LIVE
    session registry.  After a superseding register, the OLD sessionRef must
    be refused -- env is not just a place to store the --actor string."""
    app, store, graph_id = graph
    _register_session(app, KANBAN, "session-old")
    # a newer session of the same actor supersedes it (last-writer-wins)
    _register_session(app, KANBAN, "session-new")
    denied = _call(
        app,
        "pac.flag.set",
        graphId=graph_id,
        nodeId="own",
        actor=KANBAN,
        sessionRef="session-old",
    )
    assert _code(denied) in {"CALLER_NOT_AUTHORIZED", "SESSION_SUPERSEDED"}
    assert store.node(graph_id, "own").flag is False
    # the CURRENT binding still works
    allowed = _call(
        app,
        "pac.flag.set",
        graphId=graph_id,
        nodeId="own",
        actor=KANBAN,
        sessionRef="session-new",
    )
    assert isinstance(allowed, dict) and allowed.get("ok") is True
    assert store.node(graph_id, "own").flag is True


# --------------------------------------------------------------------------- #
# T13: the G1=A double-sided matrix through the real handler
# --------------------------------------------------------------------------- #


def test_the_verified_exact_uri_is_the_only_permitted_cell(graph) -> None:
    app, store, graph_id = graph
    outcome = _call(
        app,
        "pac.flag.set",
        graphId=graph_id,
        nodeId="own",
        actor=KANBAN,
        sessionRef="session-1",
    )
    assert isinstance(outcome, dict) and outcome.get("ok") is True
    node = store.node(graph_id, "own")
    assert node.flag is True and node.flag_set_by == KANBAN
    # reset is the same rule
    outcome = _call(
        app,
        "pac.flag.reset",
        graphId=graph_id,
        nodeId="own",
        actor=KANBAN,
        sessionRef="session-1",
    )
    assert isinstance(outcome, dict) and outcome.get("ok") is True
    assert store.node(graph_id, "own").flag is False


@pytest.mark.parametrize(
    "actor_name,node_id",
    [
        ("sibling", "own"),  # agent -> same owner segment, different actor
        ("sibling", "cross-machine"),  # agent -> same name on a foreign machine
        ("sibling", "human"),  # agent -> a person's node
        ("kanban", "sibling"),  # the mirror: the other agent's own node
    ],
)
def test_every_other_direction_is_refused_with_zero_side_effects(
    graph, actor_name, node_id
) -> None:
    app, store, graph_id = graph
    sessions = {
        "sibling": (SIBLING, "sibling-session"),
        "kanban": (KANBAN, "session-1"),
    }
    actor, session_ref = sessions[actor_name]
    _register_session(app, actor, session_ref)
    denied = _call(
        app,
        "pac.flag.set",
        graphId=graph_id,
        nodeId=node_id,
        actor=actor,
        sessionRef=session_ref,
    )
    assert _code(denied) == "PAC_FLAG_NOT_OWNER", denied
    facts = _flag_facts(store, graph_id)
    assert not any(flag for _, _, flag in facts), "no flag moved on refusal"


def test_refusals_leave_no_journal_or_outbox_side_effects(graph) -> None:
    app, store, graph_id = graph
    _register_session(app, SIBLING, "sibling-session")
    before = [event["seq"] for event in store.journal_events(graph_id)]
    _call(
        app,
        "pac.flag.set",
        graphId=graph_id,
        nodeId="own",
        actor=SIBLING,
        sessionRef="sibling-session",
    )
    _call(
        app,
        "pac.flag.reset",
        graphId=graph_id,
        nodeId="own",
        actor=SIBLING,
        sessionRef="sibling-session",
    )
    assert [event["seq"] for event in store.journal_events(graph_id)] == before
    assert store.notifications(graph_id) == []


def test_graph_lifecycle_shares_the_fence_and_the_owner_check(graph) -> None:
    app, store, graph_id = graph
    _register_session(app, SIBLING, "sibling-session")
    # a different agent cannot close the creator's graph
    denied = _call(
        app,
        "pac.graph.close",
        graphId=graph_id,
        actor=SIBLING,
        sessionRef="sibling-session",
    )
    assert _code(denied) in {"PAC_GRAPH_NOT_OWNER", "PAC_FLAG_NOT_OWNER"}, denied
    # without any binding, no lifecycle write either
    denied = _call(app, "pac.graph.close", graphId=graph_id)
    assert _code(denied) == "CALLER_NOT_AUTHORIZED"
    # the graph is still active
    assert store.graph(graph_id)["closed_at"] is None


def test_daemon_passes_pac_error_codes_through_verbatim(graph) -> None:
    """One code space across the local (human) and fenced (agent) paths."""
    app, _store, graph_id = graph
    denied = _call(
        app,
        "pac.flag.set",
        graphId="no-such-graph",
        nodeId="own",
        actor=KANBAN,
        sessionRef="session-1",
    )
    # PacError(PAC_GRAPH_NOT_FOUND) rides DaemonRequestError with its code
    assert _code(denied) == "PAC_GRAPH_NOT_FOUND"


def test_flag_turn_notification_targets_the_owner_uri(graph) -> None:
    """§5.2 through the fence: the planned notification recipient is the
    downstream owner's full URI (delivery failure is durable, never silent)."""
    app, store, graph_id = graph
    outcome = _call(
        app,
        "pac.flag.set",
        graphId=graph_id,
        nodeId="own",
        actor=KANBAN,
        sessionRef="session-1",
    )
    assert isinstance(outcome, dict) and outcome.get("ok") is True
    [planned] = store.notifications(graph_id)
    assert planned.recipient == SIBLING
    assert planned.message_id is None  # typed IO is None in this fixture: durable
    assert outcome["undelivered"] == 1
