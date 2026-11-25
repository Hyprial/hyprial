# Decision record (I6-3, plan §H.6; I6-4):
# - Defines HTTP-exposed ``MethodSpec`` declarations, examples and handlers for
#   ``agent.cell.get`` and ``agent.cell.update``.
# - I6-4 registers them in the daemon request catalog; they replace the
#   retired ``adapter.pin*`` and ``authority.*`` requests.
"""Daemon request specs and handlers for ``agent.cell.*`` (I6-3, registered in I6-4)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import DaemonRequestError, ipc_errors

from ...registry import BoundMethod
from .examples import CELL_EXAMPLES
from .specs import (
    AGENT_CELL_GET,
    AGENT_CELL_GET_SPEC,
    AGENT_CELL_UPDATE,
    AGENT_CELL_UPDATE_SPEC,
    CELL_SPECS,
)

JsonObject = dict[str, Any]


def _require_local_operator(
    app: Any,
    params: Mapping[str, Any],
    trusted_message_origin: str | None,
) -> None:
    bind_check = getattr(app, "_identity_binding_operator", None)
    if callable(bind_check):
        operator, _caller_pid = bind_check(
            dict(params),
            trusted_message_origin=trusted_message_origin,
        )
        if not operator:
            raise DaemonRequestError(
                ipc_errors.CALLER_NOT_AUTHORIZED,
                "only the host local operator may update an agent cell",
            )
        return
    if trusted_message_origin not in (None, "ipc") or any(
        field in params for field in ("actor", "sessionRef")
    ):
        raise DaemonRequestError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "only the host local operator may update an agent cell",
        )


def _admit_local_operator(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> None:
    _require_local_operator(app, params, trusted_message_origin)


def cell_get(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    """Handle ``agent.cell.get`` via the in-memory cell runtime and actor snapshot."""

    from hyprial.daemon.impl.application.actors.cell_admin import get_agent_cell

    return get_agent_cell(app, params, trusted_message_origin=trusted_message_origin)


def cell_update(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    """Handle ``agent.cell.update`` for the local operator with CAS revision check."""

    from hyprial.daemon.impl.application.actors.cell_admin import update_agent_cell

    _require_local_operator(app, params, trusted_message_origin)
    return update_agent_cell(app, params, trusted_message_origin=trusted_message_origin)


CELL_BINDINGS = (
    (AGENT_CELL_GET_SPEC, cell_get),
    BoundMethod(
        AGENT_CELL_UPDATE_SPEC,
        cell_update,
        preauthorize=_admit_local_operator,
    ),
)

__all__ = [
    "AGENT_CELL_GET",
    "AGENT_CELL_GET_SPEC",
    "AGENT_CELL_UPDATE",
    "AGENT_CELL_UPDATE_SPEC",
    "CELL_BINDINGS",
    "CELL_EXAMPLES",
    "CELL_SPECS",
    "cell_get",
    "cell_update",
]
