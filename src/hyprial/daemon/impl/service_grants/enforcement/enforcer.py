# Decision notes (service grant S4-py, 2026-11-26, coordinator ruling on the
# design-mismatch evidence):
# - One object owns the admission sets of this device's user exposures. It
#   derives them, compares with what it last pushed, pushes what changed, and
#   remembers only that. The sidecar holds the gate; nothing is persisted.
# - The roster is read once per derivation, with a bound, because
#   membership-status without a tailnet id returns every tailnet's rows and
#   each org picks its own effective tailnet out of that one observation.
# - A derivation never raises into the maintenance path. A roster that cannot
#   be read, or answers nothing, derives an empty key set: the port tightens
#   until a later tick can read it. That is reported per grant, without keys.
# - Over the sidecar's key limit nothing is pushed for that port. The last
#   set stays in force rather than being replaced by a wider or empty one,
#   because a half pushed set would admit the wrong devices.
# - Unexpose revokes this device's grants for the port through S3's
#   revoke_all_for_port. A failure there is logged once and swallowed: the
#   caller is already past the point where the port should stay exposed.
# - A derivation and an unexpose are ordered by a generation, not by a lock
#   over the whole derivation (S4-py fix, 2026-11-27, critic finding 2). The
#   lock covers only the exposure snapshot: which ports are exposed, and the
#   generation they were listed under. The supervisor lookup, the roster
#   read, the directory reads and the sidecar pushes run outside it. Under
#   one lock a derivation spent its roster budget and one push per port with
#   every unexpose queued behind it.
# - The generation is compared per port. An unexpose records, against its own
#   port, the generation it advanced to; a derivation's push for that port is
#   dropped when its list is older than that record, and only that push. The
#   first shape dropped the whole derivation, which cost every other port the
#   push it had just read a grant for; its second shape compared "at or
#   after", which refused the port for ever once it was exposed again. Both
#   are held by a test that fails under each.
# - The stale mark is kept by a count of triggers, apart from the generation.
#   A derivation clears it only if no trigger arrived after the derivation
#   read its grants, so a grant written during a roster read is not lost; and
#   an unexpose, which advances the generation but triggers nothing, does not
#   keep the mark owed for ever.
# - One wait is kept on purpose. A derivation that is already pushing the
#   very port an unexpose wants is let to finish, and the unexpose follows
#   its push on the wire. Without it a push begun before the unexpose could
#   land after it and reopen the port with no record left to close it. That
#   wait is bounded by one sidecar push, and at the bound the unexpose fails
#   and changes nothing, so the desired state, the sidecar and the grants
#   still agree: the port is exposed under the set it already had.
# - DEFAULT_UNEXPOSE_WAIT_SECONDS is one push with a margin. A push is the
#   supervisor's endpoint lookup (at most 2 s) and then the sidecar's answer,
#   which the controller gives up on after its response timeout (10 s), ahead
#   of the authority's own wait (12 s): 12 s, and 3 s of margin for the
#   claim that precedes them. It is stated once, here.
# - That bound covers the enforcer's wait alone. The removal itself is a
#   second wait that belongs to the forwarding layer: its authority runs one
#   sidecar command at a time, so an unexpose is queued behind whatever is
#   ahead of it there, a roster read (bounded by its budget, 5 s) or another
#   port's push (by the response timeout, 10 s) among them. The enforcer
#   cannot bound that one and does not try; it ends in the forwarding
#   layer's timeouts and it does not fail. A push that never answers costs
#   an unexpose its failure only when it is a push of the port being
#   unexposed; the unexpose of any other port is only delayed by it.
# - What was last pushed belongs to one sidecar incarnation. A relaunched
#   sidecar replays every exposure with nobody admitted, so when the live
#   backend is another object than the one the sets were pushed to, that
#   memory is dropped and an equal set is pushed again instead of skipped.
# - The inputs (directory store, facade, home, owner, device id) are asked for
#   at each derivation, never captured at construction, so a relogin or a
#   replaced OrgFS runtime is seen by the next tick. That is the same rule the
#   service grant IPC handler follows when it builds its service per call.
# - A caller that changed the inputs marks them stale and returns at once;
#   the derivation runs on the cadence that owns it. The mark is cleared only
#   by a derivation that ran to its push, so a derivation that could not run
#   (no sidecar key yet, a failing read) leaves the change owed to the next
#   tick instead of waiting out the whole interval.
# - The key limit is the forwarding policy's: the sidecar's parser and this
#   object must agree on one number, and there is one place that states it.
# - A derivation that finds no user exposure stops there: no roster read, no
#   grant read, no event. Every sidecar start offers a derivation and most
#   devices expose nothing, so without this every device would put a
#   membership-status read on its wire for a set it has nowhere to push.
# - A directory read that fails is one event per org and read, not one per
#   derivation (S4-py fix, 2026-11-27, coordinator ruling). The port tightens
#   to nobody either way; what was missing was the line that said so. It
#   carries the org and the read and nothing of the failure: no grant, whose
#   documents are what could not be read, no message and no path. The same
#   failure is logged once for as long as it lasts, because a derivation runs
#   at least every interval and a line per derivation is how the first line
#   gets lost. It is remembered against the org and the read, and a
#   derivation that fails neither of them for that pair forgets it, so the
#   next failure of it is logged again. What is remembered is what the last
#   derivation that reached its pushes saw: one that found no exposure reads
#   nothing, and one that failed elsewhere reports that instead.
# - An org whose tailnet binding cannot be resolved while its grants were
#   read is the one directory failure that knows the grants, so it is also
#   reported per grant, each as key_unavailable under tailnet-unresolved.
# - Integration fix (2026-11-27): an org that inherits its tailnet (its
#   effective tailnet id is not its own space id) has its own directory read
#   too: its device rows whose owners are its members are what its grants may
#   admit out of the parent's roster. That directory read failing is the
#   read DEVICES, reported once like the others, and tightens that org.
#   key_unavailable follows the same rule: remembered by (org, grantId,
#   reason) as the last derivation that reached its pushes saw it, logged
#   only when it is not remembered, so it is said again after it went away
#   or when its reason changed.
"""Pushing each exposure's derived admission set to the sidecar."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hyprial.daemon.impl.forwarding.policy import MAX_ADMISSION_KEYS, ForwardingSidecarError
from hyprial.daemon.impl.forwarding.store import ExposureStore
from hyprial.daemon.impl.org.network.binding import resolve_org_directory_space
from hyprial.daemon.impl.org.network.create.sub_org import effective_tailnet_id
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.orgfs.api import OrgFs
from hyprial.daemon.impl.service_grants import (
    ServiceGrantService,
    StoredServiceGrant,
    normalize_bare_owner,
)

from .derive import derive_admission
from .models import AdmissionSet, EnforcementInputs, NOBODY, UnresolvedGrant
from .reader import orgs, own_grants

MAX_KEYS = MAX_ADMISSION_KEYS
COMPONENT = "service"
KEY_UNAVAILABLE = "service.grant.key_unavailable"
LIMIT_EVENT = "service.grant.admission_limit"
UNEXPOSE_FAILED = "service.grant.unexpose_revoke_failed"
DERIVATION_FAILED = "service.grant.admission_unavailable"
ROSTER_UNREADABLE = "service.grant.roster_unreadable"
DIRECTORY_UNAVAILABLE = "service.grant.directory_unavailable"
PUSH_REJECTED = "network.exposure.admission_rejected"
TAILNET = "tailnet"
DEVICES = "devices"
DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_MEMBERSHIP_TIMEOUT = 5.0
DEFAULT_UNEXPOSE_WAIT_SECONDS = 15.0


def _error_code(error: BaseException) -> str:
    """The error's wire code when it has one, else its type name."""

    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(error).__name__


