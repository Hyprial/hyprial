"""The org-membership actor: Casdoor changes reach OrgFS one user at a time.

Design-casdoor-group-sync §3.  The owner node keeps the OrgFS member tables
in line with the Casdoor group, which is the authority:

- a webhook or a local membership command names ONE user; the actor reads
  that user from Casdoor (one call per owned org) and acts on that user only;
- a full pass over the OrgFS members runs at start and then every
  ``BACKSTOP_INTERVAL_SECONDS`` to catch a lost webhook.  The group-scoped
  endpoint cannot list a group, so the pass reads each OrgFS member; users
  who are in the group but not in OrgFS (``casdoorOnly``) are only learnt
  from webhooks.

Removal rules (org-improver and infra-ops, 2026-10-08):
- ``not-member`` removes.  Casdoor only says it after its permission check,
  for a user that exists.
- ``no-user`` is also what a wrong name mapping produces, before any
  permission check.  It removes only a user this node has seen as a member
  before (``confirmed``), and otherwise reports ``casdoorMissing``.
- Breaker: a full pass in which more than max(1, 20 % of members) are
  ``no-user`` removes nobody, not even ``not-member`` ones, and raises
  ``syncHalted`` until a later pass is clean.  While halted, a single-user
  ``not-member`` still removes; ``no-user`` only reports.
- The owner is never removed.
- Anything else is an error: nothing changes, ``lastError`` records it.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from hyprial.daemon.impl.org.network.casdoor.client import (
    CasdoorGroupError,
    casdoor_group,
    casdoor_user,
)
from hyprial.kernel import (
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
    EffectCompleted,
    EffectLane,
    EffectRequest,
    GenerationScheduler,
    canonical_user_uri,
)

BACKSTOP_INTERVAL_SECONDS = 6 * 3600.0
BREAKER_SHARE = 0.2
REMOVAL_WINDOW_SECONDS = 600.0
_PENDING_LIMIT = 256

_Key = tuple[str, str | None, str | None]  # ("user", org|None, user) | ("all", org|None, None)
_Logger = Callable[..., None]


@dataclass(frozen=True, slots=True)
class _Enqueue:
    key: _Key


@dataclass(frozen=True, slots=True)
class _Backstop:
    generation: int


@dataclass(frozen=True, slots=True)
class _Work:
    key: _Key
    confirmed: dict[str, frozenset[str]]
    halted: frozenset[str]
    recent_removals: dict[str, int]


@dataclass(frozen=True, slots=True)
class Outcome:
    """One user's verdict in one org; ``action`` names what was done."""

    org: str
    user: str
    action: str  # in-sync|casdoor-only|removed|casdoor-missing|absent|held|error
    state: str | None = None
    error: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class _PassResult:
    full_orgs: tuple[str, ...]
    outcomes: tuple[Outcome, ...]
    halted: dict[str, str]
    clean: frozenset[str]  # full passes that read every user without an error
    single: bool


@dataclass
class _OrgStatus:
    casdoor_only: set[str] = field(default_factory=set)
    casdoor_missing: set[str] = field(default_factory=set)
    last_sync_at: str | None = None
    last_full_sync_at: str | None = None
    last_error: dict[str, str] | None = None
    halted: dict[str, str] | None = None


def member_uri(user: str) -> str:
    """``hyprial/<name>``, ``user:<name>`` or ``<name>`` -> ``user:<name>``."""

    if user.startswith("user:"):
        return user
    return canonical_user_uri(user.split("/", 1)[1] if "/" in user else user)


def removal_limit(members: int) -> int:
    """The most removals one pass (or one window) may make: max(1, 20 %)."""

    return max(1, int(members * BREAKER_SHARE))


def breaker_trips(removals: int, members: int) -> bool:
    """``removals`` this pass would make, of ``members`` (the owner excluded)."""

    return removals > removal_limit(members)


def decide(state: str, *, in_orgfs: bool, owner: bool, confirmed: bool, halted: bool) -> str:
    """The action for one read; ``halted`` means "no-user never removes"."""

    if state == "member":
        return "in-sync" if in_orgfs else "casdoor-only"
    if owner:
        return "error"
    if not in_orgfs:
        return "absent"
    if state == "not-member":
        return "removed"
    if confirmed and not halted:
        return "removed"
    return "casdoor-missing"


