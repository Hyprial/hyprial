"""Public model operations select an owned incarnation before reading credentials.

Saving configuration does not start a process or send a model request. Runtime
authorization is rechecked by the common launch authority on every launch.
"""

from hyprial.error import codes
from hyprial.identity import AgentError, SecretResolutionError, resolve_agent_secrets
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.application.actors.cell_admin.service import _resolve_agent
from hyprial.daemon.impl.desired_state.bindings.models import Configuration, ModelBindingError, text
from hyprial.daemon.impl.harnesses.model_routes.connection import select_secret
from hyprial.daemon.impl.harnesses.pi.bindings.errors import PiModelBindingError
from hyprial.daemon.impl.provider_keys.launch import launch_resolver
from ..admission import intent_digest
from .cell import refuse
from .coordinator import Coordinator
from .settings import operation_limit


def integer(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        refuse(codes.MODEL_CONFIGURATION_INVALID, "invalid-connection", "validate")
    return value


def target(app, params):
    agent = _resolve_agent(app, params["name"])
    if agent.owner != app.owner:
        raise DaemonRequestError(codes.TARGET_NOT_AUTHORIZED,
                                 "Model configuration requires an owned local agent.")
    member = params.get("member", agent.uri)
    if member == agent.actor:
        member = agent.uri
    return agent, text(member)


def authorize(app, agent, config):
    try:
        secrets = resolve_agent_secrets(
            registry=app._agent_registry, hyprial_home=app.hyprial_home,
            agent_name=agent.actor,
            owner_secrets=launch_resolver(app, registry=app._agent_registry),
        )
        if config.auth_kind == "native":
            from hyprial.daemon.impl.harnesses.model_routes.native import select_native

            select_native(config, secrets, app._agent_registry, agent)
        else:
            select_secret(config, secrets)
    except PiModelBindingError as error:
        refuse(error.code, error.data["reason"], error.data["phase"])
    except SecretResolutionError as error:
        reason = "revoked" if error.category in {"revoked", "grant-revoked"} else "unreadable"
        refuse(codes.MODEL_AUTH_UNAVAILABLE, reason, "authorize")


def configuration(raw, member):
    data = dict(raw)
    if "configRevision" in data:
        data["configRevision"] = integer(data["configRevision"])
    data["intentDigest"] = intent_digest(member, data["harness"], data["model"])
    config = Configuration.from_json(data)
    if config.auth_kind == "connection" and config.harness != "pi":
        refuse(codes.MODEL_COMBINATION_UNSUPPORTED, "runtime-option", "admission")
    if config.mode != "headless":
        refuse(codes.MODEL_COMBINATION_UNSUPPORTED, "mode", "admission")
    return config


def operation_json(operation):
    if operation is None:
        return None
    plan = operation.coordination
    return {
        "operationId": operation.operation_id, "phase": operation.phase,
        "requestDigest": operation.request_digest,
        "expectedBindingRevision": operation.expected_revision,
        "resultBindingRevision": operation.result_revision,
        "coordinationPhase": plan.phase if plan else None,
        "expectedCellRevision": plan.expected_revision if plan else None,
        "observedCellRevision": plan.observed_revision if plan else None,
    }


def report(coordinator, operation_id=None, result=None):
    state = coordinator._call("status", coordinator.agent.agent_id, coordinator.member)
    binding = state.binding
    if result is None:
        if operation_id is not None:
            result = next((row for row in state.operations if row.operation_id == operation_id), None)
        else:
            result = state.pending or next((row for row in state.operations
                if binding is not None and row.operation_id == binding.operation_id), None)
    observation = {"cellId": coordinator.cell.cell_id, "revision": None, "intentMatches": None}
    try:
        cell, intent = coordinator.cell.read()
        observation["revision"] = cell.revision
        if binding is not None:
            config = binding.configuration
            observation["intentMatches"] = (
                intent == {"kind": config.harness, "model": config.model}
                and config.intent_digest == intent_digest(coordinator.member, config.harness, config.model)
            )
    except DaemonRequestError:
        pass
    return {
        "ok": True, "agentId": coordinator.agent.agent_id, "member": coordinator.member,
        "binding": None if binding is None else {
            "bindingRef": binding.binding_ref, "bindingRevision": binding.binding_revision,
            "configuration": binding.configuration.to_json(),
        },
        "operation": operation_json(result), "cell": observation,
        "cleanupPending": state.cleanup is not None, "runtime": "not-verified",
    }


def handle(app, method, params):
    try:
        agent, member = target(app, params)
        coordinator = Coordinator(app, agent, member, authorize=lambda c: authorize(app, agent, c))
        operation_id = params.get("operationId")
        if operation_id is not None:
            text(operation_id)
        if method == "status":
            return report(coordinator, operation_id)
        expected = integer(params["expectedBindingRevision"])
        if method == "cancel":
            result = coordinator.cancel(operation_id, expected)
        else:
            config = configuration(params["configuration"], member)
            result = coordinator.configure(
                operation_id, expected, integer(params["expectedCellRevision"]), config,
                operation_limit=operation_limit(app.hyprial_home),
            )
        return report(coordinator, result=result)
    except ModelBindingError as error:
        refuse(error.code, error.reason, error.phase)
    except AgentError:
        raise DaemonRequestError(codes.INVALID_ARGUMENT, "Invalid local agent reference.") from None
