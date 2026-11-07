"""The four P2 input surfaces share validation and preserve stored aliases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hyprial.daemon.impl.alias_resolver import (
    AliasKind,
    AliasRecord,
    AliasResolver,
    StaticAliasSource,
)
from hyprial.identity import AgentError
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.daemon.impl.pac.workflows.runtime.types import WorkflowServiceError
from hyprial.daemon.impl.pac.contracts.workflow import load_workflow_text
from hyprial.identity import DeviceRecord, write_device_record
from hyprial.daemon.impl.configuration.identity import write_settings_identity
from tests.kernel.actors.identity.support import _registered, application
from tests.support.runtime._owned_applications import close_application


USER_ID = "u.user-a"
DEVICE_ID = "d.11111111111141118111111111111111"


def _seed_p1(
    home: Path,
    *,
    owner: str = "op",
    username: str = "op",
    device_name: str = "node-1",
) -> None:
    write_settings_identity(
        owner,
        mode="casdoor",
        issuer="https://auth.hyprial.test",
        user_id="user-a",
        username=username,
        hyprial_home=home,
    )
    write_device_record(
        home,
        DeviceRecord(
            device_id="node-1",
            owner="op",
            server_public="nodekey:server",
            client_public="nodekey:client",
            device_uid="11111111111141118111111111111111",
            name=device_name,
        ),
    )


def _app(tmp_path: Path):
    _seed_p1(tmp_path)
    app = application(tmp_path)
    app.handle("agent.create", {"name": "driver"})
    app.handle("agent.create", {"name": "foo"})
    return app


def _ambiguous_resolver() -> AliasResolver:
    return AliasResolver(
        StaticAliasSource(
            (
                AliasRecord(AliasKind.USER, "op", USER_ID),
                AliasRecord(AliasKind.DEVICE, "node-1", DEVICE_ID),
                AliasRecord(AliasKind.AGENT, "foo", "a.first"),
                AliasRecord(AliasKind.AGENT, "foo", "a.second"),
            ),
            local={
                AliasKind.USER: AliasRecord(AliasKind.USER, "op", USER_ID),
                AliasKind.DEVICE: AliasRecord(AliasKind.DEVICE, "node-1", DEVICE_ID),
            },
        )
    )


def _workflow(owner: str, *, escalate_to: str | None = None) -> str:
    document = {
        "version": 2,
        "name": "alias-input",
        "nodes": [{"id": "work", "owner": owner, "task": "work"}],
    }
    if escalate_to is not None:
        document["escalate_to"] = escalate_to
    return yaml.safe_dump(document)


def _routine(actor: str, *, escalate_to: str = "user:op") -> str:
    return yaml.safe_dump(
        {
            "version": 2,
            "mode": "scheduled",
            "name": "alias-input",
            "schedule": {"interval": "15m"},
            "task": "work",
            "actor": actor,
            "role": "review",
            "on_task_timeout": {
                "action": "escalate",
                "escalate_to": escalate_to,
            },
        }
    )


def test_a_cli_and_mcp_message_path_refuses_ambiguous_but_queues_unknown_local(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    try:
        app._alias_resolver = _ambiguous_resolver()  # type: ignore[attr-defined]
        with pytest.raises(DaemonRequestError) as ambiguous:
            app.handle(
                "message.send",
                {"from": _registered(app, "driver"), "to": ["foo"], "summary": "hi", "message": "hi"},
            )
        assert ambiguous.value.code == "AMBIGUOUS_TARGET"
        assert ambiguous.value.data == {
            "alias": "foo",
            "candidates": ["a.first", "a.second"],
            "kind": "agent",
        }

        unknown = app.handle(
            "message.send",
            {
                "from": _registered(app, "driver"),
                "to": ["agent:op:node-1:ghost"],
                "summary": "hi",
                "message": "hi",
            },
        )
        assert unknown["deliveries"][0]["queued"] is True
        assert unknown["deliveries"][0]["code"] == "TARGET_UNRESOLVED"
    finally:
        close_application(app)


def test_round_3_unknown_bare_message_keeps_legacy_code_and_data(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        with pytest.raises(DaemonRequestError) as failure:
            app.handle(
                "message.send",
                {"from": _registered(app, "driver"), "to": ["ghost"], "summary": "hi", "message": "hi"},
            )
        assert failure.value.code == "TARGET_IS_NODE"
        assert failure.value.data == {"target": "ghost"}
    finally:
        close_application(app)


def test_a_pac_plan_and_run_admission_refuses_ambiguous_owner(tmp_path: Path) -> None:
    app = _app(tmp_path)
    service = GraphWorkflowService(
        state_dir=tmp_path / "pac",
        owner=app.owner,
        machine=app.node_id,
        sender=NullSender(),
        admit=app._workflow_admit,  # type: ignore[attr-defined]
        start_thread=False,
    )
    try:
        app._alias_resolver = _ambiguous_resolver()  # type: ignore[attr-defined]
        raw = _workflow("agent:op:node-1:foo")
        with pytest.raises(DaemonRequestError) as plan_failure:
            app.handle("workflow.plan", {"yaml": raw, "actor": "user:op"})
        assert plan_failure.value.code == "AMBIGUOUS_TARGET"
        with pytest.raises(DaemonRequestError) as run_failure:
            service.start(yaml_text=raw, sender="user:op")
        assert run_failure.value.code == "AMBIGUOUS_TARGET"
    finally:
        service.close()
        close_application(app)


@pytest.mark.parametrize(
    ("owner", "expected_code", "expected_data"),
    [
        ("ghost", "WORKFLOW_SCHEMA_ERROR", None),
        (
            "agent:op:node-1:ghost",
            "SENDER_UNRESOLVED",
            {"sender": "agent:op:node-1:ghost"},
        ),
    ],
)
def test_round_3_pac_unknown_owner_keeps_legacy_code_and_data(
    tmp_path: Path, owner: str, expected_code: str, expected_data: object
) -> None:
    app = _app(tmp_path)
    service = GraphWorkflowService(
        state_dir=tmp_path / "pac",
        owner=app.owner,
        machine=app.node_id,
        sender=NullSender(),
        admit=app._workflow_admit,  # type: ignore[attr-defined]
        start_thread=False,
    )
    raw = _workflow(owner)
    try:
        with pytest.raises(DaemonRequestError) as plan_failure:
            app.handle("workflow.plan", {"yaml": raw, "actor": "user:op"})
        assert plan_failure.value.code == expected_code
        assert plan_failure.value.data == expected_data

        with pytest.raises((DaemonRequestError, WorkflowServiceError)) as run_failure:
            service.start(yaml_text=raw, sender="user:op")
        assert run_failure.value.code == expected_code
        assert getattr(run_failure.value, "data", None) == expected_data
    finally:
        service.close()
        close_application(app)


def test_a_routine_yaml_load_refuses_ambiguous_actor(tmp_path: Path) -> None:
    app = _app(tmp_path)
    original = app._routine_service  # type: ignore[attr-defined]
    try:
        app._alias_resolver = _ambiguous_resolver()  # type: ignore[attr-defined]
        app._routine_service = object()  # type: ignore[attr-defined]
        with pytest.raises(DaemonRequestError) as failure:
            app.handle("routine.add", {"yaml": _routine("agent:op:node-1:foo"), "actor": "user:op"})
        assert failure.value.code == "AMBIGUOUS_TARGET"
    finally:
        app._routine_service = original  # type: ignore[attr-defined]
        close_application(app)


@pytest.mark.parametrize(
    ("actor", "expected_code", "expected_data"),
    [
        ("ghost", "ROUTINE_SCHEMA_ERROR", None),
        (
            "agent:op:node-1:ghost",
            "SENDER_UNRESOLVED",
            {"sender": "agent:op:node-1:ghost"},
        ),
    ],
)
def test_round_3_routine_unknown_actor_keeps_legacy_code_and_data(
    tmp_path: Path, actor: str, expected_code: str, expected_data: object
) -> None:
    app = _app(tmp_path)
    original = app._routine_service  # type: ignore[attr-defined]
    try:
        app._routine_service = object()  # type: ignore[attr-defined]
        with pytest.raises(DaemonRequestError) as failure:
            app.handle(
                "routine.add",
                {"yaml": _routine(actor), "actor": "user:op"},
            )
        assert failure.value.code == expected_code
        assert failure.value.data == expected_data
    finally:
        app._routine_service = original  # type: ignore[attr-defined]
        close_application(app)


def test_a_outside_domain_keeps_existing_behavior_and_logs_once(tmp_path: Path) -> None:
    app = _app(tmp_path)
    events: list[tuple[str, dict[str, object]]] = []
    app._alias_resolver = AliasResolver(  # type: ignore[attr-defined]
        StaticAliasSource(
            (
                AliasRecord(AliasKind.USER, "op", USER_ID),
                AliasRecord(AliasKind.DEVICE, "node-1", DEVICE_ID),
            ),
            local={
                AliasKind.USER: AliasRecord(AliasKind.USER, "op", USER_ID),
                AliasKind.DEVICE: AliasRecord(AliasKind.DEVICE, "node-1", DEVICE_ID),
            },
        ),
        log=lambda event, **fields: events.append((event, fields)),
    )
    target = "agent:other:peer:worker"
    try:
        for _ in range(2):
            result = app.handle(
                "message.send",
                {"from": _registered(app, "driver"), "to": [target], "summary": "hi", "message": "hi"},
            )
            assert result["deliveries"][0]["target"] == target
        assert events == [
            (
                "alias.unresolved",
                {
                    "kind": "user",
                    "surface": "daemon",
                },
            )
        ]
    finally:
        close_application(app)


def test_b_real_settings_device_and_registry_sources_follow_agent_incarnations(tmp_path: Path) -> None:
    app = _app(tmp_path)
    resolver = app._alias_resolver  # type: ignore[attr-defined]
    try:
        assert resolver.resolve(AliasKind.USER, "op", within_domain=True).identifier == USER_ID
        assert resolver.resolve(AliasKind.DEVICE, "node-1", within_domain=True).identifier == DEVICE_ID
        first = app.agents.require("foo").agent_id
        assert resolver.resolve(AliasKind.AGENT, "foo", within_domain=True).identifier == first

        app.handle("agent.destroy", {"name": "foo"})
        app.handle("agent.create", {"name": "foo"})
        second = app.agents.require("foo").agent_id

        assert second != first
        assert resolver.resolve(AliasKind.AGENT, "foo", within_domain=True).identifier == second
        assert resolver.resolve(AliasKind.AGENT, "foo", within_domain=True).identifier == second
    finally:
        close_application(app)


def test_round_4_display_names_never_define_the_local_uri_domain(tmp_path: Path) -> None:
    _seed_p1(tmp_path, username="op", device_name="display-device")
    app = application(tmp_path)
    app.handle("agent.create", {"name": "driver"})
    try:
        for outside in (
            "agent:op:display-device:ghost",
        ):
            result = app.handle(
                "message.send",
                {
                    "from": _registered(app, "driver"),
                    "to": [outside],
                    "summary": "hi",
                    "message": "hi",
                },
            )
            assert result["deliveries"][0]["target"] == outside

        local = app.handle(
            "message.send",
            {
                "from": _registered(app, "driver"),
                "to": ["agent:op:node-1:ghost"],
                "summary": "hi",
                "message": "hi",
            },
        )
        assert local["deliveries"][0]["queued"] is True
        assert local["deliveries"][0]["code"] == "TARGET_UNRESOLVED"
    finally:
        close_application(app)


def test_round_4_owner_and_node_id_enforce_domain_without_p1_profile(tmp_path: Path) -> None:
    app = application(tmp_path)
    app.handle("agent.create", {"name": "driver"})
    try:
        local = app.handle(
            "message.send",
            {
                "from": _registered(app, "driver"),
                "to": ["agent:op:node-1:ghost"],
                "summary": "hi",
                "message": "hi",
            },
        )
        assert local["deliveries"][0]["queued"] is True
        assert local["deliveries"][0]["code"] == "TARGET_UNRESOLVED"
    finally:
        close_application(app)


class _OnlinePresence:
    def __init__(self, actor: str) -> None:
        self.actor = actor

    def actor_online(self, actor: str) -> bool:
        return actor == self.actor

    def online_actors(self) -> tuple[str, ...]:
        return (self.actor,)

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


def test_round_5_presence_only_escalation_target_is_accepted_by_pac_and_routine(
    tmp_path: Path,
) -> None:
    """A locally claimed (presence-online) actor with no registry or
    desired-state row is a valid escalation target: P2's local-known check
    reuses dev's claim predicate instead of a narrower registry view."""

    app = _app(tmp_path)
    actor = "agent:op:node-1:presence-only"
    app._presence = _OnlinePresence(actor)  # type: ignore[assignment]
    try:
        workflow = load_workflow_text(
            _workflow("agent:op:node-1:foo", escalate_to=actor)
        )
        admitted = app._workflow_admit(workflow, "user:op")  # type: ignore[attr-defined]
        assert admitted.escalate_to == actor

        routine = app._routine_text_loader(  # type: ignore[attr-defined]
            _routine("agent:op:node-1:foo", escalate_to=actor)
        )
        app._routine_admit(routine)  # type: ignore[attr-defined]
        assert routine.escalate_to == actor
    finally:
        close_application(app)


