"""Doctor row for a degraded pending org invite redemption.

A daemon whose startup could not finish a pending invite redemption now
serves anyway and retries with bounded backoff; the daemon keeps a durable
``tailnet/redemption-degraded.json`` record so this check can report the
exact reason without a running daemon (same shape as the start-failures
check). The row is a warning, not a failure: the daemon is up, only
cross-machine membership completion is pending. A record with
``retryable: false`` names a permanent invite refusal (e.g. INVITE_EXPIRED):
the daemon no longer replays that invite, so the row asks for a fresh one.
"""
from __future__ import annotations

from pathlib import Path

from hyprial.shell.impl.cli.commands.common.support import JsonObject


def redemption_doctor_check(hyprial_home: Path) -> JsonObject | None:
    """One warn row while a pending redemption is degraded; None otherwise."""
    from hyprial.daemon import read_degraded_redemption

    record = read_degraded_redemption(hyprial_home)
    if record is None:
        return None
    tailnet_id = record['tailnetId']
    if record.get('retryable') is False:
        return _stale_redemption_check(record)
    detail = (
        f"pending org invite redemption for {tailnet_id} is degraded: "
        f"{record.get('code')}: {record.get('error')} "
        f"({record['attempts']} attempt(s), first {record.get('firstAt')}, "
        f"last {record.get('lastAt')}); the daemon serves and retries with "
        'bounded backoff, and the redemption completes without a restart '
        'once the authority is reachable'
    )
    return {'name': 'membership-redemption', 'status': 'warn', 'detail': detail,
            'metrics': dict(record),
            'action': {'command': 'hyprial org migration-status',
                       'description': ('Inspect the pending redemption receipt and the invite '
                                       'authority (hq); no action is needed while the authority '
                                       'is merely unreachable.')}}


def _stale_redemption_check(record: JsonObject) -> JsonObject:
    detail = (
        f"pending org invite redemption for {record['tailnetId']} cannot complete: "
        f"{record.get('code')}: {record.get('error')} "
        f"({record['attempts']} attempt(s), first {record.get('firstAt')}, "
        f"last {record.get('lastAt')}); the daemon serves and does not replay "
        'this invite'
    )
    return {'name': 'membership-redemption', 'status': 'warn', 'detail': detail,
            'metrics': dict(record),
            'action': {'command': 'hyprial org join -',
                       'description': ('Join again with a fresh invite from the same authority; '
                                       'the recorded invite is permanently refused.')}}
