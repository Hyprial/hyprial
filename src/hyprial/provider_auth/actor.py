"""Typed provider-auth admission with one episode writer and isolated helpers."""

from __future__ import annotations

import heapq
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from uuid import uuid4

from hyprial.actor_runtime import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.actor_runtime.effects import EffectRequest, EffectCompleted, EffectLane
from .classification import classify_provider_failure
from .coordinator import AuthNoticeIntent, MAX_ROUNDS, ProviderAuthCoordinator
from .helper import HelperOutcome, DeviceCodeAnnouncement


@dataclass(frozen=True, slots=True)
class TurnAuthFailed:
    failure: str
    harness: str
    provider: str | None
    model: str | None
    worker: str
    runtime_context: object = None


@dataclass(frozen=True, slots=True)
class RestoreAuth:
    pass


@dataclass(frozen=True, slots=True)
class RunAuthHelper:
    operation_id: str
    episode_key: str
    episode_id: str
    round_no: int
    provider: str


@dataclass(frozen=True, slots=True)
class AuthCodeObserved:
    helper: RunAuthHelper
    announcement: DeviceCodeAnnouncement


@dataclass(frozen=True, slots=True)
class AuthHelperFinished:
    helper: RunAuthHelper
    outcome: HelperOutcome


@dataclass(frozen=True, slots=True)
class RetryAuthHelper:
    episode_key: str
    episode_id: str


@dataclass(frozen=True, slots=True)
class NotifyOwner:
    intent: AuthNoticeIntent


@dataclass(frozen=True, slots=True)
class NoticeAttemptOutcome:
    intent: AuthNoticeIntent
    delivered: bool
    current: bool = True


@dataclass(frozen=True, slots=True)
class AuthNoticeCompleted:
    intent: AuthNoticeIntent
    delivered: bool


@dataclass(frozen=True, slots=True)
class _DecisionWork:
    command: object
    credit_token: str | None = None


class _StopView:
    def __init__(self, daemon_stop, local_stop):
        self._daemon = daemon_stop
        self._local = local_stop

    def is_set(self):
        return self._daemon.is_set() or self._local.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return self.is_set()
            remaining = (
                0.1
                if deadline is None
                else min(0.1, max(0.0, deadline - time.monotonic()))
            )
            self._local.wait(remaining)
        return True


class _EpisodeWriter(ProviderAuthCoordinator):
    def __init__(self, authority, **options):
        self._authority = authority
        super().__init__(**options)

    def _spawn_round(self, episode):
        if self._stop.is_set():
            episode.inflight = False
            return
        self._authority._claim_episode_credit(
            episode.key, episode.episode_id
        )
        successor = bool(
            getattr(self._authority._helper_decision_context, "active", False)
        )
        if not self._authority._start_helper(episode, successor=successor):
            episode.inflight = False
            self._log("provider.auth.helper.overloaded", provider=episode.provider)

    def _new_episode(self, *args, **kwargs):
        episode = super()._new_episode(*args, **kwargs)
        self._authority._claim_episode_credit(
            episode.key, episode.episode_id
        )
        return episode

    def _restore_child_available(self, episode_key, episode_id):
        return self._authority._restore_child_available(
            episode_key, episode_id
        )

    def _notify(self, intent: AuthNoticeIntent) -> bool:
        self._authority._accept_notice(intent)
        return False  # admission is pending, never a delivered receipt


