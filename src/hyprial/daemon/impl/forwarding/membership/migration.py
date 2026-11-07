# Decision record (brief degraded-start, 2026-11-03): the startup resume of a
# pending invite redemption must not abort daemon start. A receipt with
# ``redemption`` and ``state != 'complete'`` whose ``initialize_once`` fails
# against the invite authority is recorded durably and retried by the
# forwarding reconcile tick with bounded backoff (30s base, 600s cap);
# every other resume/import failure on this path stays fatal.
#
# Decision record (release 0.5.5.3.4, combining #2319 with #2484): a pending
# redemption whose receipt ``lastError.code`` is permanent for that invite
# (PERMANENT_REDEMPTION_CODES: the sidecar refuses the same request forever)
# is stale. Startup and the reconcile retry no longer replay it; the degraded
# record keeps the code with ``retryable: false`` and ``nextAttemptAtMs: null``
# so doctor/ps say a fresh invite is needed. Only an explicit ``org join``
# (redeem_once) sends an invite again; a different inviteId replaces the stale
# request and clears that tailnet's degraded record, and so does a completed
# redemption. Transient codes keep #2319's bounded-backoff retry.
#
# Decision record (release 0.5.5.3.4 review follow-up): the degraded record
# stays a single file, with a precedence rule instead of per-tailnet keys. A
# stale (``retryable: false``) record never replaces another tailnet's record;
# a retryable one replaces another tailnet's stale one; attempts and firstAt
# are only inherited from the same tailnet's record. Receipts are resumed in
# tailnetId order and stale ones are held after every resume, so restarts
# converge on the same record whatever the receipt glob order. A pending
# redemption that completes at startup clears its tailnet's record. A
# non-retryable record makes the reconcile tick return before any disk read.
# A retryable record whose receipt has since become stale (an explicit join of
# the same inviteId refused permanently) is rewritten as stale by the next tick
# or start, and a record written before ``retryable`` existed derives it from
# its code on read. A stale receipt found without a record (upgrade path) gets
# stage-tagged text rebuilt from lastError.code (identical to the first degrade
# for INVITE_EXPIRED), because the receipt's lastError keeps {code, at}.
# Known limit: with two transient tailnets only one holds the record; the other
# is retried at the next start, not by the tick.
"""One-time OFFLINE directory import. Never a runtime admission fallback."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from hyprial.identity import (
    DirectoryDevice, address_path, directory_owner_principal, org_from_space_name,
    protected_directory_doc_id, read_device_record,
)

from ..policy import ForwardingSidecarError
from .hardening.authority import select_authority
from .hardening.rows import dedupe_rows, parse_updated_at, validate_row
from .hardening.spaces import SKIP_NO_ROWS, SKIP_SPACE_MISSING, log_skip, space_skip_reason
from .hardening.store import store_tailnets
from .settings import durable_json_write as atomic_json_write, pin_authority, tailnet_settings


def start_before_transport(app: Any, *, listen: tuple[str, ...], connect: tuple[str, ...]) -> None:
    if connect:
        raise ValueError('Managed networking accepts sidecar slots only, not explicit Zenoh connect endpoints.')
    if any(not endpoint.startswith('unixsock-stream/') for endpoint in listen):
        raise ValueError('sidecar-managed networking requires private Unix listeners')
    app.zenoh_listen = listen
    # Decide everything the import needs from local files first. A refusal
    # (MIGRATION_AUTHORITY_MISSING) then costs no sidecar launch, no control
    # handshake and no 30 s membership round-trip before the start fails.
    plans = plan_migrations(app)
    app._start_forwarding_supervisor()
    migrate_for_app(app, plans)


def parse_legacy_member(value: dict, *, check_address=None) -> dict:
    # The compatibility parser is deliberately local to this one-time import.
    raw = dict(value)
    raw['clientPublicByServer'] = {}
    # A corrupted updatedAt (seen in production: "2026-10-197T005:52:31…") is
    # not a reason to lose the device; it only ranks the row as the oldest.
    updated_at = raw.get('updatedAt') if parse_updated_at(raw.get('updatedAt')) is not None else None
    if updated_at is None:
        raw['updatedAt'] = '1970-01-01T00:00:00Z'
    device = DirectoryDevice.from_record(raw)
    if check_address is not None and not check_address(device.address, device.server_public):
        raise ValueError('address/server mismatch')
    # Go verifies the embedded CBOR server key as part of the atomic init.
    return {'owner': device.owner.removeprefix('user:'), 'deviceId': device.device_id,
            'masterPublic': device.client_public, 'serverPublic': device.server_public,
            'clientPublicByServer': {}, 'address': device.address,
            'state': 'active', 'revision': 1, 'updatedAt': updated_at}


def receipt_path(home: Path, tailnet_id: str) -> Path:
    digest = hashlib.sha256(tailnet_id.encode()).hexdigest()
    return Path(home) / 'tailnet' / 'migration' / (digest + '.json')


EXPIRED_REDEMPTION_TEXT = ('The recorded invite expired (stage=invite-redemption); '
                           'retry with a fresh invite from the same authority.')


def initialize_once(home: Path, params: dict, rpc, *, initialized: bool, diagnostics=None) -> None:
    path = receipt_path(home, params['tailnetId'])
    try:
        receipt = json.loads(path.read_text())
    except FileNotFoundError:
        receipt = None
    if receipt and receipt['state'] == 'complete':
        if not initialized:
            raise ForwardingSidecarError('Membership store is missing after completed migration; restore its backup.', code='MEMBERSHIP_NOT_INITIALIZED')
        return
    if receipt and receipt['state'] == 'skipped':
        return  # plan_migrations rewrites it to pending first when the space becomes a candidate
    if receipt is None:
        receipt = {'state': 'pending', 'params': params, 'diagnostics': diagnostics or {}}
        atomic_json_write(path, receipt)
    # Lost replies replay the exact original normalized request, not new rows.
    pin_authority(home, params['tailnetId'], receipt['params']['authority'])
    try:
        if 'redemption' in receipt:
            rpc('invite-redeem', receipt['redemption'])
        else:
            rpc('membership-init', receipt['params'])
    except ForwardingSidecarError as error:
        receipt['lastError'] = {'code': error.code, 'at': datetime.now(UTC).isoformat(timespec='seconds')}
        atomic_json_write(path, receipt)
        if 'redemption' in receipt and error.code == 'INVITE_EXPIRED':
            raise ForwardingSidecarError(
                EXPIRED_REDEMPTION_TEXT, code=error.code, details=error.details,
            ) from error
        raise
    receipt.pop('lastError', None)
    receipt['state'] = 'complete'
    atomic_json_write(path, receipt)


PERMANENT_REDEMPTION_CODES = frozenset({
    'INVITE_EXPIRED', 'INVITE_INVALID', 'INVITE_CONSUMED', 'INVITE_VERSION_UNSUPPORTED',
})


def stale_redemption_code(receipt: dict) -> str | None:
    """The permanent failure code of a pending redemption receipt, or None when it may be replayed."""
    if receipt.get('state') == 'complete' or 'redemption' not in receipt:
        return None
    code = (receipt.get('lastError') or {}).get('code')
    return code if code in PERMANENT_REDEMPTION_CODES else None


def stale_redemption_error(code: str) -> str:
    """The degraded-record text for a stale receipt, matching what the first failed resume records."""
    if code == 'INVITE_EXPIRED':
        return EXPIRED_REDEMPTION_TEXT
    return (f'The recorded invite was refused with {code} (stage=invite-redemption); '
            'retry with a fresh invite from the same authority.')


def redeem_once(home: Path, invite: dict, rpc) -> None:
    tailnet_id = invite['tailnetId']
    params = {'tailnetId': tailnet_id, 'authority': invite['authority']}
    # Filtered status refuses unknown tailnets; initial redemption enumerates.
    status = rpc('membership-status', {})
    initialized = any(row['tailnetId'] == tailnet_id for row in status['tailnets'])
    path = receipt_path(home, tailnet_id)
    try:
        previous = json.loads(path.read_text())
    except FileNotFoundError:
        previous = None
    if previous is not None and previous['state'] == 'complete' and not initialized:
        raise ForwardingSidecarError('Restore the missing membership store before rejoining.', code='MEMBERSHIP_NOT_INITIALIZED')
    new_invite = (previous is None
        or previous.get('redemption', {}).get('invite', {}).get('inviteId') != invite['inviteId'])
    if previous is not None and previous['params']['authority'] != invite['authority']:
        explicit_repin = (previous['state'] == 'complete' and new_invite
            and tailnet_settings(home)['authorities'].get(tailnet_id) == invite['authority'])
        if not explicit_repin:
            raise ForwardingSidecarError('Explicit re-pin required.', code='AUTHORITY_PIN_MISMATCH')
    if new_invite:
        pin_authority(home, tailnet_id, invite['authority'])
        atomic_json_write(path, {'state': 'pending', 'params': params,
            'redemption': {'operationId': 'redeem-' + invite['inviteId'], 'invite': invite},
            'diagnostics': {'source': 'invite-v2'}})
        clear_degraded_redemption(home, tailnet_id=tailnet_id)
    initialize_once(home, params, rpc, initialized=initialized)
    clear_degraded_redemption(home, tailnet_id=tailnet_id)


def offline_rows(fs, space) -> tuple[list[dict], dict]:
    accepted, rejected, sources = [], [], []
    current = {member.user.removeprefix('user:') for member in fs.members(space.space_id)}
    owners = fs.listdir(space.space_id, 'directory/devices')
    for owner in owners:
        if owner.kind != 'dir':
            continue
        try:
            principal = directory_owner_principal(owner.name).removeprefix('user:')
            entries = fs.listdir(space.space_id, f'id:{owner.node_id}')
        except ValueError:
            rejected.append({'owner': owner.name, 'code': 'INVALID_OWNER'})
            continue
        for entry in entries:
            if entry.kind != 'doc' or not entry.name.endswith('.json'):
                continue
            path = f'directory/devices/{owner.name}/{entry.name}'
            try:
                if entry.doc_id != protected_directory_doc_id(space.space_id, path, space_owner=space.owner):
                    raise ValueError('document identity mismatch')
                text, _ = fs.read_text(space.space_id, path)
                sources.append((path, text))
                row = parse_legacy_member(json.loads(text))
                if row['owner'] != principal or row['deviceId'] != entry.name[:-5] or principal not in current:
                    raise ValueError('owner/device mismatch or removed member')
                code = validate_row(row)
                if code is not None:
                    rejected.append({'path': path, 'code': code})
                    continue
                accepted.append(row)
            except Exception as error:
                rejected.append({'path': path, 'code': getattr(error, 'code', 'INVALID_ROW')})
    # The sidecar refuses two active devices sharing any key (MEMBER_CONFLICT);
    # a renamed host leaves exactly that behind. Drop the older row here.
    kept, dropped = dedupe_rows(accepted)
    members = [{key: value for key, value in row.items() if key != 'updatedAt'} for row in kept]
    digest = hashlib.sha256(json.dumps(sorted(sources), separators=(',', ':')).encode()).hexdigest()
    return members, {'sourceDigest': digest, 'accepted': [row['deviceId'] for row in members],
                     'rejected': rejected, 'dropped': dropped,
                     'droppedDeviceIds': [item['deviceId'] for item in dropped]}


def plan_migrations(app: Any) -> list[dict]:
    """Decide every pending import from local files only; no sidecar involved.

    Raises MIGRATION_AUTHORITY_MISSING before any sidecar launch so a refused
    import fails the start phase in seconds, not after the control handshake.
    Only a ``complete`` receipt is authoritative. A ``pending`` (or ``skipped``)
    directory-import receipt is advisory: its request is recomputed from the
    current local files every start (skip rules, authority rule, row hygiene)
    and rewritten, the superseded params kept under ``diagnostics.supersededParams``.
    """
    record = read_device_record(app.hyprial_home)
    if record is None:
        return []
    settings = tailnet_settings(app.hyprial_home)
    initialized = store_tailnets(app.hyprial_home)
    fs = app._require_orgfs_runtime().facade
    plans = []
    advisory = {tailnet_id: receipt for tailnet_id, receipt in read_receipts(app.hyprial_home).items()
                if receipt.get('state') != 'complete' and 'redemption' not in receipt}
    for space in fs.spaces():
        if org_from_space_name(space.name) is None:
            continue
        tailnet_id = space.space_id
        receipt = advisory.pop(tailnet_id, None)
        if tailnet_id in initialized or (receipt is None and receipt_path(app.hyprial_home, tailnet_id).exists()):
            # An existing sidecar table is authoritative (a pending receipt is
            # then replayed against it); a complete receipt resumes on its own.
            continue
        reason = space_skip_reason(fs, space, app.hyprial_home, local_owner=record.owner)
        members, diagnostics = ([], {}) if reason else offline_rows(fs, space)
        if reason is None and not members:
            reason = SKIP_NO_ROWS
        if reason is not None:
            log_skip(app, space, reason)
            if receipt is not None:
                skip_receipt(app.hyprial_home, tailnet_id, receipt, reason)
            continue
        authority = select_authority(members, owner=space.owner, tailnet_id=tailnet_id,
                                     pin=settings['authorities'].get(tailnet_id),
                                     local_device_id=record.device_id,
                                     authority_default=settings['authorityDefault'])
        params = {'operationId': 'migration-' + hashlib.sha256(tailnet_id.encode()).hexdigest(),
                  'tailnetId': tailnet_id, 'authority': authority,
                  'migration': {'source': 'directory-0.5.5', 'sourceDigest': diagnostics['sourceDigest'], 'completed': True},
                  'members': members if record.device_id == authority['deviceId'] else []}
        if receipt is not None:
            recompute_receipt(app.hyprial_home, tailnet_id, receipt, params, diagnostics)
        plans.append({'tailnetId': tailnet_id, 'params': params, 'diagnostics': diagnostics})
    for tailnet_id, receipt in advisory.items():
        # The org space is gone from the local store: nothing left to import.
        if receipt.get('state') != 'skipped':
            skip_receipt(app.hyprial_home, tailnet_id, receipt, SKIP_SPACE_MISSING)
            log = getattr(app, '_log', None)
            if log is not None:
                log('info', 'zenoh', 'membership.migration.skipped', spaceId=tailnet_id, org=None, reason=SKIP_SPACE_MISSING)
    return plans


DEGRADED_REDEMPTION_FILE = 'redemption-degraded.json'
RETRY_BASE_MS = 30_000
RETRY_MAX_MS = 600_000
_REDEMPTION_LOCK = threading.Lock()


def degraded_redemption_path(home: Path) -> Path:
    return Path(home) / 'tailnet' / DEGRADED_REDEMPTION_FILE


def redemption_retry_delay_ms(attempts: int) -> int:
    return min(RETRY_BASE_MS * 2 ** max(attempts - 1, 0), RETRY_MAX_MS)


def read_degraded_redemption(home: Path) -> dict | None:
    """The durable degraded-redemption record, or None when absent/unreadable."""
    try:
        record = json.loads(degraded_redemption_path(home).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get('tailnetId'), str):
        return None
    if not isinstance(record.get('attempts'), int) or record['attempts'] < 1:
        return None
    if 'retryable' not in record:
        record['retryable'] = record.get('code') not in PERMANENT_REDEMPTION_CODES
        if not record['retryable']:
            record['nextAttemptAtMs'] = None
    return record


def record_degraded_redemption(home: Path, *, tailnet_id: str, invite_id: str | None,
                               code: str, error: str) -> dict:
    """Count one more failed redemption attempt and arm the next bounded-backoff retry.

    A permanent code (PERMANENT_REDEMPTION_CODES) arms no retry: the record says
    ``retryable: false`` with ``nextAttemptAtMs: null``. Another tailnet's record
    is kept (and the returned record not written) unless it is stale and this
    one is retryable.
    """
    now = datetime.now(UTC).isoformat(timespec='seconds')
    retryable = code not in PERMANENT_REDEMPTION_CODES
    with _REDEMPTION_LOCK:
        previous = read_degraded_redemption(home)
        other = previous is not None and previous['tailnetId'] != tailnet_id
        same = previous if previous is not None and not other else None
        attempts = (same['attempts'] if same else 0) + 1
        record = {'tailnetId': tailnet_id, 'inviteId': invite_id, 'code': code, 'error': error[:500],
                  'attempts': attempts,
                  'firstAt': same['firstAt'] if same and same.get('firstAt') else now,
                  'lastAt': now, 'retryable': retryable,
                  'nextAttemptAtMs': (time.time_ns() // 1_000_000 + redemption_retry_delay_ms(attempts)
                                      if retryable else None)}
        if not other or (retryable and previous.get('retryable') is False):
            atomic_json_write(degraded_redemption_path(home), record)
    return record


def clear_degraded_redemption(home: Path, *, tailnet_id: str | None = None) -> dict | None:
    """Remove the degraded record (only ``tailnet_id``'s when given); return what was removed."""
    with _REDEMPTION_LOCK:
        previous = read_degraded_redemption(home)
        if tailnet_id is not None and (previous is None or previous['tailnetId'] != tailnet_id):
            return None
        try:
            degraded_redemption_path(home).unlink()
        except OSError:
            pass
    return previous


def degrade_pending_redemption(app: Any, tailnet_id: str, receipt: dict,
                               error: ForwardingSidecarError) -> None:
    """Record one failed startup resume of a pending redemption; the daemon keeps serving."""
    invite = receipt.get('redemption', {}).get('invite', {})
    record = record_degraded_redemption(app.hyprial_home, tailnet_id=tailnet_id,
                                        invite_id=invite.get('inviteId') if isinstance(invite, dict) else None,
                                        code=error.code, error=str(error))
    log = getattr(app, '_log', None)
    if log is not None:
        log('warn', 'zenoh', 'membership.redemption.degraded', tailnetId=tailnet_id,
            code=error.code, error=str(error)[:300], attempts=record['attempts'],
            nextRetryAtMs=record['nextAttemptAtMs'], retryable=record['retryable'],
            detail=DEGRADED_DETAIL if record['retryable'] else STALE_DETAIL)


DEGRADED_DETAIL = ('pending org invite redemption is degraded; the daemon serves and the '
                   'redemption retries with bounded backoff until the authority is reachable')
STALE_DETAIL = ('the recorded org invite cannot be redeemed; the daemon serves and does not '
                'replay it; join again with a fresh invite from the same authority')


def hold_stale_redemption(app: Any, tailnet_id: str, receipt: dict, code: str) -> None:
    """Startup skip of a stale pending redemption: no RPC, the degraded record stays observable."""
    invite = receipt.get('redemption', {}).get('invite', {})
    invite_id = invite.get('inviteId') if isinstance(invite, dict) else None
    record = read_degraded_redemption(app.hyprial_home)
    if (record is None or record['tailnetId'] != tailnet_id or record.get('inviteId') != invite_id
            or record.get('retryable') is not False):
        record = record_degraded_redemption(app.hyprial_home, tailnet_id=tailnet_id, invite_id=invite_id,
                                            code=code, error=stale_redemption_error(code))
    log = getattr(app, '_log', None)
    if log is not None:
        log('warn', 'zenoh', 'membership.redemption.degraded', tailnetId=tailnet_id, code=code,
            attempts=record['attempts'], nextRetryAtMs=None, retryable=False, replayed=False,
            detail=STALE_DETAIL)


def hold_stale_redemptions(app: Any, receipts: dict[str, dict]) -> None:
    """Hold every stale pending redemption, in tailnetId order."""
    for tailnet_id, receipt in sorted(receipts.items()):
        code = stale_redemption_code(receipt)
        if code is not None:
            hold_stale_redemption(app, tailnet_id, receipt, code)


def retry_degraded_redemption(app: Any) -> None:
    """Maintenance-tick hook: finish a degraded redemption once its backoff window elapsed.

    Best effort and throttled by the durable record's ``nextAttemptAtMs``; a
    completed redemption clears the record and logs ``membership.redemption
    .completed`` so status and doctor go quiet without a restart.
    """
    home = getattr(app, 'hyprial_home', None)
    if home is None:
        return
    record = read_degraded_redemption(home)
    if record is None or record.get('retryable') is False:
        return
    if time.time_ns() // 1_000_000 < (record.get('nextAttemptAtMs') or 0):
        return
    supervisor = getattr(app, '_forwarding_supervisor', None)
    if supervisor is None:
        return
    tailnet_id = record['tailnetId']
    receipt = read_receipts(home).get(tailnet_id)
    if receipt is None or receipt.get('state') == 'complete' or 'redemption' not in receipt:
        clear_degraded_redemption(home)
        hold_stale_redemptions(app, read_receipts(home))
        return
    stale_code = stale_redemption_code(receipt)
    if stale_code is not None:
        hold_stale_redemption(app, tailnet_id, receipt, stale_code)
        return
    log = getattr(app, '_log', None)
    started_ms = time.time_ns() // 1_000_000
    try:
        initialize_once(home, receipt['params'], supervisor.membership_request, initialized=False)
    except ForwardingSidecarError as error:
        updated = record_degraded_redemption(home, tailnet_id=tailnet_id,
                                             invite_id=record.get('inviteId'),
                                             code=error.code, error=str(error))
        if log is not None:
            log('warn', 'zenoh', 'membership.redemption.retry-failed', tailnetId=tailnet_id,
                code=error.code, error=str(error)[:300], attempts=updated['attempts'],
                nextRetryAtMs=updated['nextAttemptAtMs'], retryable=updated['retryable'])
        return
    except Exception as error:  # noqa: BLE001 - the retry must never kill the tick
        # An unexpected failure is still a failed attempt: arm the same bounded
        # backoff so the tick never hot-loops on a due record.
        updated = record_degraded_redemption(home, tailnet_id=tailnet_id,
                                             invite_id=record.get('inviteId'),
                                             code=type(error).__name__, error=str(error))
        if log is not None:
            log('warn', 'zenoh', 'membership.redemption.retry-failed', tailnetId=tailnet_id,
                errorType=type(error).__name__, error=str(error)[:300],
                attempts=updated['attempts'], nextRetryAtMs=updated['nextAttemptAtMs'])
        return
    previous = clear_degraded_redemption(home)
    if log is not None:
        log('info', 'zenoh', 'membership.redemption.completed', tailnetId=tailnet_id,
            attempts=previous['attempts'] if previous else None,
            elapsedMs=time.time_ns() // 1_000_000 - started_ms)
    hold_stale_redemptions(app, read_receipts(home))


def read_receipts(home: Path) -> dict[str, dict]:
    receipts = {}
    for path in (Path(home) / 'tailnet' / 'migration').glob('*.json'):
        receipt = json.loads(path.read_text())
        receipts[receipt['params']['tailnetId']] = receipt
    return receipts


def recompute_receipt(home: Path, tailnet_id: str, receipt: dict, params: dict, diagnostics: dict) -> None:
    """Rewrite an advisory receipt to the recomputed request; keep lastError and the old params."""
    if receipt.get('state') == 'pending' and receipt['params'] == params:
        return
    diagnostics = dict(diagnostics)
    if receipt['params'] != params:
        diagnostics['supersededParams'] = {'params': receipt['params'], 'reason': 'recomputed-on-startup',
                                           'previousState': receipt.get('state'),
                                           'at': datetime.now(UTC).isoformat(timespec='seconds')}
    receipt = {key: value for key, value in receipt.items() if key not in ('skipReason', 'skippedAt')}
    receipt.update(state='pending', params=params, diagnostics=diagnostics)
    atomic_json_write(receipt_path(home, tailnet_id), receipt)


def skip_receipt(home: Path, tailnet_id: str, receipt: dict, reason: str) -> None:
    receipt.update(state='skipped', skipReason=reason, skippedAt=datetime.now(UTC).isoformat(timespec='seconds'))
    atomic_json_write(receipt_path(home, tailnet_id), receipt)


def migrate_for_app(app: Any, plans: list[dict] | None = None) -> None:
    supervisor = getattr(app, '_forwarding_supervisor', None)
    if supervisor is None:
        return
    rpc = supervisor.membership_request
    statuses = {item['tailnetId']: item for item in rpc('membership-status', {})['tailnets']}
    record = read_device_record(app.hyprial_home)
    if record is None:
        return
    if plans is None:
        plans = plan_migrations(app)
    # Resume authoritative receipts first: complete ones (store must exist),
    # invite redemptions, and pending imports whose table the sidecar already
    # holds (a lost reply; the sidecar replays by operationId). Pending
    # directory imports without a table were recomputed into `plans`.
    receipts = read_receipts(app.hyprial_home)
    for tailnet_id, receipt in sorted(receipts.items()):
        if receipt.get('state') == 'skipped' or stale_redemption_code(receipt) is not None:
            continue
        pending_redemption = receipt.get('state') != 'complete' and 'redemption' in receipt
        if receipt.get('state') == 'complete' or 'redemption' in receipt or tailnet_id in statuses:
            try:
                initialize_once(app.hyprial_home, receipt['params'], rpc, initialized=tailnet_id in statuses)
            except ForwardingSidecarError as error:
                if not pending_redemption:
                    raise
                degrade_pending_redemption(app, tailnet_id, receipt, error)
                continue
            if pending_redemption:
                clear_degraded_redemption(app.hyprial_home, tailnet_id=tailnet_id)
    hold_stale_redemptions(app, receipts)
    for plan in plans:
        tailnet_id = plan['tailnetId']
        receipt = read_receipts(app.hyprial_home).get(tailnet_id)
        if tailnet_id in statuses or (receipt is not None and receipt.get('state') == 'complete'):
            continue
        initialize_once(app.hyprial_home, plan['params'], rpc, initialized=False, diagnostics=plan['diagnostics'])
        rpc('roster-fetch', {'tailnetId': tailnet_id})
        rpc('member-announce', {'operationId': 'announce-' + uuid.uuid4().hex,
                              'tailnetId': tailnet_id,
                              'address': address_path(app.hyprial_home).read_text().strip(),
                              'clientPublicByServer': {}})