def test_round_5_presence_only_owner_still_needs_a_registry_row(
    tmp_path: Path,
) -> None:
    """The node owner / routine actor keeps dev's resolve-or-reject gate:
    an identity on the wire carries a registry row, so a presence-only
    actor is still SENDER_UNRESOLVED there (unchanged from dev)."""

    app = _app(tmp_path)
    actor = "agent:op:node-1:presence-only"
    app._presence = _OnlinePresence(actor)  # type: ignore[assignment]
    try:
        workflow = load_workflow_text(_workflow(actor))
        with pytest.raises(DaemonRequestError) as owner:
            app._workflow_admit(workflow, "user:op")  # type: ignore[attr-defined]
        assert owner.value.code == "SENDER_UNRESOLVED"

        routine = app._routine_text_loader(_routine(actor))  # type: ignore[attr-defined]
        with pytest.raises(DaemonRequestError) as routine_actor:
            app._routine_admit(routine)  # type: ignore[attr-defined]
        assert routine_actor.value.code == "SENDER_UNRESOLVED"
    finally:
        close_application(app)


def test_round_4_pac_escalate_to_uses_alias_admission(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        app._alias_resolver = _ambiguous_resolver()  # type: ignore[attr-defined]
        with pytest.raises(DaemonRequestError) as failure:
            app.handle(
                "workflow.plan",
                {
                    "yaml": _workflow(
                        "user:op",
                        escalate_to="agent:op:node-1:foo",
                    ),
                    "actor": "user:op",
                },
            )
        assert failure.value.code == "AMBIGUOUS_TARGET"
    finally:
        close_application(app)


class _RaisingAliasSource:
    def records(self, kind: AliasKind):
        raise AgentError(f"broken {kind.value} source")

    def local_record(self, kind: AliasKind):
        return None

    def is_local_domain(self, kind: AliasKind, value: str) -> bool:
        return kind is AliasKind.USER and value == "op" or (
            kind is AliasKind.DEVICE and value == "node-1"
        )

    def knows_local_agent(self, value: str) -> bool:
        return False


def test_round_4_source_failure_warns_and_never_breaks_a_send(tmp_path: Path) -> None:
    app = _app(tmp_path)
    events: list[tuple[str, str, str, dict[str, object]]] = []
    app._alias_resolver = AliasResolver(_RaisingAliasSource())  # type: ignore[arg-type,attr-defined]
    app._log = (  # type: ignore[method-assign]
        lambda level, component, event, **fields: events.append(
            (level, component, event, fields)
        )
    )
    try:
        result = app.handle(
            "message.send",
            {
                "from": _registered(app, "driver"),
                "to": ["agent:op:node-1:foo"],
                "summary": "hi",
                "message": "hi",
            },
        )
        assert result["deliveries"][0]["target"] == "agent:op:node-1:foo"
        assert (
            "warn",
            "daemon",
            "alias.source-unavailable",
            {"errorType": "AgentError", "surface": "daemon"},
        ) in events
    finally:
        close_application(app)


def test_source_unavailable_warns_once_per_error_type_and_surface(
    tmp_path: Path,
) -> None:
    """A broken alias source must not warn on every send (review 980)."""

    app = _app(tmp_path)
    events: list[tuple[str, str, str, dict[str, object]]] = []
    app._alias_resolver = AliasResolver(_RaisingAliasSource())  # type: ignore[arg-type,attr-defined]
    app._log = (  # type: ignore[method-assign]
        lambda level, component, event, **fields: events.append(
            (level, component, event, fields)
        )
    )
    try:
        sender = _registered(app, "driver")
        for _ in range(3):
            app.handle(
                "message.send",
                {"from": sender, "to": ["agent:op:node-1:foo"], "summary": "hi", "message": "hi"},
            )
        warns = [entry for entry in events if entry[2] == "alias.source-unavailable"]
        assert warns == [
            (
                "warn",
                "daemon",
                "alias.source-unavailable",
                {"errorType": "AgentError", "surface": "daemon"},
            )
        ]
    finally:
        close_application(app)


def test_round_4_message_entry_point_surface_is_not_mislabeled_cli(tmp_path: Path) -> None:
    app = _app(tmp_path)
    events: list[tuple[str, dict[str, object]]] = []
    app._alias_resolver = AliasResolver(  # type: ignore[attr-defined]
        StaticAliasSource((), local={}),
        log=lambda event, **fields: events.append((event, fields)),
    )
    try:
        app.handle(
            "message.send",
            {
                "from": _registered(app, "driver"),
                "to": ["agent:other:peer:worker"],
                "summary": "hi",
                "message": "hi",
            },
        )
        app.handle(
            "message.send",
            {
                "from": "adapter:lark:test",
                "to": ["agent:another:peer:worker"],
                "summary": "hi",
                "message": "hi",
            },
        )
        assert events == [
            ("alias.unresolved", {"kind": "user", "surface": "daemon"}),
            ("alias.unresolved", {"kind": "user", "surface": "adapter"}),
        ]
    finally:
        close_application(app)


def test_e_validation_does_not_rewrite_message_pac_or_routine_storage_forms(tmp_path: Path) -> None:
    app = _app(tmp_path)
    actor = "agent:op:node-1:foo"
    try:
        sent = app.handle(
            "message.send",
            {"from": _registered(app, "driver"), "to": [actor], "summary": "hi", "message": "hi"},
        )
        assert sent["deliveries"][0]["target"] == actor

        spec = load_workflow_text(_workflow(actor))
        admitted = app._workflow_admit(spec, "user:op")  # type: ignore[attr-defined]
        assert admitted.nodes[0].owner == actor

        raw = _routine(actor)
        parsed = app._routine_text_loader(raw)  # type: ignore[attr-defined]
        app._validate_routine_aliases(parsed)  # type: ignore[attr-defined]
        assert parsed.actor == actor
        assert json.loads(json.dumps(yaml.safe_load(raw)))["actor"] == actor
    finally:
        close_application(app)
