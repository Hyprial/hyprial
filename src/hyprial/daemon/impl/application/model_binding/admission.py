"""Read one incarnation/member binding and pin it before any launch effects.

Local binding revisions are distinct from grant revisions. Cell intent includes
only the selected member's identity, harness and model, not unrelated members.
"""

from dataclasses import replace
from hashlib import sha256

from hyprial.error import codes
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.desired_state.bindings.models import canonical


class ModelLaunchAdmissionError(DaemonRequestError):
    permanent_start_failure = True
    start_precondition_failure = True


def refuse(code, phase, reason):
    raise ModelLaunchAdmissionError(
        code, f"{code}: {phase}/{reason}", data={"phase": phase, "reason": reason}
    )


def status(app, agent, member):
    try:
        result = app.desired_state.model_binding_store.status(agent.agent_id, member)
    except Exception:
        refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "bind", "state-unreadable")
    if result.cleanup is not None:
        refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "bind", "destroy-cleanup-pending")
    if result.pending is not None:
        refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "bind", "operation-in-progress")
    return result


def snapshot(binding):
    return {
        "bindingRef": binding.binding_ref,
        "bindingRevision": binding.binding_revision,
    }


def intent_digest(member, harness, model):
    return sha256(
        canonical({"member": member, "harness": harness, "model": model}).encode()
    ).hexdigest()


def connection_provider(configuration):
    if configuration.auth_kind == "native":
        from hyprial.daemon.impl.harnesses.model_routes.native import target_for

        return target_for(configuration).target
    from hyprial.daemon.impl.harnesses.pi.bindings import connection_provider_id

    return connection_provider_id(
        configuration.connection_ref, configuration.config_revision
    )


def validate_cell(app, agent, member, binding):
    from hyprial.daemon.impl.application.actors.cell_admin.service import (
        _require_agent_cell,
    )
    from hyprial.daemon.impl.cells.structure import parse_structure

    try:
        cell = _require_agent_cell(getattr(app, "_cell_runtime", None), agent)
        structure = parse_structure(cell)
        selected = next(
            row
            for row in structure.members
            if row.name == member and row.role == "harness"
        )
        config = binding.configuration
        agrees = (
            not structure.problems
            and selected.kind == config.harness
            and selected.model == config.model
            and intent_digest(member, selected.kind, selected.model)
            == config.intent_digest
        )
    except Exception:
        agrees = False
    if not agrees:
        refuse(codes.MODEL_CONFIGURATION_CONFLICT, "bind", "cell-intent-mismatch")


def select(app, spec, *, allow_unpinned=False):
    from hyprial.daemon.impl.composition.cells.reconcile import launch_member_name

    agent = app.agents.get(spec.name)
    if agent is None:
        if spec.model_binding is not None:
            refuse(codes.MODEL_CONFIGURATION_CONFLICT, "bind", "revision-mismatch")
        return None
    member = launch_member_name(agent, spec)
    binding = status(app, agent, member).binding
    if binding is None:
        if agent.capabilities.get("modelConfigurationRequired") is True:
            refuse(codes.MODEL_CONFIGURATION_INCOMPLETE, "bind", "configuration-required")
        if spec.model_binding is not None:
            refuse(codes.MODEL_CONFIGURATION_CONFLICT, "bind", "revision-mismatch")
        return None
    if spec.model_binding != snapshot(binding) and not (
        allow_unpinned and spec.model_binding is None
    ):
        refuse(codes.MODEL_CONFIGURATION_CONFLICT, "bind", "revision-mismatch")
    config = binding.configuration
    if (
        (config.auth_kind == "connection" and config.harness != "pi")
        or config.harness != spec.harness
    ):
        refuse(codes.MODEL_COMBINATION_UNSUPPORTED, "admission", "runtime-option")
    if config.auth_kind == "native" and spec.args and not (
        config.harness == "pi" and spec.args == ("--approve",)
    ):
        refuse(codes.MODEL_COMBINATION_UNSUPPORTED, "admission", "runtime-option")
    if config.auth_kind == "native" and config.harness != "pi" and spec.session_ref is not None:
        refuse(codes.MODEL_RESUME_MISMATCH, "resume", "binding-changed")
    if config.mode != "headless" or not spec.headless:
        refuse(codes.MODEL_COMBINATION_UNSUPPORTED, "admission", "mode")
    if spec.model not in (None, config.model) or spec.model_provider not in (
        None,
        connection_provider(config),
    ):
        refuse(codes.MODEL_COMBINATION_UNSUPPORTED, "admission", "runtime-option")
    validate_cell(app, agent, member, binding)
    return binding


def pin(app, spec):
    binding = select(app, spec, allow_unpinned=True)
    if binding is None:
        return spec
    return replace(
        spec,
        model_binding=snapshot(binding),
        model=binding.configuration.model,
        model_provider=connection_provider(binding.configuration),
    )
