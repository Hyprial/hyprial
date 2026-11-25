"""D5 org-cell binding refusals and unbound-org network annotations.

Decision notes (contract OC0 §3.9 D5):
- Legacy orgs (a directory space without a bound org cell) never get a
  silent backfill: member operations fail with the named ``ORG_CELL_UNBOUND``
  error pointing at the migration guide, and the owner-side leave sweep
  skips them while reporting them in its ``unbound`` result list.
- ``annotate_unbound_org_cells`` surfaces the same orgs in the
  ``org.network`` ``dataProblems`` list (reason ``org-cell-unbound``) so
  ``hyprial doctor`` can point at inspection and safe deletion.
- ``annotate_deleted_orgs`` reports orgs this home has not left whose OrgFS
  marker is ``deleted`` or whose local tailnet row is closed (reason
  ``org-deleted``, value the ``org leave`` command), so a member learns of an
  owner's deletion from ``org.network`` and doctor.
- The closed-tailnet half is one live ``membership-status`` probe bounded by
  ``NETWORK_STATUS_PROBE_SECONDS`` (under doctor's 2 s org-metadata wait).
  Any failure to read it (timeout, overloaded supervisor state, missing or
  failed sidecar, malformed status) keeps the view readable and adds a
  ``tailnet-status-unavailable`` data problem per org whose check did not
  run; there is no cached status that carries ``closed`` to fall back on.
- The binding callback is optional: platforms that cannot resolve org cells
  (member machines, degraded runtimes) answer ``None`` and the checks stand
  down instead of blocking healthy orgs.
- The refusals carry no wire data: the org methods declare these codes
  without a data schema, so the message names the org and the guide.
"""

from __future__ import annotations

from hyprial.kernel import ipc_errors

from typing import Any

from hyprial.identity import ORG_META_DOC, org_space_name

from hyprial.daemon.impl.org.network.lifecycle.deletion import MIGRATION_DOC

NETWORK_STATUS_PROBE_SECONDS = 1.0
TAILNET_STATUS_UNAVAILABLE = "tailnet-status-unavailable"


def require_bound_org_cell(service: Any, org: str, space_id: str) -> None:
    """D5: refuse member operations on orgs without a bound org cell."""

    from ..service import OrgNetworkError

    if service._org_cell_bound is None:
        return
    if service._org_cell_bound(org, space_id) is False:
        raise OrgNetworkError(
            ipc_errors.ORG_CELL_UNBOUND,
            f"org {org!r} has no org cell; it is a legacy organization. "
            f"Follow {MIGRATION_DOC} to rebuild or delete it",
        )


def refuse_deleted_or_unbound(service: Any, org: str, space_id: str) -> None:
    """Refuse member operations on deleted orgs, then apply the D5 check."""

    from ..service import OrgNetworkError

    if service._org_state(org) == "deleted":
        raise OrgNetworkError(
            ipc_errors.ORG_ALREADY_DELETED,
            f"org {org!r} is deleted; its rows are inert history",
        )
    require_bound_org_cell(service, org, space_id)


def skip_unbound_leave_processing(service: Any, org: str, space_id: str) -> bool:
    """Report (True) when leave processing must skip an unbound legacy org."""

    if service._org_cell_bound is None:
        return False
    if service._org_cell_bound(org, space_id) is False:
        service._log(
            "warn",
            "org",
            "org.leaves.unbound",
            org=org,
            migrationDoc=MIGRATION_DOC,
        )
        return True
    return False


def annotate_unbound_org_cells(
    service: Any, view: dict[str, Any], orgs: list[str]
) -> None:
    """Surface D5 legacy orgs (no bound org cell) as network data problems."""

    if service._org_cell_bound is None:
        return
    problems = view.get("dataProblems")
    if not isinstance(problems, list):
        return
    for org in orgs:
        space = service._find_space(org_space_name(org))
        if space is None or service._org_state(org) == "deleted":
            continue
        try:
            bound = service._org_cell_bound(org, space.space_id)
        except Exception:  # noqa: BLE001 - an unreadable runtime degrades to unknown
            bound = None
        if bound is not False:
            continue
        problems.append(
            {
                "org": org,
                "path": "cells/org",
                "reason": "org-cell-unbound",
                "value": MIGRATION_DOC,
            }
        )


def _closed_tailnets(service: Any) -> set[str] | None:
    """Closed tailnet ids, or None when the sidecar status cannot be read."""

    if service._network_isolated:
        return set()
    probe = getattr(service, "_network_probe", None) or (
        lambda: service._tailnet_raw_call("membership-status", {})
    )
    try:
        status = probe()
    except Exception as error:  # noqa: BLE001
        service._log(
            "warn",
            "org",
            "org.network.closed-unavailable",
            code=getattr(error, "code", type(error).__name__),
        )
        return None
    rows = status.get("tailnets") if isinstance(status, dict) else None
    if not isinstance(rows, list):
        service._log(
            "warn", "org", "org.network.closed-unavailable", code="INVALID_STATUS"
        )
        return None
    return {
        row["tailnetId"]
        for row in rows
        if isinstance(row, dict) and row.get("closed") and isinstance(row.get("tailnetId"), str)
    }


def annotate_deleted_orgs(
    service: Any, view: dict[str, Any], org: str | None
) -> None:
    """Surface owner-deleted orgs this home has not left as data problems."""

    from ..state import read_left_orgs

    problems = view.get("dataProblems")
    if not isinstance(problems, list):
        return
    left = set(read_left_orgs(service._home))
    candidates = [org] if org is not None else list(service._store.orgs())
    probed = False
    closed: set[str] | None = None
    for name in candidates:
        if name in left:
            continue
        space = service._find_space(org_space_name(name))
        if space is None:
            continue
        if service._org_state(name) == "deleted":
            path, reason, value = ORG_META_DOC, "org-deleted", f"hyprial org leave {name}"
        else:
            if not probed:
                closed, probed = _closed_tailnets(service), True
            if closed is None:
                problems.append(
                    {"org": name, "path": "tailnet", "reason": TAILNET_STATUS_UNAVAILABLE}
                )
                continue
            if space.space_id not in closed:
                continue
            path, reason, value = "tailnet", "org-deleted", f"hyprial org leave {name}"
        problems.append({"org": name, "path": path, "reason": reason, "value": value})


__all__ = [
    "MIGRATION_DOC",
    "NETWORK_STATUS_PROBE_SECONDS",
    "TAILNET_STATUS_UNAVAILABLE",
    "annotate_deleted_orgs",
    "annotate_unbound_org_cells",
    "refuse_deleted_or_unbound",
    "require_bound_org_cell",
    "skip_unbound_leave_processing",
]
