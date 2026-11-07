from __future__ import annotations

import ast
import dataclasses
import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import get_args

import pytest

from hyprial.daemon.impl.adapters.lark.ports.ports import AdapterPinProjection, AdapterProjection, AdapterReloadProjection, AdaptersProjection
from hyprial.identity.impl.agents.actor.ports import AgentProjection, AgentsProjection
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessLaunchProjection, HarnessStatusProjection
from hyprial.daemon.impl.operations.session_ports import SessionMutationProjection
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import TurnResultProjection
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.contracts.ports import SubmissionProjection, SubmitMessageCommand
from hyprial.biz.impl.routine.contracts.ports import (
    RoutineInFlightProjection,
    RoutineProjection,
    RoutinesProjection,
)

PORT_MODULE_NAMES = (
    'hyprial.daemon.impl.inbox.contracts.ports',
    'hyprial.biz.impl.routine.contracts.ports',
    'hyprial.daemon.impl.harnesses.runtime.ports',
    'hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports',
    "hyprial.daemon.impl.adapters.lark.ports.ports",
    'hyprial.daemon.impl.operations.session_ports',
    'hyprial.identity.impl.agents.actor.ports',
)
EVENT_ALIAS_NAMES = (
    "InboxEvent",
    "RoutineEvent",
    "HarnessEvent",
    "TurnEvent",
    "LarkEvent",
    "SessionEvent",
    "AgentEvent",
)
COMMAND_ALIAS_NAMES = (
    "InboxCommand",
    "RoutineCommand",
    "HarnessCommand",
    "TurnCommand",
    "LarkCommand",
    "SessionCommand",
    "AgentCommand",
)


def _modules() -> tuple[ModuleType, ...]:
    return tuple(importlib.import_module(name) for name in PORT_MODULE_NAMES)


def _source_tree(module: ModuleType) -> ast.Module:
    assert module.__file__ is not None
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def test_inbox_and_contract_ports_import_in_a_fresh_process() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                'from hyprial.daemon.impl.inbox import DeliveryCustody, InboxService; '
                'from hyprial.daemon.impl.ipc.wire import evaluate_case; '
                "from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_domain_ports_have_no_backend_pykka_service_or_store_imports() -> None:
    forbidden = ("pykka", "actor_runtime.backend", "actor_runtime.pykka_backend")
    for module in _modules():
        for node in ast.walk(_source_tree(module)):
            imported: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported = (node.module or "",)
            for name in imported:
                assert not any(marker in name for marker in forbidden), (module, name)
                assert not name.endswith((".service", ".store")), (module, name)


def test_commands_are_frozen_slotted_correlated_and_have_no_mutable_fields() -> None:
    for module, alias_name in zip(_modules(), COMMAND_ALIAS_NAMES, strict=True):
        command_types = get_args(getattr(module, alias_name))
        assert command_types
        for command_type in command_types:
            assert dataclasses.is_dataclass(command_type)
            assert command_type.__dataclass_params__.frozen is True
            assert hasattr(command_type, "__slots__")
            fields = dataclasses.fields(command_type)
            assert any(field.name == "correlation_id" for field in fields)
            for field in fields:
                annotation = repr(field.type)
                assert "list[" not in annotation
                assert "dict[" not in annotation
                assert "set[" not in annotation


def test_every_async_event_is_correlated_generation_and_version_fenced() -> None:
    for module, alias_name in zip(_modules(), EVENT_ALIAS_NAMES, strict=True):
        event_types = get_args(getattr(module, alias_name))
        assert event_types
        for event_type in event_types:
            fields = {field.name for field in dataclasses.fields(event_type)}
            assert {"correlation_id", "generation", "version"} <= fields


def test_timer_and_io_contracts_explicitly_carry_generation_and_version() -> None:
    for module in _modules():
        for value in vars(module).values():
            if not isinstance(value, type) or not dataclasses.is_dataclass(value):
                continue
            if not any(
                marker in value.__name__ for marker in ("Timer", "Elapsed", "Io")
            ):
                continue
            fields = {field.name for field in dataclasses.fields(value)}
            assert {"generation", "version"} <= fields, value


def test_projection_ports_never_expose_blocking_ask_or_get_methods() -> None:
    for module in _modules():
        tree = _source_tree(module)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in {"ask", "get"}
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"ask", "get", "proxy"}