class _Snapshot:
    """What one derivation listed under the lock: the ports, and when.

    ``generation`` is the enforcer's generation at the moment the exposures
    were read. An unexpose advances it, which is how a derivation that listed
    a port before the unexpose learns that its list is no longer the truth
    for that port. ``changes`` is filled in before the roster read with the
    number of triggers so far, which is what decides whether the derivation
    may clear the stale mark.
    """

    def __init__(
        self,
        generation: int,
        backend: Any,
        server_public: str,
        exposures: Sequence[Mapping[str, object]],
    ) -> None:
        self.generation = generation
        self.backend = backend
        self.server_public = server_public
        self.changes = 0
        self._exposures = {
            exposure["port"]: {
                key: exposure[key] for key in ("port", "target", "proxyProtocol")
            }
            for exposure in exposures
            if isinstance(exposure.get("port"), int) and not isinstance(exposure.get("port"), bool)
        }
        self.ports = tuple(self._exposures)

    def exposure(self, port: int) -> Mapping[str, object]:
        return self._exposures[port]


class ServiceAdmissionEnforcer:
    """Derives and pushes the admission set of every user exposure."""

    def __init__(
        self,
        *,
        supervisor: Any,
        exposure_store: ExposureStore,
        grants_service: Callable[[], ServiceGrantService],
        inputs: Callable[[], EnforcementInputs] | None = None,
        store: OrgFsDirectoryStore | None = None,
        fs: OrgFs | None = None,
        home: Any = None,
        owner: str | None = None,
        device_id: str | None = None,
        roster_reader: Callable[[], Mapping[str, Any] | None] | None = None,
        revoke_all_for_port: Callable[[int], dict[str, Any]] | None = None,
        grants_reader: Callable[[], Mapping[str, Sequence[StoredServiceGrant]]] | None = None,
        now_ms: Callable[[], int],
        clock: Callable[[], float] = time.monotonic,
        logger: Callable[..., None] | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        membership_timeout: float = DEFAULT_MEMBERSHIP_TIMEOUT,
        unexpose_wait_seconds: float = DEFAULT_UNEXPOSE_WAIT_SECONDS,
    ) -> None:
        if inputs is None:
            fixed = EnforcementInputs(
                store=store, fs=fs, home=home, owner=owner or "", device_id=device_id or ""
            )

            def inputs() -> EnforcementInputs:
                return fixed

        self._supervisor = supervisor
        self._exposure_store = exposure_store
        self._inputs = inputs
        self._grants_service = grants_service
        self._roster_reader = roster_reader
        self._revoke_all_for_port = revoke_all_for_port
        self._grants_reader = grants_reader
        self._now_ms = now_ms
        self._clock = clock
        self._logger = logger
        self._interval = interval_seconds
        self._membership_timeout = membership_timeout
        self._unexpose_wait = unexpose_wait_seconds
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._pushed: dict[int, AdmissionSet] = {}
        self._backend: Any = object()
        self._last_derivation = float("-inf")
        self._dirty = True
        self._generation = 0
        self._pushing: dict[int, int] = {}
        self._removing: set[int] = set()
        self._retired: dict[int, int] = {}
        self._changes = 0
        self._unreadable: set[tuple[str, str]] = set()
        self._unavailable: set[tuple[str, str, str]] = set()

    def invalidate(self) -> None:
        """Mark the sets stale after a change that the caller knows about.

        Never derives and never raises, so it is safe on an IPC thread, on
        the sidecar's relaunch path and inside a lock the derivation takes:
        it takes only this object's own lock, for one assignment.
        """

        with self._lock:
            self._dirty = True
            self._changes += 1

    def tick(self) -> None:
        """One cadence tick: derive now when stale, else at most once per interval."""

        with self._lock:
            if not (self._dirty or self._clock() - self._last_derivation >= self._interval):
                return
        self._derive_and_push(self._begin())

    def _supervisor_view(self) -> tuple[Any, str | None]:
        """The live backend and its server key: one supervisor round trip, outside the lock.

        Read before the lock is taken, because the lookup is a round trip to
        the supervisor's own thread and a trigger marks the sets stale under
        the same lock.
        """

        try:
            backend = self._supervisor.endpoints()
        except Exception as error:  # noqa: BLE001
            self._log("warn", DERIVATION_FAILED, errorType=type(error).__name__)
            return None, None
        return backend, self._server_public(backend)

    def last_set(self, port: int) -> str | None:
        """The mode last pushed for ``port``: "allow", "keys", or nothing yet."""

        with self._lock:
            pushed = self._pushed.get(port)
        return None if pushed is None else pushed.admission

    def apply_now(self) -> None:
        """Derive and push now; never raises."""

        self._derive_and_push(self._begin())

    def reconcile(self) -> None:
        """The maintenance tick's entry: ``apply_now`` at most once per interval."""

        with self._lock:
            if self._clock() - self._last_derivation < self._interval:
                return
        self._derive_and_push(self._begin())

    def _begin(self) -> _Snapshot | None:
        """Take the exposure snapshot; ``None`` when there is nothing to derive for.

        The lock is held for the snapshot alone: which ports are exposed, and
        the generation they were listed under. A port whose unexpose starts
        after this point is either still listed here under a generation that
        is no longer current, or absent from a later snapshot. The supervisor
        lookup is made before the lock and the exposure file is read inside
        it, so that the list and the generation describe the same moment.
        """

        backend, server_public = self._supervisor_view()
        if server_public is None:
            return None
        with self._lock:
            self._last_derivation = self._clock()
            if backend is not self._backend:
                self._backend = backend
                self._pushed.clear()
            exposures = [
                exposure
                for exposure in self._exposure_store.list()
                if exposure.get("port") not in self._removing
            ]
            if not exposures:
                self._dirty = False
                return None
            return _Snapshot(self._generation, backend, server_public, exposures)

    def claim_port(self, port: int, generation: int) -> bool:
        """Reserve ``port`` for one push of the derivation listed under ``generation``.

        ``False`` when the port has left since that list was taken: it is
        being unexposed now, or its unexpose began after the list and has
        already finished. Either way the push would reopen a port that was
        closed, with no record left to close it again. Only this port's
        history counts; the unexpose of another port does not stale a push
        here. The comparison is strict: a list taken at the generation an
        unexpose advanced to is later than that unexpose, and is what pushes
        the port when it is exposed again. The reservation is what an
        unexpose of the same port waits for.
        """

        with self._lock:
            if port in self._removing or self._retired.get(port, -1) > generation:
                return False
            self._retired.pop(port, None)
            self._pushing[port] = self._pushing.get(port, 0) + 1
            return True

    def release_port(self, port: int) -> None:
        """End one reservation of ``port``; an unexpose waiting for it proceeds."""

        with self._settled:
            left = self._pushing.get(port, 0) - 1
            if left > 0:
                self._pushing[port] = left
            else:
                self._pushing.pop(port, None)
                self._settled.notify_all()

    def remember(self, port: int, admission: AdmissionSet, generation: int) -> None:
        """Record what a derivation listed under ``generation`` pushed for ``port``.

        A derivation that was made stale after the push, by an unexpose of
        this port, does not overwrite what is remembered: the port is gone
        and there is nothing to remember for it.
        """

        with self._lock:
            if port not in self._removing and self._retired.get(port, -1) <= generation:
                self._pushed[port] = admission

    def _triggers(self) -> int:
        """How many triggers have marked the sets stale so far."""

        with self._lock:
            return self._changes

    def _finish(self, seen: int) -> None:
        """A derivation read its grants after trigger ``seen``: the sets are no longer stale.

        The count is taken before the roster read and the grants are read
        after it, so a trigger that arrives in between has been seen by this
        derivation and may be cleared. One that arrives after the grants were
        read is a later count and keeps the mark, owed to the next tick.
        """

        with self._lock:
            if self._changes == seen:
                self._dirty = False

    def _remember(self, port: int, admission: AdmissionSet) -> bool:
        with self._lock:
            return self._pushed.get(port) == admission

    def begin_removal(self, port: int) -> bool:
        """Register ``port`` as being unexposed and wait for its push to end.

        Returns ``True`` when the port is registered and no derivation holds
        it, ``False`` at the bound, and raises ``ValueError`` when another
        unexpose of the same port is already under way.

        The registration is what makes a later derivation leave the port out,
        and the generation it records is what makes an earlier one drop its
        push, also after the registration is gone. That record lives until a
        derivation later than it claims the port again: at most one integer
        for every port this device has ever unexposed, and it stays behind
        when this method answers ``False``, where it keeps the port derivable
        as the exposure it still is.

        The wait is for one thing only: a push of this very port that a
        derivation has already claimed. It is bounded by
        ``DEFAULT_UNEXPOSE_WAIT_SECONDS``, which is one push with a margin.
        At the bound the registration is withdrawn, nothing else has changed,
        and the caller fails the unexpose: the exposure is still desired,
        still on the sidecar under its current set, and its grants are still
        in force, which is the only end state that never leaves a port open
        with no record to close it. The caller may retry.

        This is the first of the unexpose's two waits, and only this one is
        the enforcer's. The second is the removal itself: the forwarding
        authority runs one sidecar command at a time, so the unexpose command
        is queued behind whatever is ahead of it there. That wait is bounded
        by the forwarding layer's own timeouts, command by command, not here,
        and it does not fail. Must be paired with :meth:`forget_port` or
        :meth:`cancel_removal` once this answers ``True``.
        """

        with self._settled:
            if port in self._removing:
                raise ValueError(f"port {port} is already being unexposed")
            self._generation += 1
            self._retired[port] = self._generation
            self._removing.add(port)
            deadline = self._clock() + self._unexpose_wait
            while self._pushing.get(port):
                left = deadline - self._clock()
                if left <= 0 or not self._settled.wait(left):
                    if self._pushing.get(port):
                        self._removing.discard(port)
                        return False
            return True

    def cancel_removal(self, port: int) -> None:
        """An unexpose that failed after its registration: the port is not going anywhere.

        The port is exposed again for the next derivation and the sets are
        marked stale, so whatever the unexpose was going to change is
        re-derived rather than left half applied.
        """

        with self._lock:
            self._removing.discard(port)
            self._dirty = True

    def forget_port(self, port: int) -> dict[str, Any]:
        """Revoke this device's grants for ``port`` and forget what was pushed.

        To be called after :meth:`begin_removal` answered ``True``, the port
        is off the sidecar and out of the exposure store. Returns
        ``{"port", "revoked", "failed"}`` in the shape of S3's
        revoke_all_for_port. A failure is one ``failed`` entry and one log
        event; it does not raise.
        """

        with self._lock:
            self._removing.discard(port)
            self._pushed.pop(port, None)
        try:
            result = self._revoke(port)
        except Exception as error:  # noqa: BLE001
            code = _error_code(error)
            self._log("warn", UNEXPOSE_FAILED, port=port, org="*", code=code)
            return {"port": port, "revoked": [], "failed": [{"org": "*", "code": code}]}
        for item in result.get("failed", []):
            self._log(
                "warn",
                UNEXPOSE_FAILED,
                port=port,
                org=item.get("org"),
                code=item.get("code"),
            )
        return result

    def _revoke(self, port: int) -> dict[str, Any]:
        if self._revoke_all_for_port is not None:
            return self._revoke_all_for_port(port)
        return self._grants_service().revoke_all_for_port(port)

    def _derive_and_push(self, snapshot: _Snapshot | None) -> None:
        if snapshot is None:
            return
        snapshot.changes = self._triggers()
        unreadable: set[tuple[str, str]] = set()
        try:
            roster = self._read_roster()
            inputs = self._inputs()
            grants = self._read_grants(inputs, unreadable)
            inherited: set[str] = set()
            tailnet_ids = self._tailnet_ids(inputs, grants, unreadable, inherited)
            org_devices = self._org_devices(inputs, inherited, unreadable)
            projection = derive_admission(
                grants,
                {org: roster for org in grants},
                tailnet_ids,
                owner=inputs.owner,
                device_id=inputs.device_id,
                server_public=snapshot.server_public,
                now_ms=self._now_ms(),
                tailnets_unresolved=tuple(org for org, step in unreadable if step == TAILNET),
                org_devices=org_devices,
            )
        except Exception as error:  # noqa: BLE001
            self._log("warn", DERIVATION_FAILED, errorType=type(error).__name__)
            return
        self._report_directory(unreadable)
        self._push(projection, snapshot)
        self._finish(snapshot.changes)

    def _report_directory(self, unreadable: set[tuple[str, str]]) -> None:
        """Log each directory read that has newly failed, and remember what failed now.

        A pair of org and read that is already remembered is not logged
        again; one that did not fail this time is forgotten, so that its next
        failure is logged.
        """

        with self._lock:
            fresh = sorted(unreadable - self._unreadable)
            self._unreadable = unreadable
        for org, step in fresh:
            self._log("warn", DIRECTORY_UNAVAILABLE, org=org, step=step)

    def _push(self, projection: Mapping[int, Any], snapshot: _Snapshot) -> None:
        """Push each port whose set changed, unless its unexpose has begun since the list."""

        with self._lock:
            reported = set(self._unavailable)
        seen: set[tuple[str, str, str]] = set()
        for port in snapshot.ports:
            result = projection.get(port)
            if result is None:
                self._push_one(port, snapshot, NOBODY)
                continue
            self._report(port, result.unresolved, reported, seen)
            if result.admission.admission == "keys" and len(result.admission.keys) > MAX_KEYS:
                self._log(
                    "warn", LIMIT_EVENT, port=port, count=len(result.admission.keys), limit=MAX_KEYS
                )
                continue
            self._push_one(port, snapshot, result.admission)
        with self._lock:
            self._unavailable = seen

    def _push_one(self, port: int, snapshot: _Snapshot, admission: AdmissionSet) -> bool:
        """Push one port's set unless it is unchanged or the port is being unexposed."""

        if self._remember(port, admission):
            return True
        if not self.claim_port(port, snapshot.generation):
            return False
        try:
            self._supervisor.expose(
                snapshot.exposure(port),
                admission=admission.admission,
                keys=list(admission.keys),
            )
        except ForwardingSidecarError as error:
            self._log("error", PUSH_REJECTED, port=port, code=error.code)
            return False
        finally:
            self.release_port(port)
        self.remember(port, admission, snapshot.generation)
        return True

    def _pushed_matches(self, port: int, admission: AdmissionSet) -> bool:
        with self._lock:
            return self._pushed.get(port) == admission

    def _report(
        self,
        port: int,
        unresolved: Sequence[UnresolvedGrant],
        reported: set[tuple[str, str, str]],
        seen: set[tuple[str, str, str]],
    ) -> None:
        """Log each unresolved grant neither the last derivation nor this one has reported."""

        for item in unresolved:
            key = (item.org, item.grant_id, item.reason)
            if key in seen:
                continue
            seen.add(key)
            if key in reported:
                continue
            self._log(
                "warn",
                KEY_UNAVAILABLE,
                org=item.org,
                port=port,
                grantId=item.grant_id,
                recipient=dict(item.recipient),
                reason=item.reason,
            )

    @staticmethod
    def _server_public(backend: Any) -> str | None:
        controller = getattr(backend, "controller", None)
        server_public = getattr(controller, "server_public", None)
        return server_public if isinstance(server_public, str) and server_public else None

    def _read_roster(self) -> Mapping[str, Any] | None:
        """One bounded membership-status read; ``None`` when it cannot be read."""

        try:
            if self._roster_reader is not None:
                return self._roster_reader()
            return self._supervisor.membership_request(
                "membership-status", {}, timeout=self._membership_timeout
            )
        except Exception as error:  # noqa: BLE001
            self._log("warn", ROSTER_UNREADABLE, code=_error_code(error))
            return None

    def _read_grants(
        self, inputs: EnforcementInputs, unreadable: set[tuple[str, str]]
    ) -> dict[str, tuple[StoredServiceGrant, ...]]:
        if self._grants_reader is not None:
            return dict(self._grants_reader())

        def could_not_read(org: str, step: str) -> None:
            unreadable.add((org, step))

        return own_grants(
            inputs.store,
            orgs(inputs.store, unreadable=could_not_read),
            owner=inputs.owner,
            unreadable=could_not_read,
        )

    def _tailnet_ids(
        self,
        inputs: EnforcementInputs,
        grants: Mapping[str, Sequence[StoredServiceGrant]],
        unreadable: set[tuple[str, str]],
        inherited: set[str],
    ) -> dict[str, str]:
        """Each org's effective tailnet id: a sub-org inherits its parent's tailnet.

        An org whose id is not its own space id is added to ``inherited``.

        An org whose id cannot be resolved is left out and named to
        ``unreadable`` as the tailnet read, which derives it with no roster:
        its grants contribute nothing until it can be resolved, and each of
        them is reported under the reason that says so.
        """

        ids: dict[str, str] = {}
        for org in grants:
            try:
                space, _reason = resolve_org_directory_space(inputs.fs, org)
                if space is None:
                    unreadable.add((org, TAILNET))
                    continue
                ids[org] = effective_tailnet_id(
                    inputs.fs, inputs.home, org, space_id=space.space_id
                )
            except Exception:  # noqa: BLE001
                unreadable.add((org, TAILNET))
                continue
            if ids[org] != space.space_id:
                inherited.add(org)
        return ids

    @staticmethod
    def _org_devices(
        inputs: EnforcementInputs, orgs: set[str], unreadable: set[tuple[str, str]]
    ) -> dict[str, frozenset[tuple[str, str]] | None]:
        """Each org's own devices whose owners are its members; ``None`` when unreadable."""

        result: dict[str, frozenset[tuple[str, str]] | None] = {}
        for org in sorted(orgs):
            try:
                membership: dict[str, bool] = {}
                devices: set[tuple[str, str]] = set()
                for device in inputs.store.list_devices(org):
                    owner = normalize_bare_owner(device.owner)
                    if owner not in membership:
                        membership[owner] = inputs.store.is_member(org, owner)
                    if membership[owner]:
                        devices.add((owner, device.device_id))
            except Exception:  # noqa: BLE001
                unreadable.add((org, DEVICES))
                result[org] = None
                continue
            result[org] = frozenset(devices)
        return result

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger(level, COMPONENT, event, **fields)
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MEMBERSHIP_TIMEOUT",
    "DEFAULT_UNEXPOSE_WAIT_SECONDS",
    "DEVICES",
    "DIRECTORY_UNAVAILABLE",
    "KEY_UNAVAILABLE",
    "LIMIT_EVENT",
    "MAX_KEYS",
    "UNEXPOSE_FAILED",
    "ServiceAdmissionEnforcer",
]
