# Decision record:
# - A daemon in the ownerless bootstrap phase answers ``ping`` and nothing that
#   needs an account.  The wire refusal for such a method reaches the CLI as a
#   ``services.CliError`` (the request port turns every wire refusal into it),
#   never as a kernel ``DaemonRequestError``; callers therefore match on the
#   code.  The set of codes lives with ``daemon status`` (``commands.py``),
#   which already holds the registered ``ipc_errors`` names: ``ACCOUNT_REQUIRED``
#   is the refusal of a declared method, ``METHOD_NOT_FOUND`` the answer for a
#   name the bootstrap host does not serve (``ps``) and ``CALLER_NOT_AUTHORIZED``
#   the GUI-hosted bootstrap's refusal.
# - Every importer reaches this module from inside a function.  It is not on
#   the post-install restart surface, so the eager CLI import graph must not
#   load it.  It also imports no backend module of its own (import ratchet);
#   the phase-checked refusal helper lives in ``commands.py`` beside the codes.
"""What the CLI reports for a daemon that is up but has no account yet."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "bootstrap_daemon_doctor_check",
    "probed_daemon",
]

_PROBED_DAEMON_KEYS = (
    "running",
    "pid",
    "epoch",
    "nodeId",
    "owner",
    "socket",
    "phase",
)


def probed_daemon(
    services: Any, *, restore_default: bool | None = None
) -> tuple[dict[str, Any], Any]:
    """The daemon portion of ``ping`` and its ``restorePending`` answer."""

    probe = services._daemon_probe(timeout=2.0)
    daemon = {key: probe[key] for key in _PROBED_DAEMON_KEYS if key in probe}
    return daemon, probe.get("restorePending", restore_default)


def bootstrap_daemon_doctor_check(daemon: Mapping[str, Any]) -> dict[str, Any] | None:
    """A ``warn`` for a live bootstrap daemon, or None when this is not one."""

    if daemon.get("running") is not True or daemon.get("phase") != "bootstrap":
        return None
    return {
        "name": "daemon",
        "status": "warn",
        "detail": (
            f"daemon is running in bootstrap (pid {daemon.get('pid')}) with no "
            "account; daemon-backed checks were not evaluated"
        ),
        "action": {
            "command": "hyprial login",
            "description": "Sign in so the running daemon can serve this home.",
        },
    }