class ProviderAuthAuthority:
    """All episode mutations are routed through one typed decision lane.

    The lane owns the legacy policy and profile/credential I/O; helper execution
    and owner-channel delivery cannot occupy that lane. No daemon turn callback
    waits for any of these effects. Helper results are fenced by episode + round.
    """

    def __init__(self, **options):
        notification_capacity = int(options.pop("notification_capacity", 32))
        helper_capacity = int(options.pop("helper_capacity", 16))
        observation_capacity = int(
            options.pop("observation_capacity", notification_capacity)
        )
        self._notification_retry_seconds = float(
            options.pop("notification_retry_seconds", 1.0)
        )
        if (
            notification_capacity < 1
            or helper_capacity < 1
            or observation_capacity < 1
            or self._notification_retry_seconds <= 0
        ):
            raise ValueError("provider auth capacities/retry must be positive")
        self._guard = threading.Lock()
        self._closed = False
        self._stop = threading.Event()
        self._runners = {}
        self._commands = {}
        self._rejected = 0
        self._completed = 0
        self._failed = 0
        self._decision_capacity = 128
        self._notification_capacity = notification_capacity
        self._helper_capacity = helper_capacity
        self._business_capacity = observation_capacity
        self._restore_child_capacity = min(
            helper_capacity, observation_capacity
        )
        self._episode_custody_capacity = (
            observation_capacity + self._restore_child_capacity - 1
        )
        # One episode can have every round's code attempt in native custody and
        # one current terminal/recovery successor. Native attempts whose terminal
        # successor already settled retain their own fixed lane credit.
        self._notice_custody_capacity = (
            notification_capacity
            + self._episode_custody_capacity * (MAX_ROUNDS + 1)
        )
        self._helper_start_custody_capacity = self._business_capacity
        self._business_credits: dict[str, tuple[str, object]] = {}
        self._observation_credits: dict[str, str] = {}
        self._episode_credits: dict[tuple[str, str], str] = {}
        self._notice_credits: dict[str, tuple[str, str]] = {}
        self._restore_token: str | None = None
        self._restore_cursor: str | None = None
        self._restore_pending = False
        self._observations: OrderedDict[str, object] = OrderedDict()
        self._observation_retries = 0
        self._helper_starts: OrderedDict[
            tuple[str, str], tuple[object, bool]
        ] = OrderedDict()
        self._helper_retries = 0
        self._restore_retries = 0
        self._notices: dict[str, AuthNoticeIntent] = {}
        self._external_decisions: dict[str, tuple[str, str]] = {}
        self._notice_attempts: dict[str, str] = {}
        self._notification_sent = 0
        self._notification_retries = 0
        self._retry_condition = threading.Condition()
        self._retry_heap: list[tuple[float, int, str, object]] = []
        self._retry_scheduled: set[tuple[str, object]] = set()
        self._retry_sequence = 0
        self._retry_shutdown = False
        self._notifier = options["notifier"]
        self._helper_decision_context = threading.local()
        self._decision_context = threading.local()
        options["stop"] = _StopView(options["stop"], self._stop)
        self._core = _EpisodeWriter(self, **options)
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="provider-auth",
                handler_factory=lambda: self._receive,
                mailbox_capacity=self._decision_capacity,
            )
        )
        self._decisions = EffectLane(
            name="provider-auth-state",
            execute=self._decide,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=self._decision_capacity,
        )
        self._helpers = EffectLane(
            name="provider-auth-helper",
            execute=self._run_helper,
            complete=lambda event: self._runtime.tell(self._handle, event),
            # The second half is a bounded handoff reserve: a timed-out round
            # may start its successor before the old completion is ACKed.
            capacity=helper_capacity * 2,
            workers=min(4, helper_capacity * 2),
        )
        self._notifications = EffectLane(
            name="provider-auth-notify",
            execute=self._notify,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=notification_capacity,
            workers=min(4, notification_capacity),
        )
        self._retry_thread = threading.Thread(
            target=self._retry_loop,
            name="provider-auth-notify-retry",
            daemon=True,
        )
        self._retry_thread.start()

    def _admit(self, command, *, internal=False):
        with self._guard:
            if self._closed and not internal:
                self._rejected += 1
                return AdmissionResult.CLOSED
            if internal:
                result = self._submit_decision_locked(command)
            else:
                key = self._observation_key(command)
                if (
                    isinstance(command, RestoreAuth)
                    and self._restore_token is not None
                ):
                    result = AdmissionResult.ACCEPTED
                elif key in self._observations:
                    self._observations[key] = command
                    self._observations.move_to_end(key)
                    result = AdmissionResult.ACCEPTED
                elif any(
                    active_key == key
                    for active_key, _token in self._external_decisions.values()
                ):
                    # The exact worker/route/failure class is already accepted.
                    result = AdmissionResult.ACCEPTED
                else:
                    token = self._acquire_business_credit_locked(key)
                    if token is None:
                        result = AdmissionResult.OVERLOADED
                    else:
                        if isinstance(command, RestoreAuth):
                            self._restore_token = token
                            self._restore_cursor = None
                            self._restore_pending = False
                        result = self._submit_decision_locked(
                            command, external_key=key, credit_token=token
                        )
                        if result is not AdmissionResult.ACCEPTED:
                            result = self._defer_observation_locked(
                                command, key=key, credit_token=token
                            )
            if result is not AdmissionResult.ACCEPTED:
                self._rejected += 1
            return result

    def _submit_decision_locked(
        self,
        command,
        *,
        external_key: str | None = None,
        credit_token: str | None = None,
    ) -> AdmissionResult:
        operation = "decision:" + uuid4().hex
        if external_key is not None:
            if credit_token is None:
                raise RuntimeError("external provider decision lacks custody credit")
            self._external_decisions[operation] = (external_key, credit_token)
            self._business_credits[credit_token] = ("decision", operation)
            self._observation_credits.pop(external_key, None)
        result = self._decisions.submit(
            EffectRequest(operation, 1, _DecisionWork(command, credit_token))
        )
        if result is not AdmissionResult.ACCEPTED:
            self._external_decisions.pop(operation, None)
            if external_key is not None and credit_token is not None:
                self._business_credits[credit_token] = (
                    "observation",
                    external_key,
                )
                self._observation_credits[external_key] = credit_token
        return result

    def _acquire_business_credit_locked(self, key: str) -> str | None:
        token = self._observation_credits.get(key)
        if token is not None:
            return token
        if len(self._business_credits) >= self._business_capacity:
            return None
        token = uuid4().hex
        self._business_credits[token] = ("observation", key)
        self._observation_credits[key] = token
        return token

    def _release_business_credit_locked(self, token: str) -> None:
        self._business_credits.pop(token, None)
        if self._restore_token == token:
            self._restore_token = None
            self._restore_cursor = None
            self._restore_pending = False
        episode_refs = frozenset(
            key
            for key, current in self._episode_credits.items()
            if current == token
        )
        for mapping in (self._observation_credits, self._episode_credits):
            for key, current in tuple(mapping.items()):
                if current == token:
                    mapping.pop(key, None)
        for key, episode_ref in tuple(self._notice_credits.items()):
            if episode_ref in episode_refs:
                self._notice_credits.pop(key, None)

    def _restore_business_credit_owner_locked(self, token: str) -> None:
        if token not in self._business_credits:
            return
        for key, current in self._episode_credits.items():
            if current == token:
                self._business_credits[token] = ("episode", key)
                return
        for key, current in self._notice_credits.items():
            if key not in self._notices:
                continue
            notice_token = self._episode_credits.get(current)
            if notice_token == token:
                self._business_credits[token] = ("notice", key)
                return
        for key, current in self._observation_credits.items():
            if current == token:
                self._business_credits[token] = ("observation", key)
                return
        if self._restore_token == token:
            self._business_credits[token] = (
                "restore",
                self._restore_cursor,
            )
            return
        self._release_business_credit_locked(token)

    def _claim_episode_credit(
        self, episode_key: str, episode_id: str
    ) -> str:
        episode_ref = (episode_key, episode_id)
        token = getattr(self._decision_context, "credit_token", None)
        with self._guard:
            existing = self._episode_credits.get(episode_ref)
            if existing is not None:
                if token is not None and token != existing:
                    self._release_business_credit_locked(token)
                return existing
            if token is None or token not in self._business_credits:
                raise RuntimeError("provider episode lacks transferable custody")
            self._episode_credits[episode_ref] = token
            self._business_credits[token] = ("episode", episode_ref)
            for key, current in tuple(self._observation_credits.items()):
                if current == token:
                    self._observation_credits.pop(key, None)
            return token

    def _restore_child_available(
        self, episode_key: str, episode_id: str | None
    ) -> bool:
        token = getattr(self._decision_context, "credit_token", None)
        with self._guard:
            if token is None or token != self._restore_token:
                raise RuntimeError("provider restore lacks group custody")
            if episode_id is not None:
                existing = self._episode_credits.get(
                    (episode_key, episode_id)
                )
                if existing == token:
                    return True
            owned = sum(
                current == token
                for current in self._episode_credits.values()
            )
            return owned < self._restore_child_capacity

    def _release_episode_credit_locked(
        self, episode_ref: tuple[str, str]
    ) -> tuple[str | None, bool]:
        token = self._episode_credits.pop(episode_ref, None)
        if token is None:
            return None, False
        for key, current in tuple(self._notice_credits.items()):
            if current == episode_ref:
                self._notice_credits.pop(key, None)
        retry_restore = token == self._restore_token and self._restore_pending
        self._restore_business_credit_owner_locked(token)
        return token, retry_restore

    @staticmethod
    def _observation_key(command) -> str:
        if isinstance(command, RestoreAuth):
            return "restore"
        context = command.runtime_context
        route = (
            f"{getattr(context, 'actor', '')}:"
            f"{getattr(context, 'entity_token', '')}"
            if context is not None
            else "host"
        )
        return "\x1f".join(
            (
                "turn",
                route,
                command.harness,
                command.provider or "",
                command.model or "",
                command.worker,
                classify_provider_failure(
                    command.failure, provider=command.provider
                ).value,
            )
        )

    def _defer_observation_locked(
        self,
        command,
        *,
        key: str | None = None,
        credit_token: str,
    ) -> AdmissionResult:
        key = key or self._observation_key(command)
        if key in self._observations:
            self._observations[key] = command
            self._observations.move_to_end(key)
            self._schedule_retry("decision", key)
            return AdmissionResult.ACCEPTED
        if self._observation_credits.get(key) != credit_token:
            raise RuntimeError("provider observation credit transfer disagrees")
        self._observations[key] = command
        self._schedule_retry("decision", key)
        return AdmissionResult.ACCEPTED

    def handle_turn_failure(
        self, failure, *, harness, provider, model, worker, runtime_context=None
    ):
        result = self._admit(
            TurnAuthFailed(failure, harness, provider, model, worker, runtime_context)
        )
        if result is not AdmissionResult.ACCEPTED:
            self._core._log("provider.auth.admission.rejected", admission=result.value)
        return result

    def resume_after_restore(self):
        return self._admit(RestoreAuth())

    def _accept_notice(self, intent: AuthNoticeIntent) -> None:
        key = intent.idempotency_key
        episode_ref = (intent.episode_key, intent.episode_id)
        self._retire_superseded_notices()
        with self._guard:
            if key in self._notices:
                return
            token = self._episode_credits.get(episode_ref) or getattr(
                self._decision_context, "credit_token", None
            )
            if token is None or token not in self._business_credits:
                raise RuntimeError("provider notice lacks transferable custody")
            self._episode_credits.setdefault(episode_ref, token)
            owned = sum(
                current == episode_ref
                for current in self._notice_credits.values()
            )
            if (
                len(self._notices) >= self._notice_custody_capacity
                or owned >= MAX_ROUNDS + 1
            ):
                raise RuntimeError(
                    "provider notice exceeded its reserved business custody"
                )
            self._notices[key] = intent
            self._notice_credits[key] = episode_ref
            self._business_credits[token] = ("notice", episode_ref)
        admitted = self._submit_notice(intent)
        if admitted is AdmissionResult.ACCEPTED:
            return
        # Logical custody was accepted even though every native attempt slot is
        # busy.  The one shared scheduler retries it; never discard the intent.
        self._schedule_retry("notice", key)

    def _retire_superseded_notices(self) -> None:
        retired: list[
            tuple[AuthNoticeIntent, tuple[str, str] | None]
        ] = []
        with self._guard:
            active = frozenset(self._notice_attempts.values())
            for key, intent in tuple(self._notices.items()):
                if key in active or self._core._notice_current(intent):
                    continue
                self._notices.pop(key, None)
                retired.append((intent, self._notice_credits.pop(key, None)))
        for intent, episode_ref in retired:
            self._cancel_retry("notice", intent.idempotency_key)
            self._complete_notice_on_owner(intent, delivered=False)
            if episode_ref is not None:
                with self._guard:
                    token = self._episode_credits.get(episode_ref)
                    if token is not None:
                        self._restore_business_credit_owner_locked(token)

    def _submit_notice(self, intent: AuthNoticeIntent) -> AdmissionResult:
        operation = "notice:" + uuid4().hex
        with self._guard:
            if intent.idempotency_key not in self._notices:
                return AdmissionResult.CLOSED
            self._notice_attempts[operation] = intent.idempotency_key
        admitted = self._notifications.submit(
            EffectRequest(operation, 1, NotifyOwner(intent))
        )
        if admitted is not AdmissionResult.ACCEPTED:
            with self._guard:
                self._notice_attempts.pop(operation, None)
        return admitted

    def _schedule_retry(self, kind: str, key: object) -> None:
        token = (kind, key)
        with self._retry_condition:
            if self._retry_shutdown or token in self._retry_scheduled:
                return
            self._retry_sequence += 1
            heapq.heappush(
                self._retry_heap,
                (
                    time.monotonic() + self._notification_retry_seconds,
                    self._retry_sequence,
                    kind,
                    key,
                ),
            )
            self._retry_scheduled.add(token)
            if kind == "notice":
                self._notification_retries += 1
            elif kind == "helper":
                self._helper_retries += 1
            elif kind == "restore":
                self._restore_retries += 1
            else:
                self._observation_retries += 1
            self._retry_condition.notify_all()

    def _cancel_retry(self, kind: str, key: object) -> None:
        token = (kind, key)
        with self._retry_condition:
            if token not in self._retry_scheduled:
                return
            self._retry_scheduled.discard(token)
            self._retry_heap = [
                item
                for item in self._retry_heap
                if (item[2], item[3]) != token
            ]
            heapq.heapify(self._retry_heap)

    def _retry_loop(self) -> None:
        while True:
            with self._retry_condition:
                while not self._retry_heap and not self._retry_shutdown:
                    self._retry_condition.wait()
                if self._retry_shutdown:
                    return
                due, _sequence, kind, key = self._retry_heap[0]
                remaining = due - time.monotonic()
                if remaining > 0:
                    self._retry_condition.wait(remaining)
                    continue
                heapq.heappop(self._retry_heap)
                self._retry_scheduled.discard((kind, key))
            if kind == "notice":
                with self._guard:
                    intent = self._notices.get(key)
                if intent is None:
                    continue
                admitted = self._submit_notice(intent)
            elif kind == "decision":
                with self._guard:
                    command = self._observations.get(key)
                    if command is None:
                        continue
                    credit_token = self._observation_credits.get(key)
                    if credit_token is None:
                        raise RuntimeError(
                            "deferred provider observation lost custody credit"
                        )
                    admitted = self._submit_decision_locked(
                        command,
                        external_key=key,
                        credit_token=credit_token,
                    )
                    if admitted is AdmissionResult.ACCEPTED:
                        self._observations.pop(key, None)
            elif kind == "helper":
                # Scheduler owns timing only. Episode state and successor
                # validation stay on the serial policy lane, including the
                # recheck immediately before helper admission.
                with self._guard:
                    if key not in self._helper_starts:
                        continue
                    episode_key, episode_id = key
                    admitted = self._submit_decision_locked(
                        RetryAuthHelper(episode_key, episode_id)
                    )
            else:
                with self._guard:
                    if (
                        key != self._restore_token
                        or not self._restore_pending
                    ):
                        continue
                    admitted = self._submit_decision_locked(
                        RestoreAuth(), credit_token=key
                    )
            if admitted is not AdmissionResult.ACCEPTED:
                self._schedule_retry(kind, key)

    def _queue_notice_completion(
        self, intent: AuthNoticeIntent, *, delivered: bool
    ) -> bool:
        operation = "notice-settled:" + intent.idempotency_key
        if operation in self._commands:
            return True
        admitted = self._decisions.submit(
            EffectRequest(
                operation,
                1,
                _DecisionWork(AuthNoticeCompleted(intent, delivered)),
            )
        )
        if admitted is AdmissionResult.ACCEPTED:
            self._commands[operation] = ("notice", intent.idempotency_key)
            return True
        return False

    def _receive(self, command):
        if isinstance(command, EffectCompleted):
            operation = command.operation_id
            if operation.startswith("helper:"):
                # EffectLane completions are at-least-once. A queued replay
                # after settlement must not recreate the finished decision ID:
                # its old ACK could otherwise retire the new lane reservation.
                with self._guard:
                    owned = self._runners.get(operation)
                if owned is None:
                    # Retire an orphaned receipt too. Each helper submission
                    # mints a fresh UUID; this ACK cannot target a later helper.
                    self._helpers.acknowledge(operation, command.generation)
                    return
                if (
                    command.error is None
                    and isinstance(command.result, tuple)
                    and len(command.result) == 2
                ):
                    helper, outcome = command.result
                else:
                    helper = owned[1]
                    outcome = HelperOutcome.FAILED
                if helper is None:
                    self._helpers.acknowledge(operation, command.generation)
                    with self._guard:
                        self._runners.pop(operation, None)
                    return
                decision_id = "finished:" + operation
                if decision_id not in self._commands:
                    payload = AuthHelperFinished(helper, outcome)
                    admitted = self._decisions.submit(
                        EffectRequest(decision_id, 1, _DecisionWork(payload))
                    )
                    if admitted is AdmissionResult.ACCEPTED:
                        self._commands[decision_id] = ("helper", operation)
                return  # helper retains result until its episode writer settles
            if operation.startswith("notice:"):
                with self._guard:
                    key = self._notice_attempts.get(operation)
                    intent = None if key is None else self._notices.get(key)
                if intent is None:
                    self._notifications.acknowledge(
                        operation, command.generation
                    )
                    return
                outcome = (
                    command.result
                    if isinstance(command.result, NoticeAttemptOutcome)
                    else NoticeAttemptOutcome(intent, False)
                )
                current = outcome.current and self._core._notice_current(intent)
                if outcome.delivered or not current:
                    if not self._queue_notice_completion(
                        intent, delivered=outcome.delivered
                    ):
                        return
                elif current:
                    # Transfer custody to the paced retry scheduler before
                    # releasing this exact native-attempt completion.
                    self._schedule_retry("notice", intent.idempotency_key)
                self._notifications.acknowledge(operation, command.generation)
                with self._guard:
                    self._notice_attempts.pop(operation, None)
            else:
                self._decisions.acknowledge(operation, command.generation)
                with self._guard:
                    external = self._external_decisions.pop(operation, None)
                    if external is not None:
                        _external_key, token = external
                        if self._business_credits.get(token) == (
                            "decision",
                            operation,
                        ):
                            self._release_business_credit_locked(token)
                completion_owner = self._commands.pop(operation, None)
                if completion_owner and completion_owner[0] == "helper":
                    helper_operation = completion_owner[1]
                    self._helpers.acknowledge(helper_operation, 1)
                    with self._guard:
                        self._runners.pop(helper_operation, None)
                elif completion_owner and completion_owner[0] == "notice":
                    key = completion_owner[1]
                    retry_restore = False
                    released_token = None
                    with self._guard:
                        pending_intent = self._notices.get(key)
                    retired_terminal = bool(
                        pending_intent is not None
                        and pending_intent.kind != "code"
                        and not self._core._notice_current(pending_intent)
                    )
                    with self._guard:
                        intent = self._notices.pop(key, None)
                        episode_ref = self._notice_credits.pop(key, None)
                        delivered = (
                            intent is not None
                            and command.error is None
                            and command.result is True
                        )
                        retired_terminal = (
                            retired_terminal and intent is pending_intent
                        )
                        if delivered:
                            self._notification_sent += 1
                        if (
                            (delivered or retired_terminal)
                            and intent is not None
                            and intent.kind != "code"
                            and episode_ref is not None
                        ):
                            released_token, retry_restore = (
                                self._release_episode_credit_locked(
                                    episode_ref
                                )
                            )
                        elif episode_ref is not None:
                            token = self._episode_credits.get(episode_ref)
                            if token is not None:
                                self._restore_business_credit_owner_locked(
                                    token
                                )
                    if retry_restore and released_token is not None:
                        self._schedule_retry("restore", released_token)
            with self._guard:
                self._completed += 1
                self._failed += command.error is not None
            return
        raise TypeError("unsupported provider auth command")

    def _decide(self, work):
        if not isinstance(work, _DecisionWork):
            raise TypeError("unsupported provider auth decision envelope")
        self._decision_context.credit_token = work.credit_token
        self._decision_context.active = True
        try:
            return self._apply_decision(work.command)
        finally:
            self._decision_context.active = False
            self._decision_context.credit_token = None

    def _complete_notice_on_owner(
        self, intent: AuthNoticeIntent, *, delivered: bool
    ) -> bool:
        if not getattr(self._decision_context, "active", False):
            raise RuntimeError("provider notice policy requires episode owner")
        return self._core._complete_notice(intent, delivered=delivered)

    def _apply_decision(self, command):
        core = self._core
        if isinstance(command, TurnAuthFailed):
            core.handle_turn_failure(
                command.failure,
                harness=command.harness,
                provider=command.provider,
                model=command.model,
                worker=command.worker,
                runtime_context=command.runtime_context,
            )
        elif isinstance(command, RestoreAuth):
            token = getattr(self._decision_context, "credit_token", None)
            if token is None or token != self._restore_token:
                raise RuntimeError("provider restore decision lost custody")
            complete, cursor = core.resume_after_restore(
                after_provider=self._restore_cursor
            )
            with self._guard:
                if token != self._restore_token:
                    raise RuntimeError("provider restore generation changed")
                self._restore_cursor = cursor
                self._restore_pending = not complete
                if complete:
                    self._restore_token = None
                    self._restore_cursor = None
                self._restore_business_credit_owner_locked(token)
        elif isinstance(command, RetryAuthHelper):
            key = (command.episode_key, command.episode_id)
            with self._guard:
                pending = self._helper_starts.get(key)
            if pending is None:
                return
            episode, successor = pending
            if core._episodes.get(command.episode_key) is not episode:
                with self._guard:
                    self._helper_starts.pop(key, None)
                return
            if self._start_helper(episode, successor=successor, allow_defer=False):
                with self._guard:
                    self._helper_starts.pop(key, None)
            else:
                self._schedule_retry("helper", key)
        elif isinstance(command, AuthNoticeCompleted):
            return self._complete_notice_on_owner(
                command.intent, delivered=command.delivered
            )
        else:
            helper = command.helper
            episode = core._episodes.get(helper.episode_key)
            if (
                episode is None
                or episode.episode_id != helper.episode_id
                or episode.round != helper.round_no
            ):
                return
            if isinstance(command, AuthCodeObserved):
                core._announce(episode, helper.round_no, command.announcement)
            elif isinstance(command, AuthHelperFinished):
                self._helper_decision_context.active = True
                try:
                    core._finish_round(episode, helper.round_no, command.outcome)
                finally:
                    self._helper_decision_context.active = False

    def _start_helper(
        self, episode, *, successor: bool = False, allow_defer: bool = True
    ) -> bool:
        operation = "helper:" + uuid4().hex
        request = RunAuthHelper(
            operation, episode.key, episode.episode_id, episode.round, episode.provider
        )
        episode_ref = (episode.key, episode.episode_id)
        with self._guard:
            if episode_ref not in self._episode_credits:
                raise RuntimeError("provider helper lacks transferable custody")
            limit = self._helper_capacity * (2 if successor else 1)
            if len(self._runners) >= limit:
                if not allow_defer:
                    return False
                key = episode_ref
                if (
                    key not in self._helper_starts
                    and len(self._helper_starts)
                    >= self._helper_start_custody_capacity
                ):
                    return False
                self._helper_starts[key] = (episode, successor)
                self._helper_starts.move_to_end(key)
                self._schedule_retry("helper", key)
                return True
            self._runners[operation] = (episode.runner, request)
        result = self._helpers.submit(EffectRequest(operation, 1, request))
        if result is not AdmissionResult.ACCEPTED:
            with self._guard:
                self._runners.pop(operation, None)
        return result is AdmissionResult.ACCEPTED

    def _run_helper(self, request):
        with self._guard:
            runner, _owned_request = self._runners[request.operation_id]
        self._core._log(
            "provider.auth.relogin.round",
            provider=request.provider,
            round=request.round_no,
            episodeId=request.episode_id,
        )

        def announce(code):
            command = AuthCodeObserved(request, code)
            while not self._stop.is_set():
                if self._admit(command, internal=True) is AdmissionResult.ACCEPTED:
                    return
                self._stop.wait(0.01)

        try:
            outcome = runner(request.provider, announce, stop=self._core._stop)
        except Exception as error:
            self._core._log(
                "provider.auth.relogin.failed",
                provider=request.provider,
                errorType=type(error).__name__,
                episodeId=request.episode_id,
            )
            outcome = HelperOutcome.FAILED
        return request, outcome

    def _notify(self, command):
        intent = command.intent
        if not self._core._notice_current(intent):
            return NoticeAttemptOutcome(intent, False, current=False)
        result = self._notifier(
            intent.text, idempotency_key=intent.idempotency_key
        )
        delivered = getattr(result, "delivered", None) is True
        self._core._log(
            "provider.auth.alert",
            delivered=delivered,
            idempotencyKey=intent.idempotency_key,
        )
        return NoticeAttemptOutcome(intent, delivered)

    def status(self):
        with self._guard:
            status = {
                "closed": self._closed,
                "rejected": self._rejected,
                "completed": self._completed,
                "failed": self._failed,
                "helpers": len(self._runners),
                "notificationsPending": len(self._notices),
                "notificationsSent": self._notification_sent,
                "notificationRetries": self._notification_retries,
                "observationsPending": len(self._observations),
                "observationRetries": self._observation_retries,
                "helperStartsPending": len(self._helper_starts),
                "helperRetries": self._helper_retries,
                "helperStartCustodyCapacity": (
                    self._helper_start_custody_capacity
                ),
                "episodeCustodyCapacity": self._episode_custody_capacity,
                "episodeCredits": len(self._episode_credits),
                "restoreChildCapacity": self._restore_child_capacity,
                "restorePending": self._restore_pending,
                "restoreRetries": self._restore_retries,
                "noticeCustodyCapacity": self._notice_custody_capacity,
                "observationCapacity": self._business_capacity,
                "businessCredits": len(self._business_credits),
                "businessCapacity": self._business_capacity,
                "externalDecisions": len(self._external_decisions),
                "notificationAttempts": len(self._notice_attempts),
            }
        decision_pending = self._decisions.snapshot().outstanding
        helper_pending = self._helpers.snapshot().outstanding
        status["decisionPending"] = decision_pending
        status["helperPending"] = helper_pending
        with self._retry_condition:
            status["retryScheduled"] = len(self._retry_scheduled)
        status["custody"] = (
            status["businessCredits"]
            + decision_pending
            + helper_pending
            + status["notificationAttempts"]
            + status["retryScheduled"]
        )
        status["custodyCapacity"] = (
            self._decision_capacity
            + (self._helper_capacity * 2)
            + self._notification_capacity
            + (self._business_capacity * 7)
        )
        return status

    def close(self, timeout=5.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        self._stop.set()
        with self._guard:
            restore_token = self._restore_token
            self._restore_pending = False
            pending_helper_starts = tuple(self._helper_starts.items())
            self._helper_starts.clear()
        if restore_token is not None:
            self._cancel_retry("restore", restore_token)
        for key, (episode, _successor) in pending_helper_starts:
            episode.inflight = False
            self._cancel_retry("helper", key)
        if not self._helpers.close(max(0.0, deadline - time.monotonic())):
            return False
        while True:
            snapshot = self._runtime.snapshot(self._handle)
            with self._guard:
                notices_pending = bool(self._notices)
                observations_pending = bool(self._observations)
                helper_starts_pending = bool(self._helper_starts)
            if (
                snapshot.queued == snapshot.in_flight == 0
                and not notices_pending
                and not observations_pending
                and not helper_starts_pending
                and self._decisions.snapshot().outstanding == 0
                and self._notifications.snapshot().outstanding == 0
            ):
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        with self._guard:
            self._business_credits.clear()
            self._observation_credits.clear()
            self._episode_credits.clear()
            self._notice_credits.clear()
            self._restore_token = None
            self._restore_cursor = None
            self._restore_pending = False
        with self._retry_condition:
            self._retry_shutdown = True
            self._retry_condition.notify_all()
        self._retry_thread.join(max(0.0, deadline - time.monotonic()))
        if self._retry_thread.is_alive():
            return False
        for lane in (self._notifications, self._decisions):
            if not lane.close(max(0.0, deadline - time.monotonic())):
                return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))
