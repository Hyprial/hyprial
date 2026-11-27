# Decision notes (service grant S5, 2026-11-26):
# - The consumer is the derived source service-connect talks to. It keeps one
#   immutable snapshot in memory (entries + protected devices) and replaces
#   it atomically; nothing it derives is ever written under service-connect.
# - refresh() is the only maintenance entry. service-connect drives it on
#   every reconcile tick and before every list/guide/connect. It is bounded
#   and never raises: on any failure the previous snapshot stays, and nothing
#   is written or deleted.
# - Which orgs count as "this device's": the orgs the directory store knows,
#   minus the ones this home left, and only where this owner is a current
#   member. Membership is re-checked every tick (one members() call), so a
#   removed member stops deriving at once. An org marked deleted derives
#   nothing; that is read when its fingerprint changes, not every tick.
# - Cost per unchanged tick, per org: one recursive listdir of the grants
#   tree, one of the devices tree, one stat of the org meta document and one
#   members() call; plus one lstat per stored Key entry and one local read of
#   the left-org list. No document is read, nothing is written or opened.
#   A changed fingerprint reloads that org whole: all its grants through S3's
#   loader and all its device rows through the store. Expiry changes no
#   bytes, so an answer also carries the earliest expiry of the grants it
#   derives from and is re-read when that moment has passed.
# - The credential is opened only behind can_open_grant_credential, as the
#   brief requires. For a grant that carries a credential and is effective
#   for this device the only way it answers no is "@members", which S3
#   already refuses to write; such a grant is treated as having no Key.
# - "Could not read" is not "no grant". A Key entry is withdrawn only when
#   every live org answered; while any org is unreadable and has no earlier
#   answer to fall back on, nothing is withdrawn.
# - Events are only service.grant.received (a Key entry was written, or
#   written again after it went missing) and service.grant.rejected (a grant
#   yields no Key, and the fixed reason). A rejection is reported once per
#   (org, grant id, reason) for as long as it lasts, so a steady tick emits
#   nothing.
# - The connections bookkeeping (connected/forget/retain/remembers) is the
#   ledger's connection set: service-connect reports which derived names are
#   mapped, so a connection is withdrawn together with its grant, also after
#   a restart.
# - Only the directory store's and the OrgFS facade's public surface is used.
#   GRANTS_DIR is the one path this package has to spell itself; a guard test
#   pins it to the store's own constant.
# - S5 fix (2026-11-27, critic 2): service-connect's seam tells the consumer
#   which device ids a hand-placed record disagrees about (contested). Every
#   grant on such a device is left out of the snapshot and rejected with
#   device-conflict, exactly like two disagreeing derived devices; its Key
#   entry is withdrawn like any grant that yields nothing. A changed set
#   re-derives at once, so the seam's own read already sees the result. The
#   device list keeps every candidate: the seam alone decides what conflicts.
# - S5 fix (2026-11-27, critic 3, as revised by the coordinator the same day):
#   an org's grants are scanned with bounds. Each grantor directory may hold
#   MAX_GRANTOR_GRANT_DOCUMENTS documents and GRANTOR_READ_BYTES_LIMIT listed
#   bytes; a grantor over either is "unknown" on its own: that grantor's part
#   of the previous answer stays (minus grants expired since, which needs no
#   read) while every other grantor is read afresh, new grants and
#   revocations included. A grantor that is unknown with no earlier answer to
#   keep derives nothing, and its Key entries are not withdrawn. More than
#   MAX_ORG_GRANTORS grantor directories, or PERIODIC_BUDGET_SECONDS spent, is
#   "unknown" for the whole org: the previous answer stays, or nothing is
#   derived and nothing withdrawn when there is none, and the same fingerprint
#   is not scanned again until it changes. service.grant.scan-limited (org,
#   phase "periodic", reason, and grantor for a grantor's limit) is emitted
#   when a limit starts or its reason changes, not on every tick it lasts.
"""The recipient side of service grants: derive, keep and withdraw what grants give this device."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.state import read_left_orgs
from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.daemon.impl.service_connect.derived import DerivedEntry, alternative_name
from hyprial.daemon.impl.service_connect.models import ProtectedDevice
from hyprial.daemon.impl.service_grants.models import normalize_bare_owner
from hyprial.daemon.impl.service_grants.queries import (
    GRANTOR_READ_BYTES_LIMIT,
    MAX_GRANTOR_GRANT_DOCUMENTS,
    MAX_ORG_GRANTORS,
    SERVICE_GRANTS_DIR,
    GrantScanLimited,
    ScanLimits,
    can_open_grant_credential,
    scan_org_service_grants,
)
from hyprial.identity import DIRECTORY_DIR, ORG_META_DOC, org_space_name

from .derive import (
    DEVICE_CONFLICT,
    KEY_GRANTED,
    KEY_NONE,
    KEY_UNAVAILABLE_STATE,
    NAME_CONFLICT,
    DerivedGrant,
    Rejection,
    assemble,
    derive_org,
)
from .keys import (
    LEDGER_RELPATH,
    Ledger,
    default_opener,
    sync_entry,
    withdraw_entry,
)
from .startup import SCAN_LIMITED_EVENT

RECEIVED_EVENT = "service.grant.received"
REJECTED_EVENT = "service.grant.rejected"
GRANTS_DIR = SERVICE_GRANTS_DIR
PERIODIC_BUDGET_SECONDS = 2.0
_COMPONENT = "service"
_UNKNOWN = object()


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What the grants give this device right now."""

    grants: tuple[DerivedGrant, ...] = ()
    devices: tuple[ProtectedDevice, ...] = ()

    def entries(self) -> tuple[DerivedEntry, ...]:
        return tuple(
            DerivedEntry(grant.name, alternative_name(grant.name, grant.grantor), grant.entry)
            for grant in self.grants
        )


