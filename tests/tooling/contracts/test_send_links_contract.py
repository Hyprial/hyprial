"""K1-K15: the send-links registry pins kernel, daemon and source constants.

Spec: docs/design/send-links-impl/wp8.md §3.2 and §6.1.  The registry
``contract/wire/send-links.json`` is the machine-readable half of
``contract/wire/send-links.md``; these tests pin it *both ways* against the
public faces (``hyprial.kernel`` / ``hyprial.daemon``) and the source that
constructs the values, without importing the link service itself (K3/K8 scan
with ``ast`` so a refactor that renames a private helper is still caught).

先红 shape: the registry file is missing -> FileNotFoundError; any drift
between registry and source -> AssertionError printing the difference.

K10 and K12 pin names that WP8 slices B and C delivered (retired E2E runner
prefixes and the MCP tool descriptions); they carried an xfail marker until
the three slices were merged into ``wp8/send-links``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from hyprial.kernel import EVENT_NAMES, RESERVED_AGENT_ACTOR_NAMES, ipc_errors
from hyprial.kernel.impl.contracts.channels.worker_channel import (
    METHOD_PARAMS as WORKER_CHANNEL_METHOD_PARAMS,
)
from hyprial.daemon import (
    HOLD_REASONS,
    HOLD_REASON_KEY,
    SUMMARY_MAX_BYTES,
    SUMMARY_REQUIRED_DATA_KEYS,
    TOP_FIELD_CAPACITY_W,
    TOP_FIELD_INBOUND_AWAITING_INJECTION,
    TOP_FIELD_OUTBOUND_IN_FLIGHT,
)
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from hyprial.daemon.impl.links.ports import (
    EVENT_LINK_MINTED,
    EVENT_LINK_RECLAIMED,
    IPC_LINK_ADD,
    IPC_LINK_DROP,
    IPC_LINK_LIST,
    IPC_LINK_STATS,
    IPC_MESSAGE_READ_BODY,
    IPC_MESSAGE_SETTLE_HINT,
    LINK_DIR,
)

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src" / "hyprial"
IMPL = SRC / "daemon" / "impl"
REGISTRY = REPO / "contract" / "wire" / "send-links.json"
GOLDEN = REPO / "contract" / "wire" / "golden"


def _registry() -> dict[str, object]:
    if not REGISTRY.is_file():
        raise FileNotFoundError(f"send-links registry missing: {REGISTRY}")
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _return_dict_keys(path: Path, name: str) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(_function(_tree(path), name)):
        if not isinstance(node, ast.Return):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Dict):
                for key in child.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.add(key.value)
    return keys


def _literal_dict_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


def _constructed_keys(path: Path, name: str, symbols: dict[str, str]) -> set[str]:
    """Every dict key one product function constructs, resolving named constants."""

    def _key(node: ast.expr | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in symbols:
            return symbols[node.id]
        return None

    keys: set[str] = set()
    for node in ast.walk(_function(_tree(path), name)):
        if isinstance(node, ast.Dict):
            keys.update(filter(None, (_key(item) for item in node.keys)))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    found = _key(target.slice)
                    if found is not None:
                        keys.add(found)
    return keys


def _log_stage_literals(path: Path) -> set[str]:
    """Closed set of ``stage`` values the link composition can log."""

    stages: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "stage":
                    stages.add(node.value.value)
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else (
            function.id if isinstance(function, ast.Name) else None)
        if name not in {"_log", "_logger", "log"}:
            continue
        for keyword in node.keywords:
            if keyword.arg == "stage" and isinstance(keyword.value, ast.Constant) \
                    and isinstance(keyword.value.value, str):
                stages.add(keyword.value.value)
    return stages


def _params_read(path: Path, name: str) -> set[str]:
    """String keys the function reads off its ``params`` argument."""

    read: set[str] = set()
    for node in ast.walk(_function(_tree(path), name)):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id == "params" and isinstance(node.slice, ast.Constant):
                read.add(str(node.slice.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if (
                isinstance(base, ast.Name)
                and base.id == "params"
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                read.add(str(node.args[0].value))
    return read


# --------------------------------------------------------------------------- K1-K9

def test_k1_registered_error_codes_are_frozen_kernel_codes() -> None:
    codes = set(_registry()["errorCodes"])  # type: ignore[arg-type]
    expected = (
        {ipc_errors.SUMMARY_REQUIRED, ipc_errors.MESSAGE_BODY_UNAVAILABLE}
        | set(HOLD_REASONS)
        | {ipc_errors.LINK_UNKNOWN, ipc_errors.LINK_RECLAIMED}
    )
    assert codes == expected, f"difference: {codes ^ expected}"
    assert codes <= ipc_errors.ALL_CODES, f"not frozen: {codes - ipc_errors.ALL_CODES}"


def test_k2_hold_reasons_and_key_match_ports() -> None:
    registry = _registry()
    assert set(registry["holdReasons"]) == set(HOLD_REASONS)  # type: ignore[arg-type]
    assert registry["holdReasonKey"] == HOLD_REASON_KEY


def test_k3_hold_details_are_closed_over_the_source() -> None:
    registry = _registry()
    details = registry["holdDetails"]  # type: ignore[assignment]
    assert isinstance(details, dict)
    policy = IMPL / "links" / "policy.py"
    sources = [policy, IMPL / "links" / "controller.py",
               IMPL / "application" / "messaging" / "inbox_surface" / "link_send.py"]
    sources += sorted((IMPL / "links" / "service").glob("*.py"))
    found: set[str] = set()
    for path in sources:
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name == "Hold":
                for arg in node.args[2:3]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
            for keyword in node.keywords:
                if (
                    keyword.arg == "detail"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    found.add(keyword.value.value)
    assert found == set(details), f"difference: {found ^ set(details)}"
    for detail, reasons in details.items():
        assert set(reasons) <= set(HOLD_REASONS), detail


def _real_app(tmp_path: Path, own: object) -> object:
    """A real router over a real inbox and a real link service."""

    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.inbox import (
        DeliveryLifecycle,
        InboxMessage,
        InboxService,
        MemoryDeliveryTransport,
    )
    from hyprial.shell.impl.daemon_wiring import daemon_dependencies

    from tests.support.links.gate import PermissiveLinkGate
    from tests.support.links.org import FakeOrgFs, make_service

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    app = DaemonApplication(
        state_dir=state, socket_path=state / "daemon.sock",
        node_id="n1", owner="o", **daemon_dependencies(),
    )
    own(app)  # type: ignore[operator]
    transport = MemoryDeliveryTransport()
    inbox = InboxService(tmp_path / "inbox.sqlite3", transport,
                         link_admission=app.link_admission)
    app._inbox = inbox  # type: ignore[assignment]
    app._harnesses = object()
    app._presence = object()
    for name in ("a", "b", "driver"):
        if app.agents.get(name) is None:
            app.handle("agent.create", {"name": name})
    app.handle("session.register", {
        "actor": "a", "sessionRef": "session-1", "cwd": "/work",
        "command": ["claude"], "source": "claude-channel", "channelConfirmed": True,
    })
    app.link_admission.bind(PermissiveLinkGate())
    inbox.receive(InboxMessage(
        message_id="m-1", conversation_id="c", sender="agent:o:n1:driver",
        recipient="agent:o:n1:a", payload=b'{"message": "B"}',
        intent="request", lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1, summary="S",
    ))
    fs = FakeOrgFs()
    fs.add_space("sp-0", name="group-test")
    app._link_service = make_service(fs=fs).service  # type: ignore[attr-defined]
    return app


def test_k4_registered_ipc_methods_are_routed_with_registered_shapes(tmp_path: Path,
                                                                     own_daemon_application: object) -> None:
    registry = _registry()
    ipc = registry["ipc"]  # type: ignore[assignment]
    assert isinstance(ipc, dict)
    ports_methods = {
        IPC_MESSAGE_READ_BODY: "message.read_body",
        IPC_MESSAGE_SETTLE_HINT: "message.settle_hint",
        IPC_LINK_LIST: "link.list",
        IPC_LINK_STATS: "link.stats",
        IPC_LINK_ADD: "link.add",
        IPC_LINK_DROP: "link.drop",
    }
    assert all(name == constant for constant, name in ports_methods.items())
    assert set(ipc) == set(ports_methods.values()) | {
        "session.turn.started", "session.turn.ended"}
    assert set(ipc) <= _IPC_STATS_METHODS, set(ipc) - _IPC_STATS_METHODS
    assert set(registry["workerChannelMethods"]) <= set(WORKER_CHANNEL_METHOD_PARAMS)  # type: ignore[arg-type]

    queries = IMPL / "application" / "messaging" / "inbox_surface" / "queries.py"
    views = IMPL / "links" / "service" / "views.py"
    turns = IMPL / "application" / "actors" / "sessions" / "turns.py"
    link_ipc = IMPL / "request_catalog" / "link.py"
    source_keys = {
        "message.read_body": _return_dict_keys(queries, "_read_body_row") | {"ok"},
        "message.settle_hint": _return_dict_keys(queries, "_ipc_message_settle_hint"),
        "link.list": _return_dict_keys(views, "list_links") | {"ok"},
        "link.stats": _return_dict_keys(views, "stats") | {"ok"},
        "link.add": _return_dict_keys(views, "add") | {"ok"},
        "link.drop": _return_dict_keys(views, "drop") | {"ok"},
        "session.turn.started": _return_dict_keys(turns, "_ipc_session_turn_started"),
        "session.turn.ended": _return_dict_keys(turns, "_ipc_session_turn_ended"),
    }
    source_params = {
        "message.read_body": _params_read(queries, "_ipc_message_read_body"),
        "message.settle_hint": _params_read(queries, "_ipc_message_settle_hint"),
        "link.list": _params_read(link_ipc, "_actor"),
        "link.stats": _params_read(link_ipc, "_actor"),
        "link.add": _params_read(link_ipc, "_actor")
        | _params_read(link_ipc, "link_add"),
        "link.drop": _params_read(link_ipc, "_actor")
        | _params_read(link_ipc, "link_drop"),
        "session.turn.started": _params_read(turns, "_ipc_session_turn_started") | {"actor"},
        "session.turn.ended": _params_read(turns, "_ipc_session_turn_ended") | {"actor"},
    }
    app = _real_app(tmp_path, own_daemon_application)
    answers = {
        "message.read_body": app.handle("message.read_body", {
            "actor": "a", "sessionRef": "session-1", "messageId": "m-1"}),
        "message.settle_hint": app.handle("message.settle_hint", {
            "actor": "a", "sessionRef": "session-1", "messageId": "m-1"}),
        "link.list": app.handle("link.list", {"from": "agent:o:n1:a"}),
        "link.stats": app.handle("link.stats", {"from": "agent:o:n1:a"}),
        "link.add": app.handle("link.add", {
            "from": "agent:o:n1:a", "peer": "agent:o:n1:b"}),
        "link.drop": app.handle("link.drop", {
            "from": "agent:o:n1:a", "peer": "agent:o:n1:b"}),
        "session.turn.started": app.handle("session.turn.started", {
            "actor": "a", "sessionRef": "session-1", "reportId": "t1"}),
        "session.turn.ended": app.handle("session.turn.ended", {
            "actor": "a", "sessionRef": "session-1", "reportIds": ["t1"]}),
    }
    for method, entry in ipc.items():
        registered = set(entry["result"])
        assert registered, method
        assert registered <= source_keys[method], (method, registered - source_keys[method])
        # ``written`` is a conditional key of ``link.drop``: it appears only on
        # the reclaiming branch, which this minimal fixture (no committed link
        # file) cannot reach.  The source pin above proves it is constructed;
        # every other registered key must appear in the real answer.
        conditional = {"link.drop": {"written"}}.get(method, set())
        assert registered - conditional <= set(answers[method]), (
            method, registered - conditional - set(answers[method]))
        assert conditional <= source_keys[method], method
        required = {p.rstrip("?") for p in entry["params"]}
        assert required <= source_params[method], (method, required - source_params[method])
    row = app.handle("message.pending.list", {
        "actor": "a", "sessionRef": "session-1"})["messages"][0]
    assert set(row) == set(registry["pendingRowKeys"]), set(row) ^ set(registry["pendingRowKeys"])
    assert "message" not in row and "topic" not in row


class _MintGate:
    """The product's ``Mint`` branch: hold with a detail and an evicted peer."""

    def __init__(self, peer: str, evict: str) -> None:
        self._peer = peer
        self._evict = evict

    def sender_known(self, *, recipient: str, sender: str) -> bool:
        return True

    def admit_dispatch(self, *, recipient: str, sender: str, message_id: str,
                       now_ms: int, intent: str = "request",
                       reply_to: str | None = None) -> bool:
        return True

    def admit_send(self, *, sender: str, target: str, intent: str,
                   reply_to: str | None, now_ms: int):  # type: ignore[no-untyped-def]
        from hyprial.daemon.impl.links.ports import Mint

        return Mint(peer=self._peer, evict=self._evict)

    def settle_hint(self, *, actor: str, message_id: str) -> str:
        return "keep"


