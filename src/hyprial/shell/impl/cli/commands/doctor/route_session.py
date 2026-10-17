"""Read-only route/zenoh session staleness probe for ``hyprial doctor``."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject


def _route_session_doctor_check(home: Path | None = None) -> JsonObject | None:
    """Log-level, read-only check for stale route/zenoh session handles.

    Main criterion: a ``daemon.lifecycle.operation.failed`` with
    ``errorCode=ROUTE_IO_FAILED``, or an ``inbox.fetch_receipt.publish_failed``
    whose detail says ``session closed``, in the window. If no ``kind=create``
    ``operation.settled``/``succeeded`` follows the last failure, new workers
    are still blocked (FAIL); if one does, it healed but recurred (WARN). No
    session close/open comparison: short-lived sessions and balanced rebuilds
    make that false-positive-heavy. No worker is started.
    """

    services = get_services()
    root = Path(home) if home is not None else services._hyprial_home()
    path = root / "state" / "logs" / "daemon.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 12_000_000))
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return None

    def parse(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    fails: list[tuple[datetime, str]] = []
    inbox: list[tuple[datetime, str]] = []
    creates: list[datetime] = []
    saw_rows = False
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        when = parse(item.get("ts"))
        if when is None or when < cutoff:
            continue
        saw_rows = True
        event = str(item.get("event") or "")
        if event.endswith("operation.failed") and item.get("errorCode") == "ROUTE_IO_FAILED":
            fails.append((when, str(item.get("ts"))))
        elif event == "inbox.fetch_receipt.publish_failed" and "session closed" in str(
            item.get("detail") or ""
        ).lower():
            # The inbox holds the same session authority: a receipt publish
            # that fails with session closed is the same stale handle.
            inbox.append((when, str(item.get("ts"))))
        elif (
            (event.endswith("operation.settled") or event.endswith("operation.succeeded"))
            and item.get("kind") == "create"
            and item.get("errorCode") is None
            and item.get("level") in (None, "info")
        ):
            # settled carries no state: success is only implied by an info
            # level and the absence of an errorCode. Never count an errored
            # settled as a heal.
            creates.append(when)
    if not saw_rows:
        return None
    # One criterion for both the heal comparison and the reported timestamp:
    # the newest failure across the route and inbox sources, not the last
    # element of the concatenated lists (which reported the inbox timestamp
    # whenever an earlier inbox row existed).
    newest = max((*fails, *inbox), default=None, key=lambda item: item[0])
    last = newest[0] if newest is not None else None
    last_ts = newest[1] if newest is not None else None
    healed = last is not None and any(when > last for when in creates)
    metrics = {
        "windowMin": 60,
        "routeIoFailed": len(fails),
        "inboxFetchReceiptFailed": len(inbox),
        "lastFailureTs": last_ts,
        "createSucceededAfterLastFailure": healed,
    }
    if not fails and not inbox:
        return {
            "name": "route-session",
            "status": "ok",
            "detail": "no ROUTE_IO_FAILED or inbox session-closed publish failure in the last hour",
            "metrics": metrics,
        }
    if healed:
        return {
            "name": "route-session",
            "status": "warn",
            "detail": (
                f"{len(fails)} ROUTE_IO_FAILED lifecycle failure(s) and "
                f"{len(inbox)} inbox fetch-receipt failure(s) in the last hour, "
                "but a kind=create operation succeeded afterwards: it "
                "healed once; watch for recurrence"
            ),
            "metrics": metrics,
        }
    return {
        "name": "route-session",
        "status": "fail",
        "detail": (
            f"{len(fails)} ROUTE_IO_FAILED lifecycle failure(s) and "
            f"{len(inbox)} inbox fetch-receipt failure(s) in the last hour, "
            "with no kind=create success after the last one: session reuse is "
            "still broken"
        ),
        "metrics": metrics,
        "action": {
            "command": "grep 'ROUTE_IO_FAILED' ~/.hyprial/state/logs/daemon.jsonl",
            "description": "Inspect the failed creates; deploy the route-session reacquire fix.",
        },
    }