@dataclass(slots=True)
class _OrgAnswer:
    """One org's last complete answer, with the fingerprint it answers for.

    ``expires_at_ms`` is the earliest expiry among the grants this answer
    derives from: an expiring grant changes nothing in the directory, so the
    answer is re-read when that moment passes even though the fingerprint is
    the same. ``unknown`` names the grantors this answer could not read and
    had no earlier answer for: their Key entries are not withdrawn.
    """

    fingerprint: object = _UNKNOWN
    wanted: tuple[tuple[DerivedGrant, ProtectedDevice], ...] = ()
    rejected: tuple[Rejection, ...] = ()
    expires_at_ms: int | None = None
    unknown: frozenset[str] = frozenset()

    def expired(self, now_ms: int) -> bool:
        return self.expires_at_ms is not None and self.expires_at_ms <= now_ms

    def unexpired(self, now_ms: int) -> _OrgAnswer:
        """The same answer without the grants that have expired since it was read."""

        if not self.expired(now_ms):
            return self
        wanted = tuple(
            pair for pair in self.wanted
            if pair[0].stored.grant.expires_at_ms is None or pair[0].stored.grant.expires_at_ms > now_ms
        )
        return _OrgAnswer(self.fingerprint, wanted, self.rejected, _earliest(wanted), self.unknown)


@dataclass(slots=True)
class _KeyPass:
    states: dict[tuple[str, str], tuple[str, str | None]] = field(default_factory=dict)
    rejected: list[Rejection] = field(default_factory=list)
    received: list[tuple[str, str, str, str]] = field(default_factory=list)


