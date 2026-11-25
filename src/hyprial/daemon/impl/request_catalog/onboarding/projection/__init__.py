"""Public onboarding.status projection builders split for structural limits."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..first_run import FirstRunSnapshot
    from ..state import AccountStatus, CallContext, OperationRecord, OperationStore


def status_snapshot(
    context: CallContext,
    records: Iterable[OperationRecord],
    *,
    account: AccountStatus | None = None,
    first_run: FirstRunSnapshot | None = None,
    username: str | None = None,
) -> dict:
    """Project visible receipts into the onboarding.status public shape."""

    visible = tuple(records)
    authenticated = context.owner is not None
    device_ready = any(
        record.kind == "device" and record.state == "ready" for record in visible
    )
    initialized = any(
        record.kind == "initialize" and record.state == "completed"
        for record in visible
    )
    if not authenticated:
        next_actions = ["login", "register"]
    elif not device_ready:
        next_actions = ["device.ensure"]
    elif not initialized:
        next_actions = ["initialize"]
    else:
        next_actions = []
    result = {
        "ok": True,
        "phase": context.phase,
        "authenticated": authenticated,
        "owner": context.owner,
        "username": username,
        "deviceReady": device_ready,
        "initialized": initialized,
        "operations": [
            {
                "operationId": record.operation_id,
                "kind": record.kind,
                "state": record.state,
                "receiptId": record.receipt_id,
            }
            for record in visible
        ],
        "nextActions": next_actions,
    }
    if first_run is not None:
        result["firstRun"] = first_run.as_status()
    if account is None:
        return result
    projected = dict(result)
    projected.update(account.as_status())
    return projected


def onboarding_status(
    store: OperationStore,
    context: CallContext,
    *,
    account: AccountStatus | None = None,
    first_run: FirstRunSnapshot | None = None,
    username: str | None = None,
) -> dict:
    """Project an OperationStore's visible receipts into onboarding.status."""

    return status_snapshot(
        context,
        store.visible(context),
        account=account,
        first_run=first_run,
        username=username,
    )


__all__ = ["onboarding_status", "status_snapshot"]