def test_command_and_error_values_are_immutable_and_closing_is_explicit() -> None:
    message = InboxMessage(
        message_id="m1",
        conversation_id="c1",
        sender="a",
        recipient="b",
        payload=b"{}",
        intent="message",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    command = SubmitMessageCommand(correlation_id="corr", message=message)
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.correlation_id = "changed"  # type: ignore[misc]
    rejection = PortCommandRejected(
        correlation_id="corr",
        domain="inbox",
        generation=2,
        version=3,
        code="PORT_CLOSING",
        detail="closing",
        admission=PortAdmission.CLOSING,
    )
    assert rejection.admission is PortAdmission.CLOSING


def test_routine_ipc_projection_is_structured() -> None:
    routine = RoutineProjection(
        version=4,
        name="daily",
        owner="agent:owner",
        enabled=True,
        next_due_ms=100,
        source_error_streak=0,
        outcomes=("done",),
        in_flight=(RoutineInFlightProjection("t1", "r1", "agent:a"),),
    )
    assert RoutinesProjection((routine,)).to_payload() == {
        "routines": [
            {
                "name": "daily",
                "owner": "agent:owner",
                "enabled": True,
                "nextDueMs": 100,
                "sourceErrorStreak": 0,
                "outcomes": ["done"],
                "registrationId": None, "mode": "source", "role": "dispatch", "actor": "agent:owner",
                "actorOwnership": "external", "launch": None, "overlap": None, "missedPeriods": None,
                "inFlight": [{"taskUuid": "t1", "runId": "r1", "target": "agent:a"}],
            }
        ]
    }


def test_existing_adapter_agent_session_and_harness_payload_shapes_are_preserved() -> (
    None
):
    adapter = AdapterProjection(
        version=1,
        adapter_id="lark:work",
        name="work",
        status="online",
        online=True,
        configured=True,
        desired=True,
        process_running=True,
        pid=42,
    )
    assert AdaptersProjection((adapter,)).to_payload() == {
        "ok": True,
        "adapters": [
            {
                "id": "lark:work",
                "provider": "lark",
                "name": "work",
                "status": "online",
                "online": True,
                "configured": True,
                "desired": True,
                "processRunning": True,
                "pid": 42,
            }
        ],
    }
    assert AdapterReloadProjection(("a",), (), (), ()).to_payload() == {
        "added": ["a"],
        "updated": [],
        "removed": [],
        "removedRunning": [],
    }
    assert AdapterPinProjection(
        "work", "agent:a", None, True, (("work", "agent:a"),)
    ).to_payload() == {
        "ok": True,
        "adapter": "work",
        "actor": "agent:a",
        "previous": None,
        "changed": True,
        "pins": {"work": "agent:a"},
    }

    agent = AgentProjection(
        version=1,
        uri="agent:o:m:a",
        actor="a",
        owner="o",
        machine="m",
        entity_token="entity-token",
        capabilities=(("chat", "true"),),
        harness_args=(("codex", ("--x",)),),
        pinned_adapters=("work",),
        created_at_ms=9,
    )
    assert AgentsProjection((agent,)).to_payload()["agents"] == [agent.to_payload()]

    assert SessionMutationProjection(
        actor="agent:o:m:a",
        session_ref=None,
        daemon_epoch=None,
        unregistered=True,
    ).to_payload() == {
        "ok": True,
        "unregistered": True,
        "actor": "agent:o:m:a",
    }

    launch = HarnessLaunchProjection("codex", "a", True, args=("--x",))
    assert launch.to_payload()["provider"] == "codex"
    assert launch.to_payload()["args"] == ["--x"]
    status = HarnessStatusProjection(1, "codex:a", "codex", "a", True, pid=10)
    assert status.to_payload() == {
        "id": "codex:a",
        "runtime": "codex",
        "name": "a",
        "running": True,
        "pid": 10,
    }


def test_inbox_and_turn_payload_dtos_keep_current_camel_case_contract() -> None:
    submission = SubmissionProjection("m1", True, queued=True)
    assert submission.to_payload() == {
        "messageId": "m1",
        "accepted": True,
        "queued": True,
    }
    result = TurnResultProjection(
        delivery_id="d1",
        recipient="agent:a",
        status="completed",
        output="done",
    )
    assert result.to_payload() == {
        "deliveryId": "d1",
        "recipient": "agent:a",
        "status": "completed",
        "output": "done",
    }
