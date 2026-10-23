"""The ``hyprial doctor`` Zenoh session check.

Split from ``commands.py`` to keep that module under the structural line
gate; the check owns the closed/degraded fail and the rebuild age window.
"""

from __future__ import annotations

import time

from hyprial.shell.impl.cli.commands.common.support import JsonObject


#: A degraded session retries with a 30 s capped backoff and a healthy
#: forwarding redial took 0.7-9.2 s on hq.  Warn only after a rebuild has
#: outlived both by a wide margin, so a normal redial never flaps doctor while
#: a stuck rebuild still surfaces.
_ZENOH_REBUILD_WARN_AFTER_MS = 60_000


def _metrics(result: JsonObject, base: dict[str, object]) -> dict[str, object]:
    metrics = dict(base)
    forwarding = result.get("forwarding")
    if not isinstance(forwarding, dict):
        metrics["connectionTelemetry"] = {
            "state": "unavailable",
            "reason": "forwarding-absent",
        }
    else:
        telemetry = forwarding.get("connectionTelemetry")
        metrics["connectionTelemetry"] = (
            telemetry
            if isinstance(telemetry, dict)
            else {"state": "unavailable", "reason": "capability-absent"}
        )
        for key in ("derpMapURL", "derpMapSource"):
            if isinstance(forwarding.get(key), str):
                metrics[key] = forwarding[key]
    return metrics


def _never_connected_check(
    result: JsonObject, base: dict[str, object]
) -> JsonObject | None:
    metrics = _metrics(result, base)
    telemetry = metrics.get("connectionTelemetry")
    slots = telemetry.get("slots") if isinstance(telemetry, dict) else None
    never_connected = [
        slot.get("peer")
        for slot in slots or ()
        if isinstance(slot, dict)
        and isinstance(slot.get("dialFailures"), int)
        and slot["dialFailures"] > 0
        and slot.get("splicesEstablished") == 0
    ]
    if not never_connected:
        return None
    return {
        "name": "zenoh",
        "status": "warn",
        "detail": (
            f"{len(never_connected)} forwarding peer(s) failed dialing and "
            "never established a splice"
        ),
        "metrics": metrics,
    }


def _zenoh_doctor_check(result: JsonObject) -> JsonObject:
    """Check the running daemon's explicit Zenoh transport endpoints.

    Discovery is disabled by design, so a daemon with neither a listen nor a
    connect endpoint is deterministically isolated from every other node.
    That is a hard failure (not a warning): the node cannot send or receive
    across the mesh no matter what.
    """

    zenoh = result.get("zenoh")
    if not isinstance(zenoh, dict):
        return {
            "name": "zenoh",
            "status": "warn",
            "detail": "daemon did not report Zenoh endpoints; upgrade the daemon",
        }
    session = zenoh.get("session")
    if isinstance(session, dict):
        state = session.get("state")
        if state in {"closed", "degraded"}:
            detail = (
                "the Zenoh session is "
                f"{state}; declarations and publishes cannot "
                "reach the mesh until it reopens"
            )
            attempts = session.get("recoveryAttempts")
            if isinstance(attempts, int) and attempts > 0:
                detail += f" ({attempts} reopen attempts"
                last = session.get("recoveryLastError")
                detail += f", last error: {last})" if last else ")"
            return {
                "name": "zenoh",
                "status": "fail",
                "detail": detail,
                "metrics": _metrics(result, session),
                "action": {
                    "command": (
                        "grep 'zenoh.session' ~/.hyprial/state/logs/daemon.jsonl"
                    ),
                    "description": (
                        "Inspect the session close/rebuild events; deploy the "
                        "session-reacquire fix or restart the daemon."
                    ),
                },
            }
        connection_check = _never_connected_check(result, session)
        if connection_check is not None:
            return connection_check
        if state == "rebuilding":
            since = session.get("rebuildingSinceMs")
            if not isinstance(since, int) or isinstance(since, bool):
                return {
                    "name": "zenoh",
                    "status": "warn",
                    "detail": (
                        "the Zenoh session reports a rebuild without a start "
                        "time; re-run doctor, or inspect the rebuild log"
                    ),
                    "metrics": _metrics(result, session),
                }
            age_ms = max(0, int(time.time() * 1000) - since)
            seconds = age_ms / 1000
            if age_ms < _ZENOH_REBUILD_WARN_AFTER_MS:
                return {
                    "name": "zenoh",
                    "status": "ok",
                    "detail": (
                        f"rebuild in flight for {seconds:.1f}s; declarations "
                        "and publishes resume on the new session"
                    ),
                    "metrics": _metrics(result, session),
                }
            return {
                "name": "zenoh",
                "status": "warn",
                "detail": (
                    f"the Zenoh session has been rebuilding for {seconds:.1f}s "
                    f"(threshold {_ZENOH_REBUILD_WARN_AFTER_MS / 1000:.0f}s); "
                    "the rebuild may be stuck"
                ),
                "metrics": _metrics(result, session),
                "action": {
                    "command": (
                        "grep 'zenoh.session' ~/.hyprial/state/logs/daemon.jsonl"
                    ),
                    "description": (
                        "Inspect the rebuild events; restart the daemon if the "
                        "session never returns to open."
                    ),
                },
            }
    listen = zenoh.get("listen")
    connect = zenoh.get("connect")
    if not isinstance(listen, list) or not isinstance(connect, list):
        listen = connect = ()
    if not listen and not connect:
        return {
            "name": "zenoh",
            "status": "fail",
            "detail": (
                "no explicit Zenoh listen/connect endpoints are configured and "
                "auto-discovery is disabled; this node cannot reach any other "
                "node (configuration check: no endpoints means no peers by "
                "construction)"
            ),
            "action": {
                "command": (
                    "hyprial init --listen tcp/<this-host>:<port> "
                    "--connect tcp/<peer-host>:<port>"
                ),
                "description": "Configure explicit Zenoh endpoints, then restart the daemon.",
            },
        }
    connection_check = _never_connected_check(
        result, session if isinstance(session, dict) else {}
    )
    if connection_check is not None:
        return connection_check
    return {
        "name": "zenoh",
        "status": "ok",
        "detail": (
            "explicit Zenoh endpoints are configured; configured is not "
            "reachable -- actual peer connectivity must be proven by a real "
            "send/ack"
        ),
        "metrics": _metrics(result, {"listen": list(listen), "connect": list(connect)}),
    }
