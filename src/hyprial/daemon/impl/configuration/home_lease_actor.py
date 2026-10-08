"""Actor-owned home lease decisions, preserving physical PID/file fencing."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from uuid import uuid4

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectLane, EffectRequest, EffectCompleted
from hyprial.kernel import GenerationScheduler
from hyprial.daemon.impl.configuration.home_guard  import ActiveDaemonHeartbeat


@dataclass(frozen=True, slots=True)
class ClaimHome:
    pass


@dataclass(frozen=True, slots=True)
class RenewHome:
    generation: str
    queued_at: float | None = None
    scheduled_for: float | None = None


@dataclass(frozen=True, slots=True)
class ReleaseHome:
    pass


@dataclass(frozen=True, slots=True)
class LeaseCommand:
    operation_id: str
    payload: ClaimHome | RenewHome | ReleaseHome


@dataclass(frozen=True, slots=True)
class LeaseResult:
    claimed: bool
    duplicate_candidate: bytes | None = None
    heartbeat_at: float | None = None


@dataclass(frozen=True, slots=True)
class LeaseLost:
    generation: str


@dataclass(frozen=True, slots=True)
class LeaseExpired:
    generation: str
    version: int


@dataclass(frozen=True, slots=True)
class DuplicateObserved:
    payload: bytes


@dataclass(frozen=True, slots=True)
class DuplicateCheckFailed:
    detail: str


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


class HomeLeaseAuthority:
    """Physical ownership remains fenced; TTL-only self-exit is opt-in.

    The policy is fixed at construction. Production uses the default False,
    preserving renewal after a pause while the same physical owner still holds
    the record. Record/PID mismatch and file failures always lose ownership.
    """

    def __init__(self, home, *, self_fence_on_expiry: bool = False, **options):
        if not isinstance(self_fence_on_expiry, bool):
            raise TypeError("self_fence_on_expiry must be a bool")
        self._self_fence_on_expiry = self_fence_on_expiry
        self._lost_callback = options.pop("ownership_lost", None)
        self._duplicate_callback = options.pop("duplicate_detected", None)
        self._failed_callback = options.pop("duplicate_check_failed", None)
        self._guard = threading.RLock()
        self._closing = False
        self._claimed = False
        self._renewing = False
        self._lease_version = 0
        self._heartbeat_at = None
        self._fence_reason = None
        self._renew_timing = None
        self._renew_timing_operation = None
        self._pending = {}
        self._errors = {}
        self._claim_reply = None
        self._release_reply = None
        self._core = ActiveDaemonHeartbeat(
            home,
            ownership_lost=lambda: self._runtime.tell(
                self._handle, LeaseLost(self.generation)
            ),
            duplicate_detected=lambda detail: self._runtime.tell(
                self._handle, DuplicateObserved(json.dumps(detail).encode())
            ),
            duplicate_check_failed=lambda detail: self._runtime.tell(
                self._handle, DuplicateCheckFailed(detail)
            ),
            **options,
        )
        self._runtime = ActorRuntime()
        self._scheduler = GenerationScheduler()
        self._handle = self._runtime.start(
            ActorSpec(
                name="home-lease",
                handler_factory=lambda: self._receive,
                mailbox_capacity=8,
            )
        )
        self.home = self._core.home
        self.path = self._core.path
        self._effects = EffectLane(
            name="home-lease-file",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=3,
        )
        self._duplicates = EffectLane(
            name="home-lease-duplicate",
            execute=lambda raw: self._core._duplicate_check_entry(json.loads(raw)),
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=1,
        )

    @property
    def generation(self):
        return self._core.generation

    def _request(self, payload):
        reply = _Reply()
        operation_id = uuid4().hex
        with self._guard:
            self._pending[operation_id] = (payload, reply)
            if isinstance(payload, RenewHome):
                self._renew_timing_operation = operation_id
            admission = self._runtime.tell(
                self._handle, LeaseCommand(operation_id, payload)
            )
            if admission is not AdmissionResult.ACCEPTED:
                self._pending.pop(operation_id)
                raise RuntimeError(f"home lease admission {admission.value}")
        return reply

    def claim(self):
        with self._guard:
            if self._closing:
                raise RuntimeError("home lease authority is closing")
            reply = self._claim_reply
            if reply is None or (reply.ready.is_set() and reply.error is not None):
                reply = self._request(ClaimHome())
                self._claim_reply = reply
        if not reply.ready.wait(max(5.0, self._core.claim_wait_timeout + 5.0)):
            raise TimeoutError("home lease claim remains accepted")
        if reply.error is not None:
            raise reply.error

    def _receive(self, event):
        if isinstance(event, LeaseCommand):
            if isinstance(event.payload, RenewHome):
                self._observe_renewal(event.operation_id, "effectSubmittedAt")
            admission = self._effects.submit(
                EffectRequest(event.operation_id, 1, event)
            )
            if admission is not AdmissionResult.ACCEPTED:
                with self._guard:
                    payload, reply = self._pending.pop(event.operation_id)
                    reply.error = RuntimeError(f"home lease effects {admission.value}")
                    reply.ready.set()
            return
        if isinstance(event, RenewHome):
            with self._guard:
                if (
                    self._closing
                    or not self._claimed
                    or self._renewing
                    or event.generation != self.generation
                ):
                    return
                self._renewing = True
                self._renew_timing = {
                    "scheduledFor": event.scheduled_for,
                    "queuedAt": event.queued_at,
                    "actorReceivedAt": time.monotonic(),
                    "effectSubmittedAt": None,
                    "ioStartedAt": None,
                    "ioCompletedAt": None,
                    "completionReceivedAt": None,
                    "fencedAt": None,
                }
            self._request(event)
            return
        if isinstance(event, LeaseLost):
            self._fence("ownership-record-lost")
            return
        if isinstance(event, LeaseExpired):
            if (
                self._self_fence_on_expiry
                and event.generation == self.generation
                and event.version == self._lease_version
            ):
                self._fence("heartbeat-deadline")
            return
        if isinstance(event, DuplicateObserved):
            if self._duplicate_callback is not None and not self._closing:
                self._duplicate_callback(json.loads(event.payload))
            return
        if isinstance(event, DuplicateCheckFailed):
            if self._failed_callback is not None and not self._closing:
                self._failed_callback(event.detail)
            return
        if not isinstance(event, EffectCompleted):
            raise TypeError("unsupported home lease message")
        if event.operation_id.startswith("duplicate:"):
            self._duplicates.acknowledge(event.operation_id, event.generation)
            return
        with self._guard:
            pending = self._pending.pop(event.operation_id, None)
            error = self._errors.pop(event.operation_id, None)
            if pending is not None:
                payload, reply = pending
                if event.result is not None and (
                    not self._closing or isinstance(payload, ReleaseHome)
                ):
                    self._claimed = event.result.claimed
                    self._heartbeat_at = event.result.heartbeat_at
                if isinstance(payload, RenewHome):
                    self._observe_renewal(event.operation_id, "completionReceivedAt")
                    self._renewing = False
                reply.error = error or (
                    RuntimeError(event.error) if event.error else None
                )
        self._effects.acknowledge(event.operation_id, event.generation)
        if pending is None:
            return
        if (
            isinstance(payload, RenewHome)
            and event.result is not None
            and not event.result.claimed
        ):
            self._fence("renewal-refused")
        if (
            event.result is not None
            and event.result.duplicate_candidate is not None
            and not self._closing
        ):
            self._duplicates.submit(
                EffectRequest(
                    "duplicate:" + uuid4().hex, 1, event.result.duplicate_candidate
                )
            )
        if not isinstance(payload, ReleaseHome):
            self._schedule()
        if isinstance(payload, ClaimHome) and self._closing and reply.error is None:
            reply.error = TimeoutError("home lease expired before claim settled")
        reply.ready.set()

    def _execute(self, command):
        try:
            if isinstance(command.payload, ClaimHome):
                candidate = self._core._claim_record()
                return LeaseResult(
                    True,
                    json.dumps(candidate).encode() if candidate is not None else None,
                    self._core._last_committed_heartbeat,
                )
            if isinstance(command.payload, RenewHome):
                self._observe_renewal(command.operation_id, "ioStartedAt")
                try:
                    renewed = self._core._renew_record()
                    return LeaseResult(
                        renewed,
                        heartbeat_at=getattr(self._core, "_last_committed_heartbeat", None),
                    )
                finally:
                    self._observe_renewal(command.operation_id, "ioCompletedAt")
            self._core._release_record()
            return LeaseResult(False)
        except BaseException as error:
            with self._guard:
                self._errors[command.operation_id] = error
            raise

    def _schedule(self):
        with self._guard:
            if self._closing or not self._claimed:
                return
            self._lease_version += 1
            version = self._lease_version
            age = max(0.0, self._core._monotonic_clock() - self._heartbeat_at)
            remaining = 2 * self._core.keepalive_duration - age
            if self._self_fence_on_expiry and remaining <= 0:
                self._fence("heartbeat-already-expired")
                return
            renewal_delay = max(0.0, self._core.keepalive_duration - age)
            scheduled_for = time.monotonic() + renewal_delay
            self._scheduler.schedule(
                "renew",
                1,
                renewal_delay,
                lambda generation: self._runtime.tell(
                    self._handle,
                    RenewHome(self.generation, time.monotonic(), scheduled_for),
                ),
            )
            # Opt-in only: the default keeps dev's resume-and-renew behavior.
            # Physical generation/PID checks in the file effect stay active
            # regardless of this TTL policy.
            if self._self_fence_on_expiry:
                self._scheduler.schedule(
                    "expiry",
                    version,
                    remaining,
                    lambda current: self._runtime.tell(
                        self._handle, LeaseExpired(self.generation, current)
                    ),
                )

    def _fence(self, reason="requested"):
        with self._guard:
            if self._closing:
                return
            self._claimed = False
            self._closing = True
            self._fence_reason = reason
            if self._renew_timing is not None:
                self._renew_timing["fencedAt"] = time.monotonic()
        self._core._stop.set()
        self._scheduler.cancel("renew")
        self._scheduler.cancel("expiry")
        if self._lost_callback is not None:
            self._lost_callback()

    def _observe_renewal(self, operation_id, phase):
        # One bounded diagnostic record for the latest admitted renewal. The
        # file worker may update its own timestamps; none drive lease policy.
        with self._guard:
            if (
                self._renew_timing is not None
                and operation_id == self._renew_timing_operation
            ):
                self._renew_timing[phase] = time.monotonic()

    def status(self):
        with self._guard:
            return {
                "generation": self.generation,
                "claimed": self._claimed,
                "closing": self._closing,
                "version": self._lease_version,
                "pending": len(self._pending),
                "renewing": self._renewing,
                "fenceReason": self._fence_reason,
                "heartbeatAgeSeconds": (
                    self._core._monotonic_clock() - self._heartbeat_at
                    if self._heartbeat_at is not None else None
                ),
                "keepaliveSeconds": self._core.keepalive_duration,
                "renewalTiming": (
                    dict(self._renew_timing) if self._renew_timing is not None else None
                ),
            }

    def close(self, timeout=5.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closing = True
            reply = self._release_reply
        self._core._stop.set()
        self._scheduler.shutdown(max(0.0, deadline - time.monotonic()))
        with self._guard:
            reply = self._release_reply
            if reply is None:
                reply = self._request(ReleaseHome())
                self._release_reply = reply
        if not reply.ready.wait(max(0.0, deadline - time.monotonic())):
            return False
        if reply.error is not None:
            raise reply.error
        if not self._duplicates.close(
            max(0.0, deadline - time.monotonic())
        ) or not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))
