"""Serving reauthentication from the CLI: one daemon operation, rendered."""

from __future__ import annotations

from typing import Any

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject

from . import first_login


def run_serving_relogin(*, no_open: bool, json_output: bool, started: bool) -> JsonObject:
    """Begin ``auth.relogin``, wait once, and map the daemon's terminal state."""

    services = get_services()
    begun = _begin(services)
    operation_id = begun.get("operationId")
    if not isinstance(operation_id, str):
        raise services.CliError(
            "INVALID_RESPONSE", "the daemon began a relogin without an operation id"
        )
    verification = begun.get("verification")
    try:
        if isinstance(verification, dict):
            first_login._show_device(services, verification, json_output=json_output)
            if not no_open:
                first_login._open(str(verification.get("verificationUri")))
        answer = first_login._wait(services, operation_id)
    except KeyboardInterrupt:
        first_login._interrupted(services, operation_id, json_output=json_output)
        raise
    return first_login._result(
        services,
        answer,
        verification if isinstance(verification, dict) else {},
        started=started,
        json_output=json_output,
    )


def _begin(services: Any) -> dict[str, Any]:
    result = services.runtime.request(
        "auth.relogin",
        {
            "operationKey": f"cli-relogin-{services.uuid4().hex}",
            "until": "serving",
        },
    )
    if not isinstance(result, dict):
        raise services.CliError("INVALID_RESPONSE", "auth.relogin result must be an object")
    return result
