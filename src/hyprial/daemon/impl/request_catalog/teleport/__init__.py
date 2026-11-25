# Decision record (file header; no inline comments by repository rule):
# - The handler lives in this module so ``source_service`` is a module-level
#   name that tests replace.  It is imported from the wiring module, which
#   stays light: the daemon composition is only loaded when a run starts.
# - A failed run is a ``DaemonRequestError`` with the run's code, message and
#   data; the registry checks that code and data against the declaration.
# - The owner's user agent is never moved, planned or not (UA1g): every
#   machine holds its own instance, so it is refused with USER_AGENT_IMMUTABLE
#   before the source service is built.
"""``teleport.run``: move a local agent to another node, or plan the move."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.daemon.impl.request_catalog.teleport.spec import (
    TELEPORT_RUN,
    TELEPORT_RUN_SPEC,
)
from hyprial.daemon.impl.teleport.errors import TeleportError
from hyprial.daemon.impl.teleport.source import TeleportRunError
from hyprial.daemon.impl.teleport.wiring.source import source_service
from hyprial.kernel import DaemonRequestError


def teleport_run(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> dict[str, Any]:
    from hyprial.daemon.impl.user_agent.naming import refuse_user_agent_target

    refuse_user_agent_target(getattr(app, "agents", None), params["agent"], "teleport")
    try:
        return source_service(app).run(
            params["agent"], to=params["to"], dry_run=params.get("dryRun", False)
        )
    except TeleportRunError as error:
        raise DaemonRequestError(error.code, error.message, error.data) from error
    except TeleportError as error:
        raise DaemonRequestError(error.code, error.message) from error


TELEPORT_BINDINGS = ((TELEPORT_RUN_SPEC, teleport_run),)

__all__ = ["TELEPORT_BINDINGS", "TELEPORT_RUN", "TELEPORT_RUN_SPEC", "teleport_run"]
