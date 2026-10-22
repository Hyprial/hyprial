"""The peer-access actor: directory changes drive allow, dial and leave (S2b).

Design-casdoor-group-sync §5.  Every node keeps its Tailcat side in line with
the org directory by events, not by a per-tick scan:

- OrgFS watches on each org directory space (device rows, the member table
  in meta, leave requests) post one coalesced rebuild to this actor;
- the rebuild reads the directory once (``build_access_snapshot``), replaces
  the sidecar allow set when it changed (``allow`` is a full replace and
  disconnects every key that left it), runs the ``after_allow`` hook (the
  zenoh slot pool re-binds there), then asks for one redial;
- the forwarding reconcile and the dial-direction rule read the last
  snapshot, so an idle org costs no directory read per tick;
- a relaunched sidecar gets the current allow set from ``desired_allow``,
  and once installed asks for a forced rebuild (``sidecar-launch``) that
  re-sends the set even if unchanged: a rebuild racing the launch, or a
  failed replay, cannot leave a stale set behind;
- a rebuild that could not read every org recomputes the readable orgs,
  carries forward only the unreadable orgs' previous contributions (and
  their watches), and tries again after ``INCOMPLETE_REBUILD_DELAY_S``.  A
  contribution is carried for at most ``CARRY_LIMIT_S`` from the first
  failed read; after that the org is dropped (its keys leave the allow set,
  fail-closed) until it reads again.  Nothing is carried across a restart.

A full rebuild runs at start and on every event; a rebuild is O(devices) and
there is no per-row incremental path to drift from the directory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import threading
import time
from typing import Any

from hyprial.daemon.impl.org.network.access.snapshot import AccessSnapshot, OrgAccess, build_access_snapshot
from hyprial.identity import DIRECTORY_DIR, LEAVES_DIR
from hyprial.kernel import (
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
    EffectCompleted,
    EffectLane,
    EffectRequest,
    GenerationScheduler,
)

INCOMPLETE_REBUILD_DELAY_S = 10.0
CARRY_LIMIT_S = 60.0

_DEVICE_ROWS = f"{DIRECTORY_DIR}/*/*.json"
_LEAVE_REQUESTS = f"{LEAVES_DIR}/*.json"
_DENY_ALL: tuple[tuple[str, ...], bool] = ((), False)
_Logger = Callable[..., None]


@dataclass(frozen=True, slots=True)
class _Rebuild:
    reason: str
    leaves: bool = False
    force: bool = False


@dataclass(frozen=True, slots=True)
class _Work:
    reasons: tuple[str, ...]
    leaves: bool
    applied: tuple[tuple[str, ...], bool] | None


@dataclass(frozen=True, slots=True)
class _Result:
    snapshot: AccessSnapshot | None
    applied: tuple[tuple[str, ...], bool] | None


class PeerAccessActor:
    """One per daemon.  ``supervisor()`` is the live forwarding supervisor or None."""

    def __init__(
        self,
        *,
        service_factory: Callable[[], Any],
        facade: Any,
        supervisor: Callable[[], Any | None],
        log: _Logger | None = None,
        after_allow: Callable[[str], object] | None = None,
        request_redial: Callable[[], object] | None = None,
        process_leaves: Callable[[], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service_factory = service_factory
        self._facade = facade
        self._supervisor = supervisor
        self._log = log or (lambda *_args, **_fields: None)
        self._after_allow = after_allow
        self._request_redial = request_redial
        self._process_leaves = process_leaves
        self._clock = clock
        self._unreadable_since: dict[str, float] = {}  # effect lane only (capacity 1)
        self._guard = threading.Lock()
        self._snapshot: AccessSnapshot | None = None
        self._applied: tuple[tuple[str, ...], bool] | None = None
        self._pending: list[str] = []
        self._pending_leaves = False
        self._pending_force = False
        self._active = False
        self._sequence = 0
        self._closed = False
        self._watches: dict[tuple[str, str], Any] = {}
        self.rebuilds = 0  # read by tests: directory reads happen only here
        self._scheduler = GenerationScheduler()
        self._retry_generation = 0
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(name="org-peer-access", handler_factory=lambda: self._receive, mailbox_capacity=64)
        )
        self._effects: EffectLane[_Work, _Result] = EffectLane(
            name="org-peer-access-rebuild",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=1,
        )

    # -- public surface --------------------------------------------------------

    def start(self) -> None:
        """First rebuild (it also registers the watches, and processes leaves)."""

        self.request("start", leaves=True)

    def request(self, reason: str, *, leaves: bool = False, force: bool = False) -> AdmissionResult:
        """A directory change, a joined/left org, or a new sidecar (``force``)."""

        with self._guard:
            if self._closed:
                return AdmissionResult.CLOSED
        return self._runtime.tell(self._handle, _Rebuild(reason, leaves, force))

    def snapshot(self) -> AccessSnapshot | None:
        with self._guard:
            return self._snapshot

    def desired_allow(self) -> tuple[tuple[str, ...], bool]:
        """What a (re)launched sidecar must admit: deny-all until a snapshot exists."""

        with self._guard:
            return self._snapshot.allow if self._snapshot is not None else _DENY_ALL

    def idle(self) -> bool:
        state = self._runtime.snapshot(self._handle)
        with self._guard:
            return not self._pending and not self._active and state.queued == state.in_flight == 0

    def stop(self, *, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
            self._pending.clear()
            watches = list(self._watches.values())
            self._watches.clear()
        self._scheduler.shutdown(max(0.0, deadline - time.monotonic()))
        for registration in watches:
            try:
                registration.close()
            except Exception:  # noqa: BLE001 - a watch that will not close cannot block shutdown
                pass
        while not self.idle():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))

    # -- actor ---------------------------------------------------------------

    def _receive(self, command: Any) -> None:
        if isinstance(command, _Rebuild):
            with self._guard:
                if not self._closed:
                    if command.reason not in self._pending:
                        self._pending.append(command.reason)
                    self._pending_leaves = self._pending_leaves or command.leaves
                    self._pending_force = self._pending_force or command.force
        elif isinstance(command, EffectCompleted):
            self._effects.acknowledge(command.operation_id, command.generation)
            result: _Result | None = command.result
            with self._guard:
                self._active = False
                if command.error is None and result is not None:
                    if result.snapshot is not None:
                        self._snapshot = result.snapshot
                    if result.applied is not None:
                        self._applied = result.applied
            if command.error is not None:
                self._log("warn", "org", "org.access.rebuild-failed", detail=str(command.error)[:300])
        else:
            raise TypeError("unsupported peer-access command")
        self._pump()

    def _pump(self) -> None:
        with self._guard:
            if self._closed or self._active or not self._pending:
                return
            work = _Work(
                tuple(self._pending),
                self._pending_leaves,
                None if self._pending_force else self._applied,
            )
            self._pending = []
            self._pending_leaves = False
            self._pending_force = False
            self._active = True
            self._sequence += 1
            operation = str(self._sequence)
        if self._effects.submit(EffectRequest(operation, 1, work)) is not AdmissionResult.ACCEPTED:
            with self._guard:
                self._active = False

    # -- effect (directory read, sidecar allow, redial; off the actor) --------

    def _execute(self, work: _Work) -> _Result:
        reason = ",".join(work.reasons)
        if work.leaves and self._process_leaves is not None:
            try:
                self._process_leaves()
            except Exception as error:  # noqa: BLE001 - a leave failure must not stop access
                self._log("warn", "org", "org.leave_requests.failed", detail=str(error)[:300])
        snapshot = build_access_snapshot(self._service_factory(), self._carry())
        self.rebuilds += 1
        now = self._clock()
        self._unreadable_since = {
            org: self._unreadable_since.get(org, now) for org in snapshot.unreadable
        }
        if snapshot.incomplete:
            self._log(
                "warn", "org", "org.access.rebuild-incomplete", reason=reason,
                carried=sorted(org for org in snapshot.unreadable if org in snapshot.orgs),
                dropped=sorted(org for org in snapshot.unreadable if org not in snapshot.orgs),
            )
            self._retry_later()
        with self._guard:
            self._snapshot = snapshot  # the redial below already reads it
        self._watch(snapshot, keep_unlisted=snapshot.incomplete)
        applied = None
        if snapshot.allow != work.applied:
            applied = self._apply_allow(snapshot, reason)
        if self._after_allow is not None:
            try:
                self._after_allow(reason)
            except Exception as error:  # noqa: BLE001 - a hook failure must not stop access
                self._log("warn", "org", "org.access.after-allow-failed", detail=str(error)[:300])
        if self._request_redial is not None:
            self._request_redial()
        return _Result(snapshot, applied)

    def _apply_allow(self, snapshot: AccessSnapshot, reason: str) -> tuple[tuple[str, ...], bool] | None:
        supervisor = self._supervisor()
        if supervisor is None:
            return None  # a sidecar that starts later replays desired_allow
        keys, allow_any = snapshot.allow
        try:
            supervisor.allow(list(keys), allow_any)
        except Exception as error:  # noqa: BLE001 - retried on the next event or relaunch
            self._log("warn", "org", "org.access.allow-failed", detail=str(error)[:300])
            return None
        self._log(
            "info", "org", "org.access.allowed",
            keys=len(keys), allowAny=allow_any, reason=reason,
        )
        return snapshot.allow

    def _carry(self) -> dict[str, OrgAccess]:
        """Previous contributions still inside the carry bound."""

        with self._guard:
            previous = self._snapshot
        if previous is None:
            return {}
        now = self._clock()
        return {
            org: part
            for org, part in previous.orgs.items()
            if now - self._unreadable_since.get(org, now) < CARRY_LIMIT_S
        }

    def _retry_later(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._retry_generation += 1
            generation = self._retry_generation
        self._scheduler.schedule(
            "incomplete", generation, INCOMPLETE_REBUILD_DELAY_S,
            lambda _generation: self.request("retry-incomplete"),
        )

    def _watch(self, snapshot: AccessSnapshot, *, keep_unlisted: bool = False) -> None:
        wanted: dict[tuple[str, str], tuple[str, str, str | None]] = {}
        for _org, space_id in snapshot.spaces:
            wanted[(space_id, "rows")] = (space_id, "rows", _DEVICE_ROWS)
            wanted[(space_id, "meta")] = (space_id, "meta", None)
            if self._process_leaves is not None:
                wanted[(space_id, "leaves")] = (space_id, "leaves", _LEAVE_REQUESTS)
        with self._guard:
            if self._closed:
                return
            gone = [] if keep_unlisted else [key for key in self._watches if key not in wanted]
            missing = [key for key in wanted if key not in self._watches]
        for key in gone:
            with self._guard:
                registration = self._watches.pop(key, None)
            if registration is not None:
                registration.close()
        for key in missing:
            space_id, kind, glob = wanted[key]
            if kind == "meta":
                registration = self._facade.watch_meta(space_id, lambda: self.request("membership"))
            elif kind == "leaves":
                registration = self._facade.watch(
                    space_id, glob, lambda _event: self.request("leave-request", leaves=True)
                )
            else:
                registration = self._facade.watch(space_id, glob, lambda _event: self.request("device-row"))
            with self._guard:
                self._watches[key] = registration
