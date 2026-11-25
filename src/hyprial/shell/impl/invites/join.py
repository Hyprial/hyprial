"""Shared explicit and pending invite join budget and stderr stage plan.

Decision record
- 2026-10-31 (fa42667d4): every CLI join path waits on the shared
  ``join-cli`` budget and announces the join stages on stderr.
- 2026-11-03 (org membership integration): login and ``org pending`` accept
  invites through ``org.invites.accept``, which runs the same daemon join, so
  that method rides the same budget and stage plan as ``org.join``.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyprial.kernel import MEMBERSHIP_BUDGETS
from hyprial.shell.impl.cli.output import progress

JOIN_METHODS = frozenset({"org.join", "org.invites.accept"})


def _announce(*, json_output: bool) -> None:
    message = (
        "Joining: redeeming invite / fetching roster, then announcing device and bootstrapping "
        f"OrgFS (up to {MEMBERSHIP_BUDGETS['join'].seconds:g}s)."
    )
    progress("org-join", message, json_output=json_output, detail=message)


def request_join(runtime, link: str, *, method: str = "org.join", json_output: bool = False):
    _announce(json_output=json_output)
    return runtime.request(method, {"link": link}, timeout=MEMBERSHIP_BUDGETS["join-cli"].seconds)


def invite_request(
    runtime, *, json_output: bool = False
) -> Callable[[str, Any], Any]:
    """Daemon request callable for invite pickup; joins wait on the join budget."""

    def request(method: str, params: Any) -> Any:
        if method not in JOIN_METHODS:
            return runtime.request(method, params)
        _announce(json_output=json_output)
        return runtime.request(
            method, params, timeout=MEMBERSHIP_BUDGETS["join-cli"].seconds
        )

    return request
