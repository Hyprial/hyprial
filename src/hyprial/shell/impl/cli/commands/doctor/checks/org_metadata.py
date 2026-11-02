"""Doctor checks for the daemon's organization and routine documents.

Both probes read one daemon method and answer ``None`` when the daemon does not
report the field, so an older daemon keeps producing a shorter doctor report
instead of a fabricated pass. They live outside ``commands.py`` because the
doctor module is at its structural line budget and these are two self-contained
data probes.
"""

from __future__ import annotations

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject


def org_metadata_doctor_check() -> JsonObject | None:
    """Report organization metadata and directory-row data problems."""

    services = get_services()
    try:
        network = services.runtime.request("org.network", {}, timeout=2.0)
    except (services.CliError, ipc_errors.TransientDaemonError):
        return None
    if not isinstance(network, dict):
        return None
    raw_problems = network.get("dataProblems")
    if not isinstance(raw_problems, list):
        return None
    problems = [problem for problem in raw_problems if isinstance(problem, dict)]
    if not problems:
        return {
            "name": "org-metadata",
            "status": "ok",
            "detail": "organization metadata and directory rows are readable",
        }
    return {
        "name": "org-metadata",
        "status": "warn",
        "detail": f"{len(problems)} organization data problem(s)",
        "action": {
            "command": "hyprial org network --json",
            "description": "Inspect dataProblems and repair the reported organization data.",
        },
        "metrics": {"dataProblems": problems},
    }


def routine_health_doctor_check() -> JsonObject | None:
    """Surface quarantined routines and address migrations (2026-09-14).

    A routine whose stored spec no longer validates (e.g. a legacy bare-name
    ``escalate_to`` with no unique roster match) is quarantined: it stops
    scheduling and refuses resume until the spec is fixed. That must be
    visible from ``hyprial doctor`` -- the incident this fixes was fifteen
    days of silence. The address-migration ledger is reported as metrics so
    automatic rewrites are auditable without paging anyone.
    """
    services = get_services()

    try:
        audit = services.runtime.request("routine.audit", {}, timeout=2.0)
    except (services.CliError, ipc_errors.TransientDaemonError):
        return None
    if not isinstance(audit, dict):
        return None
    quarantined = [
        item
        for item in audit.get("quarantined", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    migrations = audit.get("addressMigrations", [])
    migration_list = (
        [
            {
                "routine": m["routine"],
                "field": m["field"],
                "before": m["before"],
                "after": m["after"],
            }
            for m in migrations
            if isinstance(m, dict)
        ]
        if isinstance(migrations, list)
        else []
    )
    if not quarantined and not migration_list:
        return None
    check: JsonObject = {
        "name": "routine-addresses",
        "status": "warn" if quarantined else "ok",
        "detail": (
            f"{len(quarantined)} routine(s) quarantined for schema faults "
            f"(scheduling stopped, resume refused); {len(migration_list)} stored "
            "address migration(s) applied automatically, resolved against THIS "
            "machine's agents roster (confirm each rewrite is the intended "
            "recipient — a same-name agent on another machine would have been "
            "redirected silently)"
        ),
        "metrics": {
            "quarantined": [item["name"] for item in quarantined],
            "addressMigrations": migration_list,
        },
    }
    if quarantined:
        check["action"] = {
            "command": "hyprial routine status <name> --json",
            "description": (
                "Fix the quarantined spec (full agent URI / route: / user: "
                "addresses only), then remove and re-add the routine."
            ),
        }
    return check