class _OnlinePresence:
    def actor_online(self, actor: str) -> bool:
        return True


def _held_app(tmp_path: Path, own: object) -> object:
    """A real app whose send/reply boundary holds through the real gate path."""

    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.inbox import (
        DeliveryLifecycle,
        InboxMessage,
        InboxService,
        MemoryDeliveryTransport,
    )
    from hyprial.shell.impl.daemon_wiring import daemon_dependencies

    from tests.support.links.org import FakeOrgFs, make_service

    state = tmp_path / "held-state"
    state.mkdir(parents=True, exist_ok=True)
    app = DaemonApplication(
        state_dir=state, socket_path=state / "daemon.sock",
        node_id="n1", owner="o", **daemon_dependencies(),
    )
    own(app)  # type: ignore[operator]
    inbox = InboxService(tmp_path / "held-inbox.sqlite3", MemoryDeliveryTransport(),
                         link_admission=app.link_admission)
    app._inbox = inbox  # type: ignore[assignment]
    app._harnesses = object()
    app._presence = _OnlinePresence()
    for name in ("a", "b", "c"):
        if app.agents.get(name) is None:
            app.handle("agent.create", {"name": name})
    app.handle("session.register", {
        "actor": "a", "sessionRef": "session-1", "cwd": "/work",
        "command": ["claude"], "source": "claude-channel", "channelConfirmed": True,
    })
    inbox.receive(InboxMessage(
        message_id="req-b", conversation_id="c", sender="agent:o:n1:b",
        recipient="agent:o:n1:a", payload=b'{"message": "?"}',
        intent="request", lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1, summary="q",
    ))
    app.link_admission.bind(_MintGate("agent:o:n1:b", evict="agent:o:n1:c"))
    fs = FakeOrgFs()
    fs.add_space("sp-0", name="group-test")
    app._link_service = make_service(fs=fs).service  # type: ignore[attr-defined]
    return app


