"""Incarnation-scoped cleanup bridges existing destroy reservations and model storage.

Markers precede destructive effects; only an authoritative absence permits purge.
Registry uncertainty retains the marker, and ordinary stop never invokes cleanup.
"""

from hyprial.error import codes
from hyprial.kernel import DaemonRequestError


def incomplete(entity_deleted=None):
    return DaemonRequestError(
        codes.MODEL_CONFIGURATION_INCOMPLETE,
        "Agent model cleanup did not complete; reread the Agent before retrying.",
        data={
            "phase": "bind",
            "reason": "destroy-cleanup-pending",
            "entityDeleted": entity_deleted,
        },
    )


def mark_destroy(app, agent, attempt):
    try:
        state = app.desired_state.load()
        previous = next(
            (
                row
                for row in state.model_agent_lifecycle
                if row.agent_id == agent.agent_id
            ),
            None,
        )
        return app.desired_state.model_binding_store.mark_cleanup(
            agent.agent_id,
            agent.entity_token,
            attempt,
            expected_attempt=None if previous is None else previous.destroy_attempt,
        )
    except Exception:
        raise incomplete(False) from None


def rollback_destroy(app, agent, attempt):
    """A failed destroy can release only its marker for the still-live incarnation."""
    try:
        current = app._agent_registry.get_by_agent_id(agent.agent_id)
        if current is None or current.entity_token != agent.entity_token:
            return
        app.desired_state.model_binding_store.rollback_cleanup(
            agent.agent_id,
            agent.entity_token,
            attempt,
        )
    except Exception:
        return


def purge_deleted(app, agent_id, entity_token, attempt):
    try:
        current = app._agent_registry.get_by_agent_id(agent_id)
    except Exception:
        raise incomplete() from None
    if current is not None:
        raise incomplete(False)
    try:
        app.desired_state.model_binding_store.purge_destroyed_agent(
            agent_id,
            entity_token,
            attempt,
        )
    except Exception:
        raise incomplete(True) from None


def recover_destroyed(app):
    """Recover only recorded, proven-deleted incarnations; keep uncertain ones fenced."""
    markers = app.desired_state.load().model_agent_lifecycle
    for marker in markers:
        try:
            current = app._agent_registry.get_by_agent_id(marker.agent_id)
            if current is not None:
                continue
            purge_deleted(
                app, marker.agent_id, marker.entity_token, marker.destroy_attempt
            )
        except Exception:
            log = getattr(app, "_log", None)
            if callable(log):
                log(
                    "warn",
                    "agents",
                    "agent.model_cleanup.pending",
                    agentId=marker.agent_id,
                    reason="destroy-cleanup-pending",
                )


def cleanup_summary(app, agent):
    state = app.desired_state.load()
    bindings = tuple(
        row for row in state.model_bindings if row.agent_id == agent.agent_id
    )
    operations = tuple(
        row for row in state.model_binding_operations if row.agent_id == agent.agent_id
    )
    from hashlib import sha256
    from hyprial.daemon.impl.desired_state.bindings.models import canonical

    digest = sha256(
        canonical(
            {
                "bindings": [row.to_json() for row in bindings],
                "operations": [row.to_json() for row in operations],
            }
        ).encode()
    ).hexdigest()
    return {"bindings": len(bindings), "operations": len(operations)}, digest
