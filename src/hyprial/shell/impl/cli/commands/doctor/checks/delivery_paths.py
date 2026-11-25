"""Read the explicit delivery-path probe without touching daemon history.

Decision (owner, 2026-11-06): a probe that was never run is ``warn`` -- not
``fail``.  A fresh install has no probe yet, so failing there would make
``hyprial doctor`` red before the user could act.  A probe that was run and
did not deliver stays ``fail``.

Decision (I4b, owner items Q0-a and Q0-d, 2026-11-23): the ``route:``
address is retired, so the probe has two families, agent and user.  A
``route`` entry left in an older probe file is ignored.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from hyprial.shell.impl.cli.commands.common.support import JsonObject

PROBE_FILENAME = "delivery-paths-probe.json"
PROBE_MAX_AGE_SECONDS = 24 * 60 * 60
_PROBE = "hyprial message probe-delivery-paths"
_FAMILIES = ("agent", "user")


def delivery_paths_doctor_check(home: Path | None = None) -> JsonObject:
    path = (Path(home) if home is not None else _home()) / "state" / PROBE_FILENAME
    if not path.is_file():
        return {
            "name": "delivery-paths",
            "status": "warn",
            "detail": "NOT-EVALUABLE: no delivery-path probe yet; cannot prove agent/user sends",
            "action": {"command": _PROBE, "description": "Run the explicit delivery-path probe."},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {
            "name": "delivery-paths",
            "status": "fail",
            "detail": f"delivery-path probe is unreadable: {type(error).__name__}",
            "action": {"command": _PROBE, "description": "Rewrite the delivery-path probe."},
        }
    epoch = payload.get("epoch") if isinstance(payload, dict) else None
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return {
            "name": "delivery-paths",
            "status": "fail",
            "detail": "delivery-path probe has no numeric epoch",
            "action": {"command": _PROBE, "description": "Rewrite the delivery-path probe."},
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
            "action": {"command": _PROBE, "description": "Rerun the delivery-path probe."},
        }
    if not isinstance(families, dict):
        return {
            "name": "delivery-paths",
            "status": "fail",
            "detail": "delivery-path probe has no per_family map",
            "action": {"command": _PROBE, "description": "Rewrite the delivery-path probe."},
        }
    missing = [
        family
        for family in _FAMILIES
        if not (
            isinstance(families.get(family), dict)
            and families[family].get("accepted") is True
        )
    ]
    if missing:
        return {
            "name": "delivery-paths",
            "status": "fail",
            "detail": "delivery-path probe did not accept: " + ", ".join(missing),
            "metrics": metrics,
            "action": {"command": _PROBE, "description": "Fix the failed family and rerun the probe."},
        }
    return {
        "name": "delivery-paths",
        "status": "ok",
        "detail": "agent and user delivery paths each accepted a tagged probe",
        "metrics": metrics,
    }


def _home() -> Path:
    from hyprial.shell.impl.cli.commands.common.services import get_services

    return Path(get_services()._hyprial_home())
