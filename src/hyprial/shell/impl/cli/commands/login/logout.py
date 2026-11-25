# Decision record (L1, owner 2026-11-21: ``hyprial logout`` is one ``auth.logout``):
# - Principle.  The CLI only presents.  It ensures the daemon with the existing
#   launcher, asks for confirmation, sends exactly one write request
#   (``auth.logout{operationKey, confirm: true, expectedEpoch: <ping.epoch>}``)
#   and renders the receipt.  It takes no identity lock, proves no process exit,
#   stops no daemon and reads or removes no login file: the serving daemon
#   removes the artifacts and asks for its own stop, the bootstrap daemon
#   completes an empty home with ``removed: []``.  The daemon is started with
#   the shared start budget; logout has no start-timeout option of its own.
# - Missing home.  Reported not-logged-in without creating the home, asking or
#   starting a daemon (nothing can be logged in there).
# - ``daemon.stopRequested``.  True exactly when the daemon that answered was
#   serving and attempted a removal (any receipt other than a completed one that
#   removed nothing), which is when the serving gateway requests its own stop.
#   The command does not wait for or claim the exit.  "Serving" is the ping
#   phase ``serving`` or ``reconciled`` (the serving daemon after its first
#   reconcile round), the same pair the GUI admission and request-http
#   readiness accept; a reconciled daemon was reported false before
#   (cm-l1-fix, 2026-11-21).
# - Outcomes.  A completed receipt is success (``logged-out``, or
#   ``not-logged-in`` when nothing was removed); any other receipt state is
#   ``LOGOUT_FAILED`` with the receipt in ``data``; a refusal keeps the
#   daemon's own code; a lost reply is ``SUBMIT_OUTCOME_UNKNOWN``, never retried.
# - Codes.  Transport failures are recognised by their registered
#   ``hyprial.error.codes`` code (precedent: ``first_login.py``), so this module
#   imports nothing from ``hyprial.kernel``.
"""``hyprial logout``: one daemon ``auth.logout``, rendered."""

from __future__ import annotations

from typing import Any

from hyprial.error.codes import (
    DAEMON_DISCONNECTED,
    DAEMON_UNAVAILABLE,
    IPC_TIMEOUT,
    SUBMIT_OUTCOME_UNKNOWN,
)
from hyprial.shell.impl.cli.commands.common.support import (
    DAEMON_START_IDLE_BUDGET_SECONDS,
    JsonObject,
)
from hyprial.shell.impl.cli.output import confirm

PROBE_SECONDS = 5.0
REQUEST_SECONDS = 60.0
LOGOUT_FAILED = "LOGOUT_FAILED"
SERVING_PHASES = frozenset({"serving", "reconciled"})


def run_logout(services: Any, *, yes: bool, json_output: bool) -> JsonObject:
    """Ensure the daemon, confirm, send one ``auth.logout`` and map its receipt."""

    home = services._hyprial_home()
    if not home.is_dir():
        return {"status": "not-logged-in", "home": str(home), "removed": []}
    probe = _ensure_daemon(services)
    if not yes:
        if json_output or not services._stdin_isatty():
            raise services.CliError(
                "CONFIRMATION_REQUIRED",
                "logout from a non-interactive command requires --yes",
            )
        if not confirm(f"Remove the user login from {home}?"):
            raise services.CliError("CANCELLED", "logout cancelled")
    params: JsonObject = {
        "operationKey": f"cli-logout-{services.uuid4().hex}",
        "confirm": True,
    }
    epoch = probe.get("epoch")
    if isinstance(epoch, str) and epoch:
        params["expectedEpoch"] = epoch
    receipt = _request(services, params)
    removed = receipt.get("removed")
    removed = list(removed) if isinstance(removed, list) else []
    state = receipt.get("state")
    stop_requested = probe.get("phase") in SERVING_PHASES and not (
        state == "completed" and not removed
    )
    result: JsonObject = {
        "status": "logged-out" if removed else "not-logged-in",
        "home": str(home),
        "removed": removed,
        "operationId": receipt.get("operationId"),
        "receiptId": receipt.get("receiptId"),
        "daemon": {"stopRequested": stop_requested},
    }
    if state != "completed":
        raise services.CliError(
            LOGOUT_FAILED,
            f"the daemon recorded this logout as {state}",
            {**result, "state": state},
        )
    return result


def _ensure_daemon(services: Any) -> JsonObject:
    try:
        probe = services._daemon_probe(timeout=PROBE_SECONDS)
    except RuntimeError as error:
        if getattr(error, "code", None) != DAEMON_UNAVAILABLE:
            raise
        services._launch_daemon_process(
            ready_timeout=DAEMON_START_IDLE_BUDGET_SECONDS, autoinstall=False
        )
        probe = services._daemon_probe(timeout=PROBE_SECONDS)
    if not isinstance(probe, dict):
        raise services.CliError("INVALID_RESPONSE", "the daemon ping result must be an object")
    return probe


def _request(services: Any, params: JsonObject) -> JsonObject:
    try:
        receipt = services.runtime.request("auth.logout", params, timeout=REQUEST_SECONDS)
    except RuntimeError as error:
        if getattr(error, "code", None) not in (DAEMON_DISCONNECTED, IPC_TIMEOUT):
            raise
        raise services.CliError(
            SUBMIT_OUTCOME_UNKNOWN,
            "the daemon stopped answering while this logout was running; its "
            f"outcome is not known to this command ({error})",
            {
                "operationKey": params["operationKey"],
                "transport": getattr(error, "code", type(error).__name__),
                "nextSteps": ["hyprial daemon status", "hyprial logout"],
            },
        ) from error
    if not isinstance(receipt, dict):
        raise services.CliError("INVALID_RESPONSE", "auth.logout result must be an object")
    return receipt