def test_k4_registered_result_field_groups_come_from_the_product(
    tmp_path: Path, own_daemon_application: object,
) -> None:
    """C13/C17/C19: the three result-field groups are consumed, not decorative."""

    registry = _registry()
    send_py = IMPL / "application" / "messaging" / "inbox_surface" / "send.py"
    outbox_py = IMPL / "application" / "messaging" / "delivery" / "status_queries.py"
    from hyprial.daemon.impl.application.messaging.inbox_surface.link_send import (
        LinkSendPlan,
    )

    symbols = {"HOLD_REASON_KEY": HOLD_REASON_KEY}
    plan = LinkSendPlan(hold_reason=ipc_errors.LINK_MINTING, detail="minting",
                        evicted="peer")
    send_keys = _constructed_keys(send_py, "_ipc_message_send", symbols)
    send_keys |= set(plan.delivery_fields())
    reply_keys = _constructed_keys(send_py, "_ipc_message_reply", symbols)
    outbox_keys = _constructed_keys(outbox_py, "_outbox_entry_json", symbols)
    assert outbox_keys
    assert set(registry["sendResultFields"]) <= send_keys, \
        set(registry["sendResultFields"]) - send_keys
    assert set(registry["replyHoldFields"]) <= reply_keys, \
        set(registry["replyHoldFields"]) - reply_keys
    assert set(registry["outboxListFields"]) <= outbox_keys, \
        set(registry["outboxListFields"]) - outbox_keys

    from hyprial.daemon.impl.application.messaging.delivery.status_queries import (
        _outbox_entry_json,
    )
    from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, OutboxItem

    app = _held_app(tmp_path, own_daemon_application)
    sent = app.handle("message.send", {  # type: ignore[attr-defined]
        "actor": "a", "sessionRef": "session-1", "to": ["agent:o:n1:b"],
        "message": "body", "summary": "S",
    })
    called = set(sent) | set(sent["deliveries"][0])
    assert set(registry["sendResultFields"]) <= called, \
        set(registry["sendResultFields"]) - called
    replied = app.handle("message.reply", {  # type: ignore[attr-defined]
        "actor": "a", "sessionRef": "session-1", "messageId": "req-b",
        "message": "answer", "summary": "A",
    })
    assert set(registry["replyHoldFields"]) <= set(replied), \
        set(registry["replyHoldFields"]) - set(replied)

    held = OutboxItem(
        message=InboxMessage(
            message_id="held-1", conversation_id="c", sender="agent:o:n1:a",
            recipient="agent:o:n1:b", payload=b'{"message":"x"}',
            intent="request", lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1, summary="s",
        ),
        attempts=0, next_attempt_ms=5, expires_at_ms=10,
        link_hold=ipc_errors.LINK_PEER_FULL,
    )
    entry = _outbox_entry_json(held)
    assert set(registry["outboxListFields"]) <= set(entry), \
        set(registry["outboxListFields"]) - set(entry)
    assert entry[HOLD_REASON_KEY] == ipc_errors.LINK_PEER_FULL