class GrantConsumer:
    """Derive service-connect entries and Key entries from the grants addressed to this device."""

    def __init__(
        self,
        *,
        orgfs: OrgFs | None,
        home: Path,
        state_dir: Path,
        owner: str | None,
        device_id: str | None,
        now_ms: Callable[[], int] | None = None,
        logger: Callable[..., None] | None = None,
        open_credential: Callable[..., bytes] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._fs = orgfs
        self._store = None if orgfs is None else OrgFsDirectoryStore(orgfs)
        self._home = Path(home).absolute()
        self._state_dir = Path(state_dir).absolute()
        self._owner = _bare_owner(owner)
        self._device_id = device_id
        self._now = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._monotonic = monotonic or time.monotonic
        self._logger = logger
        self._open_credential = open_credential
        self._ledger = Ledger(self._home / LEDGER_RELPATH)
        self._lock = threading.RLock()
        self._snapshot = Snapshot()
        self._answers: dict[str, _OrgAnswer] = {}
        self._reported: set[tuple[str, str, str, str]] = set()
        self._unresolved: tuple[str, ...] = ()
        self._contested: frozenset[str] = frozenset()
        self._limited: dict[str, tuple[object, str]] = {}
        self._grantor_limits: dict[tuple[str, str], str] = {}

    @property
    def active(self) -> bool:
        """Whether this home can prove who it is; otherwise nothing is derived, not even "@members"."""

        return self._store is not None and bool(self._owner) and bool(self._device_id)

    def refresh(self) -> None:
        """Re-derive; bounded, never raises, and keeps the last snapshot on failure."""

        if not self.active:
            with self._lock:
                self._snapshot = Snapshot()
            return
        try:
            self._refresh()
        except Exception:
            return

    def _refresh(self) -> None:
        with self._lock:
            left = set(read_left_orgs(self._home))
            answers = {
                org: self._answer_for(org)
                for org in self._store.orgs()
                if org not in left and self._store.is_member(org, f"user:{self._owner}")
            }
            self._answers = answers
            items, devices, contested = assemble(answer.wanted for answer in answers.values())
            held = [grant for grant in items if grant.device_id in self._contested]
            items = tuple(grant for grant in items if grant.device_id not in self._contested)
            rejected = [
                *contested,
                *(Rejection(grant.org, grant.grant_id, grant.grantor, DEVICE_CONFLICT) for grant in held),
                *(item for answer in answers.values() for item in answer.rejected),
                *(
                    Rejection(grant.org, grant.grant_id, grant.grantor, NAME_CONFLICT)
                    for name in self._unresolved
                    for grant in items
                    if grant.name == name
                ),
            ]
            self._unresolved = ()
            settled = all(answer.fingerprint is not _UNKNOWN for answer in answers.values())
            unknown = {(org, grantor) for org, answer in answers.items() for grantor in answer.unknown}
            keys = self._pass_keys(items, settled, unknown)
            grants = tuple(
                grant.with_key_state(*keys.states.get((grant.org, grant.grant_id), (KEY_NONE, None)))
                if grant.has_credential
                else grant
                for grant in items
            )
            self._snapshot = Snapshot(grants, tuple(devices.values()))
            self._report(sorted({*rejected, *keys.rejected}, key=_order), keys.received)

    def _answer_for(self, org: str) -> _OrgAnswer:
        previous = self._answers.get(org)
        try:
            fingerprint = _fingerprint(self._fs, org)
        except Exception:
            return previous if previous is not None else _OrgAnswer()
        if previous is not None and previous.fingerprint == fingerprint and not previous.expired(self._now()):
            return previous
        kept = previous.unexpired(self._now()) if previous is not None else _OrgAnswer()
        stopped = self._limited.get(org)
        if stopped is not None and stopped[0] == fingerprint:
            return kept
        try:
            answer, limited = self._load(org, fingerprint, previous)
        except GrantScanLimited as error:
            if stopped is None or stopped[1] != error.reason:
                self._emit("warn", SCAN_LIMITED_EVENT, org=org, phase="periodic", reason=error.reason)
            self._limited[org] = (fingerprint, error.reason)
            return kept
        except Exception:
            return previous if previous is not None else _OrgAnswer()
        self._limited.pop(org, None)
        self._note_grantor_limits(org, limited)
        return answer

    def _load(
        self, org: str, fingerprint: object, previous: _OrgAnswer | None
    ) -> tuple[_OrgAnswer, dict[str, str]]:
        now = self._now()
        if self._store.org_state(org) == "deleted":
            return _OrgAnswer(fingerprint), {}
        limits = ScanLimits(
            self._fs,
            deadline=self._monotonic() + PERIODIC_BUDGET_SECONDS,
            max_documents=MAX_GRANTOR_GRANT_DOCUMENTS,
            max_bytes=GRANTOR_READ_BYTES_LIMIT,
            max_grantors=MAX_ORG_GRANTORS,
            clock=self._monotonic,
        )
        scan = scan_org_service_grants(self._store, org, limits)
        wanted, rejected = derive_org(
            scan.grants,
            self._store.list_devices(org),
            owner=self._owner,
            device_id=self._device_id,
            now_ms=now,
        )
        unknown: frozenset[str] = frozenset()
        if scan.limited:
            base = previous.unexpired(now) if previous is not None else _OrgAnswer()
            if base.fingerprint is _UNKNOWN:
                unknown = frozenset(scan.limited)
            else:
                wanted = (*wanted, *(pair for pair in base.wanted if pair[0].grantor in scan.limited))
                rejected = (*rejected, *(item for item in base.rejected if item.grantor in scan.limited))
                unknown = frozenset(grantor for grantor in scan.limited if grantor in base.unknown)
        return _OrgAnswer(fingerprint, tuple(wanted), tuple(rejected), _earliest(wanted), unknown), scan.limited

    def _note_grantor_limits(self, org: str, limited: dict[str, str]) -> None:
        for grantor, reason in sorted(limited.items()):
            if self._grantor_limits.get((org, grantor)) != reason:
                self._emit("warn", SCAN_LIMITED_EVENT, org=org, phase="periodic", reason=reason, grantor=grantor)
        for key in [key for key in self._grantor_limits if key[0] == org and key[1] not in limited]:
            del self._grantor_limits[key]
        self._grantor_limits.update({(org, grantor): reason for grantor, reason in limited.items()})

    def _pass_keys(
        self, items: tuple[DerivedGrant, ...], settled: bool, unknown: Collection[tuple[str, str]] = ()
    ) -> _KeyPass:
        keys = _KeyPass()
        openable: dict[tuple[str, str], DerivedGrant] = {}
        for item in items:
            if not item.has_credential:
                continue
            if can_open_grant_credential(
                item.stored,
                recipient_owner=self._owner,
                recipient_device_id=self._device_id,
                now_ms=self._now(),
            ):
                openable[(item.org, item.grant_id)] = item
        if settled:
            for key, row in tuple(self._ledger.rows.items()):
                if key not in openable and (row.org, row.grantor) not in unknown:
                    self._withdraw(key)
        for key, item in openable.items():
            outcome = sync_entry(item, self._ledger, self._opener(), self._home)
            if outcome.rejection is not None:
                keys.rejected.append(outcome.rejection)
            if outcome.stored:
                keys.states[key] = (KEY_GRANTED, outcome.entry_id)
                if outcome.written:
                    keys.received.append((item.org, item.grant_id, item.grantor, outcome.entry_id))
            else:
                keys.states[key] = (KEY_UNAVAILABLE_STATE, None)
        return keys

    def _withdraw(self, key: tuple[str, str]) -> None:
        row = self._ledger.release(key)
        if row is not None:
            withdraw_entry(row, self._home)

    def _opener(self) -> Callable[[Any], bytes]:
        if self._open_credential is not None:
            return self._open_credential
        return lambda stored: default_opener(
            stored,
            home=self._home,
            state_dir=self._state_dir,
            owner=self._owner,
            device_id=self._device_id,
        )

    def _report(self, rejected: list[Rejection], received: list[tuple[str, str, str, str]]) -> None:
        current = {(item.org, item.grant_id, item.grantor, item.reason) for item in rejected}
        for key in sorted(current - self._reported):
            self._emit("warn", REJECTED_EVENT, org=key[0], grantId=key[1], grantor=key[2], reason=key[3])
        self._reported = current
        for org, grant_id, grantor, entry_id in sorted(received):
            self._emit("info", RECEIVED_EVENT, org=org, grantId=grant_id, grantor=grantor, entryId=entry_id)
        self._ledger.save()

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is not None:
            try:
                self._logger(level, _COMPONENT, event, **fields)
            except Exception:
                pass

    def entries(self) -> tuple[DerivedEntry, ...]:
        return self._snapshot.entries()

    def devices(self) -> tuple[ProtectedDevice, ...]:
        return self._snapshot.devices

    def remembers(self, name: str) -> bool:
        return name in self._ledger.connections

    def connected(self, name: str) -> None:
        with self._lock:
            self._ledger.connections.add(name)
            self._ledger.save()

    def forget(self, name: str) -> None:
        with self._lock:
            self._ledger.connections.discard(name)
            self._ledger.save()

    def retain(self, names: Collection[str]) -> None:
        with self._lock:
            if set(names) != self._ledger.connections:
                self._ledger.connections = set(names)
                self._ledger.save()

    def owns_entry(self, entry_id: str) -> bool:
        """Whether the ledger still claims this entry for some grant."""

        with self._lock:
            return any(row.entry_id == entry_id for row in self._ledger.rows.values())

    def unresolved(self, names: tuple[str, ...]) -> None:
        with self._lock:
            self._unresolved = self._unresolved + tuple(names)

    def contested(self, device_ids: tuple[str, ...]) -> None:
        with self._lock:
            if frozenset(device_ids) == self._contested:
                return
            self._contested = frozenset(device_ids)
            self.refresh()


def _order(rejection: Rejection) -> tuple[str, str, str, str]:
    return (rejection.org, rejection.grant_id, rejection.grantor, rejection.reason)


def _earliest(wanted: Collection[tuple[DerivedGrant, ProtectedDevice]]) -> int | None:
    expiries = [
        grant.stored.grant.expires_at_ms for grant, _device in wanted if grant.stored.grant.expires_at_ms is not None
    ]
    return min(expiries) if expiries else None


def _bare_owner(owner: object) -> str | None:
    """The owner as the directory spells it (``user:x`` and ``x`` are one owner)."""

    try:
        return normalize_bare_owner(owner)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _fingerprint(fs: OrgFs, org: str) -> tuple[object, ...]:
    name = org_space_name(org)
    matches = [space for space in fs.spaces() if space.name == name]
    if len(matches) != 1:
        raise OrgFsError("unknown-space", {"org": org, "space": name})
    space_id = matches[0].space_id
    try:
        meta = fs.stat(space_id, ORG_META_DOC)
        org_meta: tuple[object, ...] = (meta.node_id, meta.version, meta.size)
    except OrgFsError as error:
        if error.code != "unknown-doc":
            raise
        org_meta = ()
    return (
        _tree(fs, space_id, GRANTS_DIR),
        _tree(fs, space_id, DIRECTORY_DIR),
        org_meta,
        matches[0].owner,
    )


def _tree(fs: OrgFs, space_id: str, path: str) -> tuple[object, ...]:
    try:
        nodes = fs.listdir(space_id, path, recursive=True)
    except OrgFsError as error:
        if error.code == "unknown-doc":
            return ()
        raise
    return tuple(
        (node.path, node.node_id, node.version, node.size, node.deleted, node.content_state)
        for node in sorted(nodes, key=lambda node: node.path)
    )


__all__ = ["RECEIVED_EVENT", "REJECTED_EVENT", "GrantConsumer", "Snapshot"]
