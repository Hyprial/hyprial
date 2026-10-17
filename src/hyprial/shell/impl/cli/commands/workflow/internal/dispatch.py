"""Shared tier report; the retired dispatch command stays unregistered."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.support import JsonObject


def tier_report(tier: str | None, *, probe: bool = False, runner=None) -> JsonObject:
    """Render static choices and recorded facts, optionally read-only diagnosis."""
    from hyprial.daemon import (
        active_profile,
        candidate_json,
        diagnose,
        recorded_candidate_json,
        resolve,
        workflow_reminders,
    )
    from hyprial.kernel import TIERS

    if tier is not None and tier not in TIERS:
        raise ValueError("tier must be fast, strong, or super")
    profile = active_profile()
    names = (tier,) if tier is not None else tuple(TIERS)
    document: JsonObject = {
        "ok": True,
        "reminders": workflow_reminders(),
        "tiers": {
            name: [recorded_candidate_json(candidate, profile) for candidate in TIERS[name]]
            for name in names
        },
        "selection": {
            name: candidate_json(resolve(name).selected) for name in names
        },
    }
    if probe:
        document["diagnostics"] = {
            name: [reading.to_json() for reading in diagnose(name, profile=profile, runner=runner)]
            for name in names
        }
    return document