def test_k5_top_fields_match_ports() -> None:
    assert _registry()["topFields"] == [
        TOP_FIELD_CAPACITY_W, TOP_FIELD_OUTBOUND_IN_FLIGHT,
        TOP_FIELD_INBOUND_AWAITING_INJECTION,
    ]


def test_k6_hook_events_are_kernel_event_names() -> None:
    registry = _registry()
    events = set(registry["hookEvents"])  # type: ignore[arg-type]
    assert events <= EVENT_NAMES, events - EVENT_NAMES
    assert {EVENT_LINK_MINTED, EVENT_LINK_RECLAIMED} <= events
    # C23: the two link hook payloads are different shapes; a reclaim has no
    # revision or side.  The service tests pin the same sets at the publisher.
    payloads = registry["hookPayloads"]  # type: ignore[assignment]
    assert set(payloads) == {EVENT_LINK_MINTED, EVENT_LINK_RECLAIMED}
    assert {"revision", "side"} <= set(payloads[EVENT_LINK_MINTED])
    assert "revision" not in payloads[EVENT_LINK_RECLAIMED]
    assert "side" not in payloads[EVENT_LINK_RECLAIMED]
    assert {"drained", "drainedMessageIds", "reason"} <= set(payloads[EVENT_LINK_RECLAIMED])


