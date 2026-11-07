"""Read the explicit delivery-path probe without touching daemon history.

Decision (owner, 2026-11-06): a probe that was never run, or a route family
that was not configured, is ``warn`` -- not ``fail``.  A fresh install has no
probe yet and a user without an adapter route cannot create one, so failing
there would make ``hyprial doctor`` red with no way to turn it green.  A probe
that was run and did not deliver stays ``fail``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from hyprial.shell.impl.cli.commands.common.support import JsonObject

PROBE_FILENAME = "delivery-paths-probe.json"
PROBE_MAX_AGE_SECONDS = 24 * 60 * 60
_SETUP = "hyprial adapter route add <adapter> <route>=<native-chat-id>"
_PROBE = "hyprial message probe-delivery-paths --route route:<adapter>:<route>"


def delivery_paths_doctor_check(home: Path | None = None) -> JsonObject:
    path = (Path(home) if home is not None else _home()) / "state" / PROBE_FILENAME
    setup = f"{_SETUP} && {_PROBE}"
    if not path.is_file():
        return {
            "name": "delivery-paths",
            "status": "warn",
            "detail": (
                "NOT-EVALUABLE: no delivery-path probe yet; cannot prove agent/user/route sends. "
                f"create a route:<adapter>:<route> with `{_SETUP}` and run the probe"
            ),
            "action": {"command": setup, "description": "Run the explicit delivery-path probe."},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {
            "name": "delivery-paths",
            "status": "fail",
            "detail": f"delivery-path probe is unreadable: {type(error).__name__}",
            "action": {"command": setup, "description": "Rewrite the delivery-path probe."},
        }
    epoch = payload.get("epoch") if isinstance(payload, dict) else None
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return {
            "name": "delivery-paths",
            "status": "fail",
            "detail": "delivery-path probe has no numeric epoch",
            "action": {"command": setup, "description": "Rewrite the delivery-path probe."},
        }
    age = max(0, int(time.time() - epoch))
    families = payload.get("per_family") if isinstance(payload, dict) else None
    metrics = {
        "epoch": int(epoch),
        "ageSeconds": age,
        "maxAgeSeconds": PROBE_MAX_AGE_SECONDS,
        "perFamily": families if isinstance(families, dict) else {},
    }
    if age > PROBE_MAX_AGE_SECONDS:
        return {
            "name": "delivery-paths",
            "status": "warn",
            "detail": "NOT-EVALUABLE: stale_probe (older than 24h)",
            "metrics": {**metrics, "evaluation": "NOT-EVALUABLE"},
            "action": {"command": setup, "description": "Rerun the delivery-path probe."},
        }
    if not isinstance(families, dict):
        return {
            "name": "delivery-paths",
            "status": "fail",
            "detail": "delivery-path probe has no per_family map",
            "action": {"command": setup, "description": "Rewrite the delivery-path probe."},
        }
    missing = [
        family
        for family in ("agent", "user", "route")
        if not (
            isinstance(families.get(family), dict)
            and families[family].get("accepted") is True
        )
    ]
    route_entry = families.get("route")
    route_unconfigured = (
        "route" in missing
        and isinstance(route_entry, dict)
        # the probe writes an explicit ``"target": None`` when no --route was given
        and "target" in route_entry
        and route_entry["target"] is None
    )
    failed = [family for family in missing if not (family == "route" and route_unconfigured)]
    if not failed and route_unconfigured:
        return {
            "name": "delivery-paths",
            "status": "warn",
            "detail": (
                "agent and user delivery paths accepted; route not configured "
                f"(optional; create one with `{_SETUP}`)"
            ),
            "metrics": {**metrics, "routeConfigured": False},
            "action": {"command": setup, "description": "Optionally configure a route and rerun the probe."},
        }
    missing = failed
    if missing:
        command = setup if "route" in missing else "hyprial message probe-delivery-paths"
        return {
            "name": "delivery-paths",
            "status": "fail",
            "detail": (
                "delivery-path probe did not accept: "
                + ", ".join(missing)
                + "; route targets need `"
                + _SETUP
                + "`"
            ),
            "metrics": metrics,
            "action": {"command": command, "description": "Fix the failed family and rerun the probe."},
        }
    return {
        "name": "delivery-paths",
        "status": "ok",
        "detail": "agent, user, and route delivery paths each accepted a tagged probe",
        "metrics": metrics,
    }


def _home() -> Path:
    from hyprial.shell.impl.cli.commands.common.services import get_services

    return Path(get_services()._hyprial_home())
