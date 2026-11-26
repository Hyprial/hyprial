"""Explicit creation keeps a durable no-defaults intent across partial failure.

Identity creation, credential authorization and Cell coordination are separate
commits. An incomplete new identity remains visible and cannot start until the
normal model coordinator finalizes a binding. Native login is never copied from
another agent or the operator home; it uses the existing explicit login method.
"""

from hyprial.error import codes
from hyprial.identity import SecretResolutionError, SecretResolver
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.harnesses.pi.bindings.errors import PiModelBindingError
from hyprial.daemon.impl.desired_state.bindings.models import ModelBindingError, record, text
from .cell import refuse
from .coordinator import Coordinator
from .service import authorize, configuration, report
from .settings import operation_limit


def _grant_connection(app, agent, config):
    from hyprial.daemon.impl.provider_keys.legacy import _gate_closed

    if not _gate_closed(app):
        refuse(codes.MODEL_AUTH_UNAVAILABLE, "unreadable", "authorize")
    rows = SecretResolver(app.hyprial_home, app._agent_registry).list_model_services(include_connections=True)
    selected = [(entry, field, route) for entry, _, routes in rows for field, route in routes.items()
                if route.get("connectionRef") == config.connection_ref]
    if len(selected) != 1:
        refuse(codes.MODEL_AUTH_UNAVAILABLE, "missing" if not selected else "ambiguous", "authorize")
    entry, field, route = selected[0]
    if route["configRevision"] != config.config_revision:
        refuse(codes.MODEL_CONFIGURATION_CONFLICT, "revision-mismatch")
    app._handle_agent("agent.secret.grant", {
        "actor": agent.actor, "grantId": "model-connection", "source": "user-provider",
        "entryId": entry, "fieldName": field, "environmentNames": ["OPENAI_API_KEY"], "revision": 1,
    }, expected_secret_agent={"uri": agent.uri, "createdAtMs": agent.created_at_ms, "agentId": agent.agent_id})


def create(app, params):
    try:
        intent = record(params["modelConfiguration"], ("operationId", "configuration"))
        operation_id = text(intent["operationId"])
        config = configuration(intent["configuration"], "new-agent")
        if config.auth_kind == "native":
            from hyprial.daemon.impl.harnesses.model_routes.native import target_for

            target_for(config)
        if any(name in params for name in ("harness", "provider", "model", "runtime", "existing")):
            refuse(codes.MODEL_CONFIGURATION_INVALID, "invalid-connection", "validate")
        if params.get("preferredHarness", config.harness) != config.harness:
            refuse(codes.MODEL_CONFIGURATION_INVALID, "invalid-connection", "validate")
        capabilities = params.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            refuse(codes.MODEL_CONFIGURATION_INVALID, "invalid-connection", "validate")
    except PiModelBindingError as error:
        refuse(error.code, error.data["reason"], error.data["phase"])
    except (KeyError, TypeError, ModelBindingError):
        refuse(codes.MODEL_CONFIGURATION_INVALID, "invalid-connection", "validate")
    create_params = {key: value for key, value in params.items() if key != "modelConfiguration"}
    create_params.update(preferredHarness=config.harness,
                         capabilities={**capabilities, "modelConfigurationRequired": True})
    created = app._handle_agent("agent.create", create_params)
    identity = created["agent"]
    try:
        agent = app.agents.require(params["name"])
        if agent.agent_id != identity["agentId"]:
            refuse(codes.MODEL_CONFIGURATION_CONFLICT, "agent-incarnation-changed")
        config = configuration(intent["configuration"], agent.uri)
        coordinator = Coordinator(app, agent, agent.uri, authorize=lambda c: authorize(app, agent, c))
        if config.auth_kind == "connection":
            _grant_connection(app, agent, config)
        cell, _ = coordinator.cell.read()
        result = coordinator.configure(operation_id, 0, cell.revision, config,
                                       operation_limit=operation_limit(app.hyprial_home))
        return {**created, "providerDefault": None,
                "modelConfiguration": report(coordinator, result=result)}
    except Exception as error:
        cause = getattr(error, "code", codes.DAEMON_ERROR)
        if isinstance(error, SecretResolutionError):
            cause = codes.MODEL_AUTH_UNAVAILABLE
        if cause not in codes.ALL_CODES:
            cause = codes.DAEMON_ERROR
        raise DaemonRequestError(codes.MODEL_CONFIGURATION_INCOMPLETE,
            "Agent identity was created; model configuration is incomplete. Read its status, authorize the selected credential and configure it before starting.",
            data={"phase": "bind", "reason": "configuration-required", "agentId": identity["agentId"],
                  "actor": identity["uri"], "operationId": operation_id, "causeCode": cause}) from None
