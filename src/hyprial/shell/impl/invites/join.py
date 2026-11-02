"""Shared explicit and pending invite join budget and stderr stage plan."""
from __future__ import annotations

from hyprial.kernel import MEMBERSHIP_BUDGETS
from hyprial.shell.impl.cli.output import progress


def request_join(runtime, link: str, *, method: str = "org.join", json_output: bool = False):
    message = (
        "Joining: redeeming invite / fetching roster, then announcing device and bootstrapping "
        f"OrgFS (up to {MEMBERSHIP_BUDGETS['join'].seconds:g}s)."
    )
    progress("org-join", message, json_output=json_output, detail=message)
    return runtime.request(method, {"link": link}, timeout=MEMBERSHIP_BUDGETS["join-cli"].seconds)
