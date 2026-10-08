"""Lifecycle step planners and the operation/spec payload parsers."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
from hyprial.kernel import parse_execution_runtime
import json
from dataclasses import asdict
from hyprial.identity import (
    AgentMutationCompleted,
    BindAgentCommand,
    CreateAgentCommand,
    DestroyAgentCommand,
    ReleaseAgentCommand,
)
from hyprial.daemon.impl.correlation.correlation  import (
    CorrelatedEvent,
)
from hyprial.daemon.impl.harnesses.runtime.ports  import (
    EnsureHarnessCommand,
    HarnessLaunchProjection,
    HarnessMutationCompleted,
    RemoveHarnessCommand,
)
from hyprial.kernel  import (
    MutationProvenance,
)
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.daemon.impl.network.route_ports  import (
    DropRouteCommand,
    EnsureRouteCommand,
    RouteSpec,
)
from hyprial.daemon.impl.operations.session_ports  import (
    RegisterSessionCommand,
    SessionMutationCompleted,
    UnregisterSessionCommand,
)

from .vocabulary import (
    LifecycleKind,
    LifecycleOperation,
    LifecycleSpec,
    LifecycleStepFailed,
    SessionLifecycleSpec,
    _Step,
)


def _create_steps(spec: LifecycleSpec, prefix: str = "") -> tuple[_Step, ...]:
    steps = [
        _Step(f"{prefix}agent.create", "agent", spec, "create", "destroy"),
        _Step(
            f"{prefix}route.persona.ensure",
            "route",
            spec,
            "ensure",
            "drop",
            f"persona:{spec.actor}",
        ),
        _Step(f"{prefix}agent.bind", "agent", spec, "bind", "release"),
        _Step(f"{prefix}harness.ensure", "harness", spec, "ensure", "remove"),
    ]
    if spec.session is not None:
        steps.append(
            _Step(
                f"{prefix}session.register", "session", spec, "register", "unregister"
            )
        )
    steps.append(
        _Step(
            f"{prefix}route.managed.ensure",
            "route",
            spec,
            "ensure",
            "drop",
            f"managed:{spec.actor}",
        )
    )
    return tuple(steps)


def _remove_steps(spec: LifecycleSpec, prefix: str = "") -> tuple[_Step, ...]:
    return tuple(
        _Step(
            f"{prefix}{step.name.split('.', 1)[1]}.{step.inverse}",
            step.domain,
            spec,
            step.inverse,
            step.forward,
            step.route_owner,
        )
        for step in reversed(_create_steps(spec))
    )


def _deactivate_steps(
    spec: LifecycleSpec, prefix: str = ""
) -> tuple[_Step, ...]:
    """Reverse runtime effects but retain the Agent identity and its pins."""

    steps = list(reversed(_create_steps(spec)))
    return tuple(
        _Step(
            f"{prefix}{step.name.split('.', 1)[1]}.{step.inverse}",
            step.domain,
            spec,
            step.inverse,
            step.forward,
            step.route_owner,
        )
        for step in steps
        if step.name
        not in {f"{prefix}agent.create", f"{prefix}route.persona.ensure"}
    )


def _plan(operation: LifecycleOperation) -> tuple[_Step, ...]:
    if operation.kind is LifecycleKind.CREATE:
        return _create_steps(operation.target)
    if operation.kind is LifecycleKind.DEACTIVATE:
        return _deactivate_steps(operation.target)
    if operation.kind is LifecycleKind.REMOVE:
        return _remove_steps(operation.target)
    if operation.source is None:
        raise ValueError("transfer requires a source spec")
    return (
        *_remove_steps(operation.source, "source."),
        *_create_steps(operation.target, "target."),
    )


def _command(
    step: _Step,
    direction: str,
    *,
    correlation: str,
    attempt_token: str,
    generation: int,
    version: int,
) -> object:
    operation = step.forward if direction == "forward" else step.inverse
    spec = step.spec
    if step.domain == "agent":
        if operation == "create":
            return CreateAgentCommand(correlation, spec.agent_name, reuse_existing=True)
        if operation == "destroy":
            return DestroyAgentCommand(correlation, spec.agent_name)
        if operation == "bind":
            return BindAgentCommand(
                correlation,
                spec.actor,
                spec.harness.harness,
                "headless" if spec.session is None else spec.session.runtime,
                (
                    spec.harness.session_ref
                    if spec.session is None
                    else spec.session.session_ref
                ),
            )
        return ReleaseAgentCommand(correlation, spec.actor)
    if step.domain == "harness":
        if operation == "ensure":
            return EnsureHarnessCommand(correlation, spec.harness)
        return RemoveHarnessCommand(
            correlation,
            spec.harness.harness,
            spec.harness.name,
            interruption_reason=spec.interruption_reason,
        )
    if step.domain == "session":
        session = spec.session
        if session is None:
            raise ValueError("session step requires session spec")
        if operation == "register":
            return RegisterSessionCommand(
                correlation,
                spec.actor,
                session.cwd,
                session.command,
                session.source,
                session.session_ref,
                runtime=session.runtime,
            )
        return UnregisterSessionCommand(correlation, spec.actor, session.session_ref)
    if operation == "ensure":
        return EnsureRouteCommand(
            correlation,
            attempt_token,
            generation,
            version,
            spec.route,
            step.route_owner,
        )
    return DropRouteCommand(
        correlation,
        attempt_token,
        generation,
        version,
        spec.route.route_id,
        step.route_owner,
    )


def _completion_provenance(domain: str, event: CorrelatedEvent) -> MutationProvenance:
    expected: dict[str, type[object]] = {
        "agent": AgentMutationCompleted,
        "session": SessionMutationCompleted,
        "harness": HarnessMutationCompleted,
    }
    if domain == "route":
        # Route failures were handled above; successful route events are the
        # remaining half of the closed union.
        if not hasattr(event, "attempt_token"):
            raise LifecycleStepFailed("route returned an invalid completion")
        provenance = getattr(event, "provenance", None)
        if not isinstance(provenance, MutationProvenance):
            raise LifecycleStepFailed("route completion lacks provenance receipt")
        if bool(getattr(event, "changed", False)) != provenance.changed:
            raise LifecycleStepFailed("route completion/provenance changed mismatch")
        return provenance
    if not isinstance(event, LifecycleMutationCompleted):
        raise LifecycleStepFailed(
            f"{domain} completion lacks atomic provenance receipt"
        )
    if event.domain != domain or not isinstance(event.payload, expected[domain]):
        raise LifecycleStepFailed(
            f"{domain} returned unexpected completion {type(event).__name__}"
        )
    payload_changed = getattr(event.payload, "changed", None)
    if (
        isinstance(payload_changed, bool)
        and payload_changed != event.provenance.changed
    ):
        raise LifecycleStepFailed(f"{domain} completion/provenance changed mismatch")
    return event.provenance


def _spec_payload(spec: LifecycleSpec) -> dict[str, object]:
    session = None if spec.session is None else asdict(spec.session)
    if session is not None:
        session["command"] = list(spec.session.command)
    harness = spec.harness.to_payload()
    return {
        "agentName": spec.agent_name,
        "actor": spec.actor,
        "harness": harness,
        "route": asdict(spec.route),
        "session": session,
        **(
            {"interruptionReason": spec.interruption_reason}
            if spec.interruption_reason is not None
            else {}
        ),
    }


def _operation_json(operation: LifecycleOperation) -> str:
    return json.dumps(
        {
            "operationId": operation.operation_id,
            "kind": operation.kind.value,
            "target": _spec_payload(operation.target),
            "source": None
            if operation.source is None
            else _spec_payload(operation.source),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _operation_from_json(payload: str) -> LifecycleOperation:
    raw = json.loads(payload)
    return LifecycleOperation(
        str(raw["operationId"]),
        LifecycleKind(str(raw["kind"])),
        _spec_from_payload(raw["target"]),
        None if raw["source"] is None else _spec_from_payload(raw["source"]),
    )


def _spec_from_payload(raw: dict[str, object]) -> LifecycleSpec:
    harness = raw["harness"]
    route = raw["route"]
    session = raw["session"]
    if not isinstance(harness, dict) or not isinstance(route, dict):
        raise ValueError("invalid lifecycle journal payload")
    return LifecycleSpec(
        agent_name=str(raw["agentName"]),
        actor=str(raw["actor"]),
        harness=HarnessLaunchProjection(
            harness=str(harness["provider"]),
            name=str(harness["name"]),
            headless=bool(harness["headless"]),
            args=tuple(str(item) for item in harness.get("args", [])),
            ownership=str(harness.get("ownership", "managed")),
            nickname=_optional_str(harness.get("nickname")),
            cwd=_optional_str(harness.get("cwd")),
            endpoint=_optional_str(harness.get("endpoint")),
            session_ref=_optional_str(harness.get("sessionRef")),
            command=tuple(str(item) for item in harness.get("command", [])),
            turn_timeout_seconds=_optional_float(harness.get("turnTimeoutSeconds")),
            idle_timeout_seconds=_optional_float(harness.get("idleTimeoutSeconds")),
            containerized=bool(harness.get("containerized", False)),
            pinned_owner=_optional_str(harness.get("pinnedOwner")),
            container_image=_optional_str(harness.get("containerImage")),
            execution_runtime=parse_execution_runtime(harness.get("executionRuntime")),
            model_provider=_optional_str(harness.get("modelProvider")),
            model=_optional_str(harness.get("model")),
        ),
        route=RouteSpec(
            str(route["route_id"]),
            str(route["liveliness_key"]),
            str(route["inbox_key"]),
            bool(route.get("advertise", True)),
        ),
        session=(
            None
            if session is None
            else SessionLifecycleSpec(
                cwd=str(session["cwd"]),
                command=tuple(str(item) for item in session["command"]),
                source=str(session["source"]),
                session_ref=str(session["session_ref"]),
                runtime=str(session.get("runtime", "claude_interactive")),
            )
        ),
        interruption_reason=_optional_str(raw.get("interruptionReason")),
    )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)