def test_k7_reserved_names_match_kernel_and_registry_text(tmp_path: Path) -> None:
    from hyprial.identity import AgentRegistry
    from hyprial.identity.impl.agents.registry import InvalidAgentNameError

    registry = _registry()
    registered = set(registry["reservedActorNames"])  # type: ignore[arg-type]
    assert registered == set(RESERVED_AGENT_ACTOR_NAMES)
    agents = AgentRegistry(tmp_path / "agents.sqlite3", owner="op", machine="node-1")
    try:
        for name in sorted(registered):
            with pytest.raises(InvalidAgentNameError) as error:
                agents.create(name)
            assert str(registry["reservedNameText"]) in str(error.value), name
    finally:
        agents.close()


def test_k8_link_log_events_are_closed_both_ways() -> None:
    registry = _registry()
    expected = {event for event in registry["logEvents"] if str(event).startswith("link.")}  # type: ignore[union-attr]
    roots = [IMPL / "links", IMPL / "composition", IMPL / "application" / "messaging" / "inbox_surface"]
    found: set[str] = set()
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            for node in ast.walk(_tree(path)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else None)
                if name not in {"_log", "_logger", "log"}:
                    continue
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                            and arg.value.startswith("link."):
                        found.add(arg.value)
    assert found == expected, f"difference: {found ^ expected}"
    composition = IMPL / "composition" / "links.py"
    stages = _log_stage_literals(composition)
    assert stages == set(registry["serviceStages"]), \
        f"difference: {stages ^ set(registry['serviceStages'])}"
    source = "\n".join(path.read_text(encoding="utf-8") for path in SRC.rglob("*.py"))
    for event in ("harness.settle.deferred", "harness.mechanism_answer.deferred",
                  "harness.stranded_sweep_deferred", "message.settle_hint.invalid"):
        assert event in source, event


