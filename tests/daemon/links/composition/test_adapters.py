"""WP2-B3 adapters: org spaces, presence, inbox, PAC, identity, PAC binder.

Spec: docs/design/send-links-impl/wp2-composition.md §2, §3, §6, §10.3.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.composition.links import (
    FacadeLinkInboxPort,
    LocalIdentity,
    OrgLinkSpaces,
    PacLifecyclePort,
    PresencePort,
    SPACE_REFRESH_MS,
    pac_task_binder,
)
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.org.network.directory import InvalidDeviceQuarantine
from hyprial.daemon.impl.pac.actors.coordinator import LaunchSpec
from hyprial.daemon.impl.pac.actors.daemon import DaemonActorRuntime
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, OutboxItem
from hyprial.daemon.impl.inbox.contracts.api import (
    LinkInboundRow,
    LinkLedgerResult,
    LinkLedgerRow,
)
from hyprial.daemon.impl.links.store import (
    CensoredRecord,
    LedgerResult,
    LedgerRow,
    LedgerUpdate,
)
from hyprial.kernel import canonical_user_uri
from tests.support.links.app import OrgFsWithMeta, stub_app
from tests.support.links.org import FakeClock, LogRecorder, make_service


def _space(fs: OrgFsWithMeta, space_id: str, name: str) -> None:
    fs.add_space(space_id, name=name)


# -- A1 -------------------------------------------------------------------


def test_org_link_spaces_is_reexported_from_dedicated_module() -> None:
    from hyprial.daemon.impl.composition.link_spaces import (
        OrgLinkSpaces as ExtractedOrgLinkSpaces,
    )

    assert OrgLinkSpaces is ExtractedOrgLinkSpaces


def test_org_link_spaces_keeps_legacy_dependency_patch_targets(
    tmp_path: Path, monkeypatch
) -> None:
    import hyprial.daemon.impl.composition.links as links_module

    calls: list[tuple[str, object]] = []

    class EmptyStore:
        def __init__(self, fs, **_kwargs) -> None:
            calls.append(("store", fs))

        def orgs(self) -> tuple[str, ...]:
            return ()

    def read_left_orgs(home: Path) -> list[str]:
        calls.append(("left", home))
        return []

    fs = OrgFsWithMeta()
    monkeypatch.setattr(links_module, "OrgFsDirectoryStore", EmptyStore)
    monkeypatch.setattr(links_module, "read_left_orgs", read_left_orgs)

    spaces = OrgLinkSpaces(
        fs,
        home=tmp_path,
        clock_ms=FakeClock(),
        invalid_devices=InvalidDeviceQuarantine(),
    )

    assert spaces() == ()
    assert calls == [("store", fs), ("left", tmp_path)]


def test_org_link_spaces_resolves_left_deleted_and_ambiguous(tmp_path: Path) -> None:
    fs = OrgFsWithMeta()
    for space_id, name in (
        ("sp-a", "group-a"),
        ("sp-b", "group-b"),
        ("sp-left", "group-left"),
        ("sp-gone", "group-gone"),
        ("sp-dup1", "group-dup"),
        ("sp-dup2", "group-dup"),
        ("sp-acl", "group-x-acl"),
        ("sp-plain", "not-an-org"),
    ):
        _space(fs, space_id, name)
    fs.mkdir("sp-gone", "directory")
    fs.put_text(
        "sp-gone", "directory/org.json", json.dumps({"state": "deleted"})
    )
    home = tmp_path / "home"
    (home / "state" / "tailcat").mkdir(parents=True)
    (home / "state" / "tailcat" / "left-orgs.json").write_text(
        json.dumps(["group-left"]), encoding="utf-8"
    )
    logs = LogRecorder()
    clock = FakeClock()
    spaces = OrgLinkSpaces(
        fs,
        home=home,
        clock_ms=clock,
        invalid_devices=InvalidDeviceQuarantine(),
        logger=logs,
    )

    from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore

    assert OrgFsDirectoryStore(fs).org_state("group-gone") == "deleted"
    assert spaces() == ("sp-a", "sp-b")
    assert logs.count("link.space_ambiguous") == 1

    # Cached for SPACE_REFRESH_MS: a second call must not rescan.
    before = fs.spaces_calls
    assert spaces() == ("sp-a", "sp-b")
    assert fs.spaces_calls == before
    clock.advance(SPACE_REFRESH_MS)
    assert spaces() == ("sp-a", "sp-b")
    assert fs.spaces_calls > before

    fs.drop_space("sp-a")
    fs.drop_space("sp-b")
    clock.advance(SPACE_REFRESH_MS)
    assert spaces() == ()
    assert logs.count("link.org_unavailable") == 1


def test_org_link_spaces_reads_deleted_marker_before_legacy_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    fs = OrgFsWithMeta()
    _space(fs, "sp-deleted", "group-deleted")
    fs.mkdir("sp-deleted", "directory")
    fs.put_text(
        "sp-deleted", "directory/org.json", json.dumps({"state": "deleted"})
    )

    def unreadable_meta(_space_id: str) -> dict[str, object]:
        raise OSError("metadata unavailable")

    monkeypatch.setattr(fs, "space_meta", unreadable_meta)
    logs = LogRecorder()
    spaces = OrgLinkSpaces(
        fs,
        home=tmp_path,
        clock_ms=FakeClock(),
        invalid_devices=InvalidDeviceQuarantine(),
        logger=logs,
    )

    assert spaces() == ()
    assert spaces.skipped_count == 1
    assert logs.fields_for("link.space_skipped")[0]["reason"] == "deleted"


def test_org_link_spaces_skips_and_logs_unreadable_legacy_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    fs = OrgFsWithMeta()
    _space(fs, "sp-unreadable", "group-unreadable")

    def unreadable_meta(_space_id: str) -> dict[str, object]:
        raise OSError("metadata unavailable")

    monkeypatch.setattr(fs, "space_meta", unreadable_meta)
    logs = LogRecorder()
    spaces = OrgLinkSpaces(
        fs,
        home=tmp_path,
        clock_ms=FakeClock(),
        invalid_devices=InvalidDeviceQuarantine(),
        logger=logs,
    )

    assert spaces() == ()
    assert spaces.skipped_count == 1
    assert logs.fields_for("link.space_skipped") == [
        {
            "level": "warn",
            "domain": "daemon",
            "org": "group-unreadable",
            "reason": "state-unreadable",
            "errorType": "OSError",
        }
    ]


@pytest.mark.parametrize(
    ("marker", "data_reason"),
    [
        ("{broken", "invalid-json"),
        ("[]", "invalid-object"),
        ("{}", "missing-state"),
        (json.dumps({"state": ""}), "invalid-state"),
    ],
)
def test_org_link_spaces_skips_malformed_lifecycle_marker(
    tmp_path: Path, marker: str, data_reason: str
) -> None:
    fs = OrgFsWithMeta()
    _space(fs, "sp-malformed", "group-malformed")
    fs.mkdir("sp-malformed", "directory")
    fs.put_text("sp-malformed", "directory/org.json", marker)
    logs = LogRecorder()
    spaces = OrgLinkSpaces(
        fs,
        home=tmp_path,
        clock_ms=FakeClock(),
        invalid_devices=InvalidDeviceQuarantine(),
        logger=logs,
    )

    assert spaces() == ()
    assert logs.fields_for("link.space_skipped") == [
        {
            "level": "warn",
            "domain": "daemon",
            "org": "group-malformed",
            "reason": "state-data-problem",
            "dataReason": data_reason,
        }
    ]


@pytest.mark.parametrize("state", ["creating", "partial"])
def test_org_link_spaces_skips_explicit_incomplete_state(
    tmp_path: Path, state: str
) -> None:
    fs = OrgFsWithMeta()
    _space(fs, f"sp-{state}", f"group-{state}")
    fs.mkdir(f"sp-{state}", "directory")
    fs.put_text(
        f"sp-{state}", "directory/org.json", json.dumps({"state": state})
    )
    logs = LogRecorder()
    spaces = OrgLinkSpaces(
        fs,
        home=tmp_path,
        clock_ms=FakeClock(),
        invalid_devices=InvalidDeviceQuarantine(),
        logger=logs,
    )

    assert spaces() == ()
    assert logs.fields_for("link.space_skipped") == [
        {
            "level": "warn",
            "domain": "daemon",
            "org": f"group-{state}",
            "reason": state,
        }
    ]


# -- A2 -------------------------------------------------------------------


class _Presence:
    def raw_online_actors(self) -> tuple[str, ...]:
        return ("node-1", "agent:o:n1:a", "user:o")

    def online_actors(self) -> tuple[str, ...]:  # pragma: no cover
        raise AssertionError("PresencePort must not pay per-actor liveness")


def test_presence_port_filters_to_agent_scheme(tmp_path: Path) -> None:
    assert PresencePort(stub_app(tmp_path, _presence=_Presence())).online_agents() == (
        "agent:o:n1:a",
    )
    assert PresencePort(stub_app(tmp_path, _presence=None)).online_agents() == ()


# -- A3 -------------------------------------------------------------------


def _message(message_id: str, *, payload: bytes) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender="agent:o:n1:a",
        recipient="agent:o:n1:b",
        payload=payload,
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=10,
    )


class _Facade:
    def __init__(self) -> None:
        self.command: object | None = None
        self.rows: dict[tuple[str, str], LinkInboundRow] = {}
        self.held_payload = b'{"replyTo":"m0"}'
        self.held_hold = "LINK_MINTING"
        self.inbound_row_calls: list[tuple[str, str]] = []

    def link_ledger_snapshot(self, now_ms: int) -> tuple[LinkLedgerRow, ...]:
        return (
            LinkLedgerRow(
                actor="agent:o:n1:a",
                message_id="m1",
                direction="out",
                peer="agent:o:n1:b",
                intent="request",
                reply_to=None,
                state="open",
                opened_at_ms=1,
                sent_at_ms=2,
                event_at_ms=None,
                threshold_ms=None,
                sample_pending=True,
                expires_at_ms=99,
            ),
        )

    def link_task_bindings(self) -> tuple[tuple[str, str, str, int], ...]:
        return (("agent:o:n1:a", "agent:o:n1:b", "pac-g1", 1),)

    def link_held_outbox(self) -> tuple[OutboxItem, ...]:
        return (
            OutboxItem(
                message=_message("m2", payload=self.held_payload),
                attempts=0,
                next_attempt_ms=0,
                expires_at_ms=7,
                link_hold=self.held_hold,
            ),
        )

    def link_inbound_live(self, actor: str, peer: str | None = None):
        return (
            LinkInboundRow(
                message_id="m3",
                sender="agent:o:n1:a",
                recipient=actor,
                intent="request",
                reply_to=None,
                received_at_ms=3,
                fetched_at_ms=4,
                injected=True,
                state="open",
                expires_at_ms=100,
            ),
        )

    def link_inbound_row(self, actor: str, message_id: str):
        self.inbound_row_calls.append((actor, message_id))
        return self.rows.get((actor, message_id))

    def update_link_ledger(self, command: object) -> LinkLedgerResult:
        self.command = command
        return LinkLedgerResult(
            released=("r1",), expired=("e1",), failed=("f1",),
            drained=(("agent:o:n1:a", "d1"),), cancelled=("c1",),
        )


def _inbound_row(message_id: str) -> LinkInboundRow:
    return LinkInboundRow(
        message_id=message_id,
        sender="agent:o:n1:a",
        recipient="agent:o:n1:b",
        intent="request",
        reply_to=None,
        received_at_ms=3,
        fetched_at_ms=None,
        injected=False,
        state="open",
        expires_at_ms=100,
    )


def test_facade_port_maps_rows_and_apply(tmp_path: Path) -> None:
    facade = _Facade()
    logs = LogRecorder()
    port = FacadeLinkInboxPort(
        facade,
        local_agents=lambda: ("agent:o:n1:a", "agent:o:n1:b"),
        logger=logs,
    )

    rows = port.ledger_snapshot(1)
    assert rows == (
        LedgerRow(
            actor="agent:o:n1:a",
            message_id="m1",
            direction="out",
            peer="agent:o:n1:b",
            intent="request",
            reply_to=None,
            state="open",
            opened_at_ms=1,
            sent_at_ms=2,
            event_at_ms=None,
            threshold_ms=None,
            sample_pending=True,
            expires_at_ms=99,
        ),
    )
    assert port.task_bindings() == (("agent:o:n1:a", "agent:o:n1:b", "pac-g1", 1),)

    held = port.held_outbox()
    assert held[0].reply_to == "m0"
    assert held[0].target == "agent:o:n1:b"
    assert held[0].hold_reason == "LINK_MINTING"
    assert held[0].created_at_ms == 10
    facade.held_payload = b"not json"
    assert port.held_outbox()[0].reply_to is None

    live = port.inbound_live("agent:o:n1:b")
    assert live[0].message_id == "m3" and live[0].injected is True

    update = LedgerUpdate(
        release=("m2",),
        reasons={"m2": "LINK_MINTING"},
        fail={"m2": "X"},
        drain={"agent:o:n1:a": ("d1",)},
        cancel_outbound={"m2": "LINK_WINDOW_FULL"},
        release_outbound=(("agent:o:n1:a", "m2"),),
        censor=(
            CensoredRecord(
                actor="agent:o:n1:a",
                message_id="m4",
                requester="agent:o:n1:b",
                received_at_ms=5,
                threshold_ms=6,
                expires_at_ms=8,
            ),
        ),
        sampled=(("agent:o:n1:a", "m2"),),
        open_outbound=(
            LedgerRow(
                actor="agent:o:n1:a",
                message_id="m5",
                direction="out",
                peer="agent:o:n1:b",
                intent="request",
                reply_to="m0",
                state="held",
                opened_at_ms=9,
                expires_at_ms=11,
            ),
        ),
        close_inbound=(("agent:o:n1:a", "m3", 9),),
        bind_tasks=(("agent:o:n1:a", "agent:o:n1:b", "pac-g1", 9),),
        unbind_tasks=(("agent:o:n1:a", "agent:o:n1:b", "pac-g0"),),
        purge_before_ms=1,
    )
    result = port.apply(update, now_ms=42)
    assert result == LedgerResult(
        released=("r1",), expired=("e1",), failed=("f1",),
        drained=(("agent:o:n1:a", "d1"),), cancelled=("c1",),
    )
    command = facade.command
    assert command is not None
    assert command.now_ms == 42
    assert command.release == ("m2",)
    assert command.drain == {"agent:o:n1:a": ("d1",)}
    assert command.censor[0].actor == "agent:o:n1:a"
    assert command.censor[0].threshold_ms == 6
    assert command.open_outbound[0].state == "held"
    assert command.open_outbound[0].reply_to == "m0"
    assert command.close_inbound == (("agent:o:n1:a", "m3", 9),)
    assert command.purge_before_ms == 1

    assert port.inbound_row("nope") is None
    facade.rows[("agent:o:n1:a", "m9")] = _inbound_row("m9")
    assert port.inbound_row("m9") is not None
    facade.rows[("agent:o:n1:b", "m9")] = _inbound_row("m9")
    assert port.inbound_row("m9") is None
    assert logs.count("link.inbound_row_ambiguous") == 1


# -- A4 -------------------------------------------------------------------


class _GraphStore:
    def __init__(self, graphs, nodes) -> None:
        self._graphs = graphs
        self._nodes = nodes
        self.closed = 0

    def graph(self, graph_id: str):
        return self._graphs.get(graph_id)

    def nodes(self, graph_id: str):
        return self._nodes.get(graph_id, ())

    def close(self) -> None:
        self.closed += 1


def test_pac_lifecycle_port_states_and_workers(tmp_path: Path) -> None:
    store = _GraphStore(
        {"g1": {"closed_at": None}, "g2": {"closed_at": 5}},
        {"g1": (SimpleNamespace(kind="actor", actor_name="w"),)},
    )
    logs = LogRecorder()
    port = PacLifecyclePort(
        tmp_path / "state", open_store=lambda: store, logger=logs
    )

    assert port.task_states(frozenset({"pac-g1", "pac-g2", "wf-x"})) == {
        "pac-g1": "active",
        "pac-g2": "ended",
        "wf-x": "unknown",
    }
    assert store.closed == 1
    assert port.worker_active("pac-g1", "agent:o:n1:w") is True
    assert port.worker_active("pac-g1", "agent:o:n1:other") is False
    assert port.worker_active("pac-g2", "agent:o:n1:w") is False
    assert port.worker_active("wf-x", "agent:o:n1:w") is None
    assert store.closed == 5

    def boom() -> _GraphStore:
        raise OSError("locked")

    unreadable = PacLifecyclePort(
        tmp_path / "state", open_store=boom, logger=logs
    )
    assert unreadable.task_states(frozenset({"pac-g1"})) == {"pac-g1": "unknown"}
    assert unreadable.worker_active("pac-g1", "agent:o:n1:w") is None
    assert logs.count("link.pac_unreadable") == 1


def test_pac_lifecycle_port_counts_a_borrowed_node_owner_as_active(
    tmp_path: Path,
) -> None:
    # A borrowed owner (an existing agent that owns a task node, no
    # ``_actor`` node of its own) stays active while the graph is open;
    # before the fix it read as False and the ledger reclaimed the pac_task
    # link right after minting (E2E-018 second-arrival handoff).
    store = _GraphStore(
        {"g3": {"closed_at": None}, "g4": {"closed_at": 7}},
        {
            "g3": (
                SimpleNamespace(kind="task", actor_name=None, owner="agent:o:n1:pm"),
                SimpleNamespace(kind="actor", actor_name="w", owner="agent:o:n1:w"),
            ),
            "g4": (
                SimpleNamespace(kind="task", actor_name=None, owner="agent:o:n1:pm"),
            ),
        },
    )
    port = PacLifecyclePort(
        tmp_path / "state", open_store=lambda: store, logger=LogRecorder()
    )
    assert port.worker_active("pac-g3", "agent:o:n1:pm") is True
    assert port.worker_active("pac-g3", "agent:o:n1:w") is True
    assert port.worker_active("pac-g3", "agent:o:n1:stranger") is False
    assert port.worker_active("pac-g4", "agent:o:n1:pm") is False


class _DesiredState:
    """A desired-state double: one nickname marker per harness name."""

    def __init__(self, markers: dict[str, str | None] | None = None) -> None:
        self.markers = dict(markers or {})
        self.calls = 0

    def harness_spec(self, name: str, *, excluding: str | None = None):
        self.calls += 1
        if name not in self.markers:
            return None
        return SimpleNamespace(nickname=self.markers[name], harness="codex")


class _HistoricalGraphStore:
    """A PAC store reporting many closed graphs; owns nothing."""

    def __init__(self, graphs: int) -> None:
        self.graphs = [f"g{index}" for index in range(graphs)]
        self.graph_ids_calls = 0

    def graph_ids(self) -> list[str]:
        self.graph_ids_calls += 1
        return self.graphs

    def workflow_roster(self, graph_id: str):
        return None

    def close(self) -> None:
        pass


def test_pac_lifecycle_port_reads_the_marker_from_desired_state(
    tmp_path: Path,
) -> None:
    """E2E-021 rework: PAC ownership is the desired-state nickname marker.

    The marker is exactly the activation marker PAC persists as ``nickname``
    (``pac:<graph>:<node>:<incarnation>:<effect>``) -- the same fact
    ``DaemonActorRuntime.observe`` reports.  The judgement is a strict parse:
    a nickname that merely looks like a marker is not evidence, and the read
    never opens the PAC store.
    """

    desired = _DesiredState(
        {
            "w": "pac:g1:w:1:4f2c",
            "resident": None,
            # Starts with "pac:" but has no incarnation field.
            "lookalike": "pac:g1:w:4f2c",
            "restarted": "pac:g1:w:2:9a1b",
        }
    )
    opened: list[int] = []

    def open_store():
        opened.append(1)
        raise AssertionError("the ownership judgement must not open PAC")

    port = PacLifecyclePort(
        tmp_path / "state",
        open_store=open_store,
        desired=desired,
        logger=LogRecorder(),
        clock_ms=FakeClock(),
    )

    assert port.is_workflow_worker("agent:o:n1:w") is True
    assert port.is_workflow_worker("agent:o:n1:restarted") is True
    assert port.is_workflow_worker("agent:o:n1:resident") is False
    assert port.is_workflow_worker("agent:o:n1:lookalike") is False
    assert port.is_workflow_worker("agent:o:n1:absent") is False
    assert port.is_workflow_worker("user:o") is False
    assert opened == []

    # No 5 s cache: a worker registered after a read is seen at once.
    desired.markers["late"] = "pac:g2:late:1:0ff1"
    assert port.is_workflow_worker("agent:o:n1:late") is True

    missing = PacLifecyclePort(
        tmp_path / "state", open_store=open_store, logger=LogRecorder()
    )
    # Unknown desired state publishes (resident), never silently skips.
    assert missing.is_workflow_worker("agent:o:n1:w") is False
    assert opened == []


def test_link_tick_does_not_open_the_pac_store_for_ownership(
    tmp_path: Path,
) -> None:
    """The 3000-historical-graph scan must leave the link tick thread."""

    fs = OrgFsWithMeta()
    fs.add_space("s1")
    store = _HistoricalGraphStore(3000)
    opened: list[int] = []
    port = PacLifecyclePort(
        tmp_path / "state",
        open_store=lambda: (opened.append(1), store)[1],
        logger=LogRecorder(),
    )
    h = make_service(fs=fs, pac=port, local_names=("resident",))

    h.tick(1)

    assert opened == [], "the ownership judgement must not open the PAC store"
    assert store.graph_ids_calls == 0


def test_link_tick_skips_a_persisted_desired_state_worker(tmp_path: Path) -> None:
    """End to end: the persisted PAC marker keeps a worker out of the roster.

    The marker is written by ``DaemonActorRuntime._launch_spec`` exactly as
    PAC writes it on start; the real port reads it from a real desired-state
    store and the tick publishes only the resident's empty file, opening no
    PAC store.
    """

    fs = OrgFsWithMeta()
    fs.add_space("s1")
    desired = DesiredStateStore(tmp_path / "home" / "desired-state.json")
    desired.upsert_harness(
        DaemonActorRuntime._launch_spec(
            "worker",
            LaunchSpec("codex", None, None, None, ()),
            "pac:wf-1:worker:1:4f2c",
        )
    )
    opened: list[int] = []
    port = PacLifecyclePort(
        tmp_path / "state",
        desired=desired,
        open_store=lambda: (opened.append(1), _HistoricalGraphStore(3000))[1],
        logger=LogRecorder(),
    )
    h = make_service(fs=fs, pac=port, local_names=("resident", "worker"))

    h.tick(1)

    documents = sorted(node.name for node in fs.listdir("s1", "link"))
    assert documents == ["agent:o:n1:resident.json"]
    assert opened == []


# -- A5 -------------------------------------------------------------------


def test_local_identity_follows_the_registry_not_the_owner() -> None:
    """A hosted agent runs here with a visitor's owner; this node still hosts
    (and must write) its link half.  Locality is this node plus the actors its
    registry speaks for, never the owner segment, while a same-owner agent on
    another node stays remote."""

    logs = LogRecorder()
    identity = LocalIdentity(
        "user:o",
        "n1",
        local=lambda: (
            "agent:o:n1:a",
            "agent:o:n2:a",
            "agent:x:n1:hosted",
            "user:o",
            "agent:o:n1:dispatch-coordinator",
            "agent:o:n1:_turn-hooks",
        ),
        internal_actor_names=frozenset({"dispatch-coordinator", "_turn-hooks"}),
        logger=logs,
    )
    assert identity.is_local("agent:o:n1:a") is True
    assert identity.is_local("agent:o:n2:a") is False
    assert identity.is_local("agent:x:n1:hosted") is True
    assert identity.is_local("agent:y:n1:not-in-registry") is False
    assert identity.is_local("user:o") is False
    assert identity.local_agents() == (
        "agent:o:n1:a",
        "agent:x:n1:hosted",
    )
    assert identity.author_of("agent:o:n1:a") == canonical_user_uri("o")
    # A hosted file is written by the hosting daemon, so its expected author is
    # this node's owner, not the visitor whose name the URI carries.
    assert identity.author_of("agent:x:n1:hosted") == canonical_user_uri("o")
    assert identity.author_of("agent:x:n2:remote") == canonical_user_uri("x")
    assert identity.author_of("nope") is None

    # The orgfs runtime computes the same author for either owner spelling.
    bare = LocalIdentity("o", "n1", local=lambda: ())
    assert bare.author_of("agent:o:n1:a") == canonical_user_uri("o")

    def boom():
        raise RuntimeError("registry down")

    failing = LocalIdentity("o", "n1", local=boom, logger=logs)
    assert failing.local_agents() == ()
    assert failing.local_agents() == ()
    assert logs.count("link.local_agents_failed") == 1


# -- A6 -------------------------------------------------------------------


class _BinderService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []
        self.callback_error = False

    def bind_task(self, *, principal, worker, task_id, now_ms):
        self.calls.append((principal, worker, task_id, now_ms))

        def callback(message_id: str) -> None:
            if self.callback_error:
                raise RuntimeError("callback failed")

        return callback


def test_pac_task_binder_shape_and_failures(tmp_path: Path) -> None:
    aliases = {
        "p": "agent:o:n1:p",
        "w": "agent:o:n1:w",
    }
    app = stub_app(
        tmp_path, _resolve_agent_alias=lambda value: aliases.get(value, value)
    )
    service = _BinderService()
    binder = pac_task_binder(app, service)

    assert binder(sender="p", recipient="w", conversation_id="wf-x") is None
    assert binder(sender="user:o", recipient="w", conversation_id="pac-g1") is None
    assert service.calls == []

    callback = binder(sender="p", recipient="w", conversation_id="pac-g1")
    assert callback is not None
    assert service.calls[0][:3] == ("agent:o:n1:p", "agent:o:n1:w", "pac-g1")
    service.callback_error = True
    callback("m1")  # never raises into the PAC delivery path
    assert app._log.count("link.pac_bind_failed") == 1

    bad = stub_app(
        tmp_path,
        _resolve_agent_alias=lambda value: (_ for _ in ()).throw(
            RuntimeError("alias boom")
        ),
    )
    assert pac_task_binder(bad, service)(
        sender="p", recipient="w", conversation_id="pac-g1"
    ) is None
    assert bad._log.count("link.pac_bind_failed") == 1


# -- S-B1 -----------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_composition_links_stays_inside_daemon_and_back() -> None:
    import hyprial.daemon.impl.composition.links as links_module

    imports = _imports(Path(links_module.__file__))
    assert not any(name.startswith("hyprial.biz") for name in imports)
    assert not any(name.startswith("hyprial.shell") for name in imports)

    links_dir = Path(links_module.__file__).parent.parent / "links"
    for path in links_dir.glob("*.py"):
        assert not any(
            name.startswith("hyprial.daemon.impl.composition")
            for name in _imports(path)
        ), path


def test_a_same_owner_agent_on_another_node_stays_non_local() -> None:
    """Locality is node + registry, never a fallback to owner matching.

    A roster sibling (same owner, different node) must stay remote even after
    the hosted-agent fix; only this node's registry speaks for an actor here.
    """

    identity = LocalIdentity(
        "user:o",
        "n1",
        local=lambda: (
            "agent:o:n1:a",
            "agent:o:n2:sibling",
            "agent:x:n1:hosted",
        ),
    )
    assert identity.is_local("agent:o:n2:sibling") is False
    assert identity.is_local("agent:x:n1:hosted") is True
    assert identity.local_agents() == ("agent:o:n1:a", "agent:x:n1:hosted")