class ConfirmedMembers:
    """Users this owner node has read as group members, per org (0600 JSON)."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, set[str]]:
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(org): {str(user) for user in users if isinstance(user, str)}
            for org, users in raw.items()
            if isinstance(users, list)
        }

    def save(self, confirmed: dict[str, set[str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        body = json.dumps({org: sorted(users) for org, users in sorted(confirmed.items())})
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, self._path)


class CasdoorMembershipSync:
    """Mailbox of per-user keys, coalesced; one effect lane does the I/O.

    ``service_factory`` builds an ``OrgNetworkService`` (its Casdoor client,
    owned orgs, OrgFS member reads and the OrgFS half of a removal).
    """

    def __init__(
        self,
        *,
        service_factory: Callable[[], Any],
        confirmed: ConfirmedMembers,
        log: _Logger | None = None,
        clock: Callable[[], float] = time.time,
        interval_seconds: float = BACKSTOP_INTERVAL_SECONDS,
        scheduler: GenerationScheduler | None = None,
    ) -> None:
        self._service_factory = service_factory
        self._confirmed_store = confirmed
        self._confirmed = confirmed.load()
        self._log = log or (lambda *_args, **_fields: None)
        self._clock = clock
        self._interval = interval_seconds
        self._guard = threading.Lock()
        self._pending: OrderedDict[_Key, None] = OrderedDict()
        self._active: _Key | None = None
        self._sequence = 0
        self._status: dict[str, _OrgStatus] = {}
        self._removals: dict[str, deque[float]] = {}  # single-user removals, for the window budget
        self._closed = False
        self._started = False
        self._runtime = ActorRuntime()
        self._scheduler = scheduler or GenerationScheduler()
        self._handle = self._runtime.start(
            ActorSpec(
                name="org-casdoor-membership",
                handler_factory=lambda: self._receive,
                mailbox_capacity=64,
            )
        )
        self._effects: EffectLane[_Work, _PassResult] = EffectLane(
            name="org-casdoor-reads",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=1,
        )

    # -- public surface --------------------------------------------------------

    def start(self) -> None:
        """Run the start-up full pass, then the backstop cadence."""

        with self._guard:
            if self._started or self._closed:
                return
            self._started = True
        self.reconcile()
        self._schedule_backstop()

    def user_changed(self, user: str, org: str | None = None) -> AdmissionResult:
        """One user may have changed (webhook, or a local command on ``org``)."""

        return self._tell(_Enqueue(("user", org, member_uri(user))))

    def reconcile(self, org: str | None = None) -> AdmissionResult:
        """A full pass over ``org`` (or every owned org)."""

        return self._tell(_Enqueue(("all", org, None)))

    def status(self, org: str) -> dict[str, Any]:
        with self._guard:
            entry = self._status.get(org, _OrgStatus())
            return {
                "casdoorOnly": sorted(entry.casdoor_only),
                "casdoorMissing": sorted(entry.casdoor_missing),
                "lastSyncAt": entry.last_sync_at,
                "lastFullSyncAt": entry.last_full_sync_at,
                "lastError": dict(entry.last_error) if entry.last_error else None,
                "syncHalted": dict(entry.halted) if entry.halted else None,
            }

    def idle(self) -> bool:
        state = self._runtime.snapshot(self._handle)
        with self._guard:
            return not self._pending and self._active is None and state.queued == state.in_flight == 0

    def stop(self, *, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
            self._pending.clear()
        self._scheduler.shutdown(max(0.0, deadline - time.monotonic()))
        while not self.idle():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))

    # -- actor ---------------------------------------------------------------

    def _tell(self, command: Any) -> AdmissionResult:
        with self._guard:
            if self._closed:
                return AdmissionResult.CLOSED
        return self._runtime.tell(self._handle, command)

    def _receive(self, command: Any) -> None:
        if isinstance(command, _Enqueue):
            self._enqueue(command.key)
        elif isinstance(command, _Backstop):
            self._enqueue(("all", None, None))
            self._schedule_backstop()
        elif isinstance(command, EffectCompleted):
            self._complete(command)
        else:
            raise TypeError("unsupported org membership command")
        self._pump()

    def _enqueue(self, key: _Key) -> None:
        with self._guard:
            if self._closed or key in self._pending:
                return  # coalesced: the queued read will see the latest state
            if len(self._pending) >= _PENDING_LIMIT:
                # One full pass covers every queued user.
                self._pending.clear()
                key = ("all", None, None)
            self._pending[key] = None

    def _pump(self) -> None:
        with self._guard:
            if self._closed or self._active is not None or not self._pending:
                return
            key, _ = self._pending.popitem(last=False)
            self._active = key
            self._sequence += 1
            operation = str(self._sequence)
            horizon = self._clock() - REMOVAL_WINDOW_SECONDS
            for stamps in self._removals.values():
                while stamps and stamps[0] < horizon:
                    stamps.popleft()
            work = _Work(
                key=key,
                confirmed={org: frozenset(users) for org, users in self._confirmed.items()},
                halted=frozenset(org for org, entry in self._status.items() if entry.halted),
                recent_removals={org: len(stamps) for org, stamps in self._removals.items()},
            )
        if self._effects.submit(EffectRequest(operation, 1, work)) is not AdmissionResult.ACCEPTED:
            with self._guard:
                self._active = None

    def _complete(self, event: EffectCompleted) -> None:
        self._effects.acknowledge(event.operation_id, event.generation)
        with self._guard:
            self._active = None
        now = datetime.fromtimestamp(self._clock(), UTC).isoformat()
        if event.error is not None or event.result is None:
            self._log("warn", "org", "org.sync.failed", reason="pass-crashed", detail=str(event.error))
            return
        result: _PassResult = event.result
        changed = False
        with self._guard:
            for outcome in result.outcomes:
                changed |= self._fold(outcome, now)
                if result.single and outcome.action == "removed":
                    self._removals.setdefault(outcome.org, deque()).append(self._clock())
            for org, reason in result.halted.items():
                self._status.setdefault(org, _OrgStatus()).halted = {"reason": reason, "at": now}
            for org in result.full_orgs:
                entry = self._status.setdefault(org, _OrgStatus())
                entry.last_full_sync_at = now
                # Only a pass that read everyone without an error may lift a
                # halt: an outage reads no "no-user" and so can never trip.
                if org not in result.halted and org in result.clean and entry.halted is not None:
                    entry.halted = None
                    self._log("info", "org", "org.sync.resumed", org=org)
            confirmed = {org: set(users) for org, users in self._confirmed.items()}
        for org, reason in result.halted.items():
            self._log("warn", "org", "org.sync.halted", org=org, reason=reason)
        if changed:
            self._confirmed_store.save(confirmed)

    def _fold(self, outcome: Outcome, now: str) -> bool:
        """Apply one outcome to the status; True when ``confirmed`` changed."""

        entry = self._status.setdefault(outcome.org, _OrgStatus())
        confirmed = self._confirmed.setdefault(outcome.org, set())
        before = len(confirmed)
        if outcome.action == "error":
            code, message = outcome.error or ("CASDOOR_SYNC_FAILED", "")
            entry.last_error = {"code": code, "message": message, "user": outcome.user, "at": now}
            self._log("warn", "org", "org.sync.failed", org=outcome.org, user=outcome.user, code=code)
            return False
        entry.last_sync_at = now
        if outcome.action == "held":
            return False
        if outcome.action in ('pending-confirmation', 'pending-authority', 'tailnet-pending'):
            entry.last_error = {'code': outcome.action, 'message': 'Explicit tailnet removal is pending.',
                                'user': outcome.user, 'at': now}
            return False
        if outcome.state == "member":
            confirmed.add(outcome.user)
        if outcome.action == "casdoor-only":
            if outcome.user not in entry.casdoor_only:
                self._log("info", "org", "org.sync.casdoor-only", org=outcome.org, user=outcome.user)
            entry.casdoor_only.add(outcome.user)
        else:
            entry.casdoor_only.discard(outcome.user)
        if outcome.action == "casdoor-missing":
            if outcome.user not in entry.casdoor_missing:
                self._log("warn", "org", "org.sync.casdoor-missing", org=outcome.org, user=outcome.user)
            entry.casdoor_missing.add(outcome.user)
        else:
            entry.casdoor_missing.discard(outcome.user)
        if outcome.action in ("removed", "absent"):
            confirmed.discard(outcome.user)
        if outcome.action == "removed":
            self._log("info", "org", "org.sync.removed", org=outcome.org, user=outcome.user, state=outcome.state)
        return len(confirmed) != before

    def _schedule_backstop(self) -> None:
        with self._guard:
            if self._closed or not self._started:
                return
        self._scheduler.schedule(
            "backstop", 1, self._interval, lambda generation: self._tell(_Backstop(generation))
        )

    # -- effect (Casdoor reads and OrgFS removals; runs off the actor) --------

    def _execute(self, work: _Work) -> _PassResult:
        service = self._service_factory()
        kind, only_org, user = work.key
        owner = canonical_user_uri(service._owner)
        owned = [(org, space) for org, space in service._owned_org_spaces() if only_org in (None, org)]
        outcomes: list[Outcome] = []
        halted: dict[str, str] = {}
        full_orgs: list[str] = []
        clean: set[str] = set()
        for org, space_id in owned:
            members = service._orgfs_member_users(space_id)
            others = [member for member in members if member != owner]
            added_at = service._orgfs_member_added_at(space_id)
            if kind == "user":
                assert user is not None
                reads = {user: self._read(service, org, user)}
                plan = self._plan(org, reads, members, work, owner=owner, halted=org in work.halted)
                if any(action == "removed" for _user, _state, action in plan):
                    used = work.recent_removals.get(org, 0)
                    if used + 1 > removal_limit(len(others)):
                        halted[org] = (
                            f"single-user removals would reach {used + 1} within "
                            f"{int(REMOVAL_WINDOW_SECONDS // 60)} min, of {len(others)} members"
                        )
                        outcomes.extend(self._hold(org, plan))
                        continue
                outcomes.extend(self._apply(service, org, space_id, plan, added_at))
                continue
            full_orgs.append(org)
            with self._guard:
                reported = self._status.get(org, _OrgStatus())
                known = set(members) | reported.casdoor_only | reported.casdoor_missing
            reads = {each: self._read(service, org, each) for each in sorted(known)}
            if not any(isinstance(state, CasdoorGroupError) for state in reads.values()):
                clean.add(org)
            plan = self._plan(org, reads, members, work, owner=owner, halted=False)
            removals = sum(1 for _user, _state, action in plan if action == "removed")
            if breaker_trips(removals, len(others)):
                halted[org] = f"{removals} of {len(others)} members would be removed in one pass"
                clean.discard(org)
                outcomes.extend(self._hold(org, plan))
                continue
            outcomes.extend(self._apply(service, org, space_id, plan, added_at))
        return _PassResult(tuple(full_orgs), tuple(outcomes), halted, frozenset(clean), kind == "user")

    @staticmethod
    def _read(service: Any, org: str, user: str) -> str | CasdoorGroupError:
        try:
            return service._casdoor.member_state(casdoor_group(org), casdoor_user(user))
        except CasdoorGroupError as error:
            return error

    @staticmethod
    def _plan(
        org: str,
        reads: dict[str, str | CasdoorGroupError],
        members: list[str],
        work: _Work,
        *,
        owner: str,
        halted: bool,
    ) -> list[tuple[str, str | CasdoorGroupError, str]]:
        """``(user, state, action)`` per read, deciding without acting."""

        confirmed = work.confirmed.get(org, frozenset())
        plan: list[tuple[str, str | CasdoorGroupError, str]] = []
        for user, state in reads.items():
            if isinstance(state, CasdoorGroupError):
                plan.append((user, state, "error"))
                continue
            action = decide(
                state,
                in_orgfs=user in members,
                owner=user == owner,
                confirmed=user in confirmed,
                halted=halted,
            )
            plan.append((user, state, action))
        return plan

    @staticmethod
    def _hold(org: str, plan: list[tuple[str, str | CasdoorGroupError, str]]) -> list[Outcome]:
        return [
            Outcome(org, user, "held", state if isinstance(state, str) else None)
            for user, state, _action in plan
        ]

    @staticmethod
    def _apply(
        service: Any,
        org: str,
        space_id: str,
        plan: list[tuple[str, str | CasdoorGroupError, str]],
        added_at: dict[str, Any],
    ) -> list[Outcome]:
        outcomes: list[Outcome] = []
        for user, state, action in plan:
            if isinstance(state, CasdoorGroupError):
                outcomes.append(Outcome(org, user, "error", None, (state.code, str(state))))
                continue
            if action == "error":
                outcomes.append(
                    Outcome(org, user, "error", state, ("CASDOOR_OWNER_NOT_IN_GROUP", f"the owner is {state} in {casdoor_group(org)}; never removed"))
                )
                continue
            if action == "removed":
                # A re-invite between the read and now gives the row a new
                # added_at: the read is about the old membership, so skip.
                if service._orgfs_member_added_at(space_id).get(user) != added_at.get(user):
                    outcomes.append(Outcome(org, user, "held", state))
                    continue
                try:
                    removal = service.remove_from_casdoor(org, user)
                    if removal['phase'] != 'complete':
                        outcomes.append(Outcome(org, user, removal['phase'], state))
                        continue
                except Exception as error:  # an OrgFS failure is this user's error, not the pass's
                    outcomes.append(Outcome(org, user, "error", state, ("ORGFS_REMOVE_FAILED", type(error).__name__)))
                    continue
            outcomes.append(Outcome(org, user, action, state))
        return outcomes