def test_k9_summary_limit_matches_ports() -> None:
    registry = _registry()
    assert registry["summaryMaxBytes"] == SUMMARY_MAX_BYTES
    assert registry["summaryRequiredDataKeys"] == list(SUMMARY_REQUIRED_DATA_KEYS)


# --------------------------------------------------------------------------- K10-K15

def _git_grep(needle: str, *paths: str) -> list[str]:
    result = subprocess.run(
        ["git", "grep", "-F", "-n", "--", needle, *paths],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_k10_retired_names_are_gone_from_src_and_contract() -> None:
    registry = _registry()
    needles = list(registry["retired"]) + list(registry["retiredPromptPhrases"])  # type: ignore[arg-type]
    for needle in needles:
        assert _git_grep(needle, "src") == [], f"{needle} still in src/"
        hits = _git_grep(needle, "contract")
        assert all("contract/wire/send-links." in line for line in hits), (needle, hits)


def test_k11_mechanism_reply_prefixes_exist() -> None:
    for prefix in _registry()["mechanismReplyPrefixes"]:  # type: ignore[union-attr]
        assert len(_git_grep(prefix, "src")) == 1, prefix


def test_k12_mcp_send_and_reply_descriptions_state_the_reply_rule() -> None:
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server

    class _Factory:
        def __init__(self) -> None:
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def connect(self):  # type: ignore[no-untyped-def]
                class _Connection:
                    async def request(self, method, params, **_kwargs):  # type: ignore[no-untyped-def]
                        return {"ok": True, "method": method}
                yield _Connection()
            self.connect = connect  # type: ignore[method-assign]

    async def _tools() -> dict[str, str]:
        server = create_mcp_server(
            StatelessDaemonProxy(_Factory()), actor="contract-actor",
            session_ref="contract-session",
        )
        async with InMemoryTransport(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                return {tool.name: tool.description or "" for tool in listed.tools}

    tools = asyncio.run(_tools())
    assert "harness_read_body" in tools
    assert "holdReason" in tools["harness_send"] and "harness_reply" in tools["harness_send"]
    assert "LINK_RECLAIMED" in tools["harness_reply"]


def test_k13_injection_format_registry_points_at_the_golden() -> None:
    registry = _registry()
    injection = registry["injectionFormat"]  # type: ignore[assignment]
    assert isinstance(injection, dict)
    golden = REPO / str(injection["golden"])
    if not golden.is_file():
        raise FileNotFoundError(golden)
    document = json.loads(golden.read_text(encoding="utf-8"))
    assert document["format"] == "summary-v1"
    assert injection["version"] == document["format"]


def test_k14_golden_agent_envelopes_carry_a_summary() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "wire_golden_validate", GOLDEN / "validate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    negative = set(module.NEGATIVE_SUMMARY_FIXTURES)
    envelopes = json.loads((GOLDEN / "envelopes.json").read_text(encoding="utf-8"))["envelopes"]
    checked = 0
    for envelope in envelopes:
        fixture_id = envelope["fixture_id"]
        if str(envelope["from"].get("actorId", "")).startswith("agent:") \
                and envelope["type"] != "control" and fixture_id not in negative:
            summary = envelope.get("summary")
            assert isinstance(summary, str) and summary, fixture_id
            assert len(summary.encode("utf-8")) <= 512, fixture_id
            checked += 1
    assert checked >= 18, checked


def test_k15_link_file_location_matches_ports() -> None:
    link_file = _registry()["linkFile"]  # type: ignore[assignment]
    assert isinstance(link_file, dict)
    assert link_file["dir"] == LINK_DIR
