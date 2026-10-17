"""DeliveryHookService: ``delivery.*`` facts and the delivery intercept.

This is the delivery pump's single hook surface.  It emits the three
``delivery.*`` observe events through the application-owned
:class:`DaemonHookService` (never blocking the pump) and drives the one
intercept this release supports: a ``hooks.json`` v2 entry with
``mode: "intercept"`` on ``delivery.prepared``.  The intercept keeps the
retired before-delivery semantics exactly: the handler's non-empty reply
becomes the delivery's opaque ``hook_text`` verbatim, and a timeout, error
or empty reply fails open -- the message is delivered unchanged.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
)
from hyprial.kernel import (
    HOOK_EVENT_SCHEMA_VERSION,
    HookInterceptPort,
    HookRegistration,
    InterceptRequest,
    delivery_event_payload,
)

from hyprial.daemon.impl.hooks.service import CONFIG_PENDING, DaemonHookService

#: How long the pump may hold one delivery while the recipient's hooks.json
#: finishes its FIRST load.  Matches the bounded config wait the before-
#: delivery coordinator used; after the first load the cache answers.
CONFIG_WAIT_SECONDS = 2.0
_CONTEXT_CAPACITY = 4_096
_UNSUPPORTED_LOG_CAPACITY = 256

#: The seam a shell-registered consumer fills to enrich intercept requests
#: (H2's recap consumer serves ``recentRecaps`` through it).  Called on the
#: invocation thread with (actor, the entry's options); its mapping becomes
#: the request's ``context``.  A missing seam, or one that raises, means
#: an empty context — never a blocked or dropped delivery.
InterceptContextProvider = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


def harness_supports_delivery_intercept(harness: str) -> bool:
    """Whether the declared headless mechanism can inject prompt context."""

    # Deferred to avoid the daemon/harness import cycle during module loading.
    from hyprial.daemon.impl.harnesses.capabilities import support
    from hyprial.kernel import Capability

    declared = support(
        harness,
        headless=True,
        capability=Capability.HEADLESS_EXEC,
    )
    return declared.mechanism != "python_worker"


@dataclass(slots=True)
class _DeliveryContext:
    recipient: str
    sender: str
    conversation: str
    delivery: "HarnessDelivery"
    config_deadline: float
    prepared_emitted: bool = False
    dispatched_at_ms: int | None = None


class DeliveryHookService:
    """Prepare, mark, settle and forget deliveries on behalf of the bridge."""

    def __init__(
        self,
        *,
        hooks: DaemonHookService,
        port: HookInterceptPort,
        supports_injection: Callable[[str], bool] | None = None,
        intercept_context: InterceptContextProvider | None = None,
        logger: Callable[..., None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        config_wait_seconds: float = CONFIG_WAIT_SECONDS,
    ) -> None:
        if config_wait_seconds < 0:
            raise ValueError("config wait must not be negative")
        self._hooks = hooks
        self._port = port
        self._supports_injection = supports_injection
        self._intercept_context = intercept_context
        self._logger = logger
        self._monotonic = monotonic
        self._config_wait_seconds = config_wait_seconds
        # The reconcile tick prepares/marks while settlement workers settle
        # and forget; one lock keeps the context table consistent for both.
        self._lock = threading.Lock()
        self._contexts: dict[str, _DeliveryContext] = {}
        self._unsupported_logged: set[str] = set()

    def prepare_delivery(self, delivery: HarnessDelivery) -> HarnessDelivery | None:
        """Return a deliverable value, or ``None`` while its bounded hook runs."""

        if delivery.hook_request:
            # The mechanism's own exchange must never be hooked recursively.
            return delivery
        context = self._context(delivery)
        registration = self._hooks.intercept_registration(delivery.recipient)
        if registration is CONFIG_PENDING:
            if self._monotonic() < context.config_deadline:
                return None
            self._log(
                "warn",
                "hooks.intercept_config_wait_expired",
                deliveryId=delivery.delivery_id,
                agent=delivery.recipient,
            )
            registration = None
        self._emit_prepared(delivery, context)
        if not isinstance(registration, HookRegistration):
            return delivery
        if not self._injection_supported(delivery.recipient):
            return delivery
        assert registration.handler is not None  # kernel config invariant
        decision = self._port.prepare(
            InterceptRequest(
                key=delivery.delivery_id,
                owner=delivery.recipient,
                handler=registration.handler,
                timeout_ms=registration.timeout_ms,
                document=self._intercept_document_factory(delivery, registration),
            )
        )
        if not decision.ready:
            return None
        if decision.text is not None:
            return replace(delivery, hook_text=decision.text)
        return delivery

    def mark_dispatched(self, delivery_id: str) -> None:
        self._port.mark_dispatched(delivery_id)
        with self._lock:
            context = self._contexts.get(delivery_id)
        if context is None:
            return
        context.dispatched_at_ms = self._hooks.clock_ms()
        # The durable turn ledger (PR #1463 finding 3): "was there a turn"
        # must never again depend on inference from adjacent traffic.  The
        # line is unconditional -- independent of any hooks.json -- and
        # carries identifiers and timestamps only.
        self._log(
            "info",
            "turn.started",
            deliveryId=delivery_id,
            actor=context.recipient,
            sender=context.sender,
            conversation=context.conversation,
            startedAtMs=context.dispatched_at_ms,
        )
        self._hooks.observe_started(
            context.delivery, started_at_ms=context.dispatched_at_ms
        )
        self._hooks.observe_delivery(
            event="delivery.dispatched",
            actor=context.recipient,
            payload=delivery_event_payload(
                delivery_id=delivery_id,
                sender=context.sender,
                conversation=context.conversation,
            ),
        )

    def settled(self, result: HarnessResult) -> None:
        """Emit ``delivery.settled`` for one terminal turn, then release it."""

        with self._lock:
            context = self._contexts.get(result.delivery_id)
        completed = result.status is HarnessResultStatus.COMPLETED
        if context is not None:
            ended_at_ms = self._hooks.clock_ms()
            started_at_ms = (
                context.dispatched_at_ms
                if context.dispatched_at_ms is not None
                else ended_at_ms
            )
            # Durable ledger twin of ``turn.started``: identifiers and
            # timing only, written even when no hook lane subscribes.
            self._log(
                "info",
                "turn.completed" if completed else "turn.failed",
                deliveryId=result.delivery_id,
                actor=context.recipient,
                sender=context.sender,
                conversation=context.conversation,
                startedAtMs=started_at_ms,
                endedAtMs=ended_at_ms,
                status=result.status.value,
                **(
                    {"failureCode": result.failure_code}
                    if result.failure_code is not None
                    else {}
                ),
            )
            try:
                self._hooks.observe_turn(
                    context.delivery,
                    result,
                    started_at_ms=started_at_ms,
                    ended_at_ms=ended_at_ms,
                )
            except Exception:  # noqa: BLE001 - the bus never owns settlement
                pass
        self._hooks.observe_delivery(
            event="delivery.settled",
            actor=result.recipient,
            payload=delivery_event_payload(
                delivery_id=result.delivery_id,
                sender=context.sender if context is not None else "",
                conversation=context.conversation if context is not None else "",
                outcome=result.status.value,
                failure_code=(
                    result.failure_code
                    if result.status is HarnessResultStatus.FAILED
                    else None
                ),
            ),
        )
        self.forget_delivery(result.delivery_id)

    def forget_delivery(self, delivery_id: str) -> None:
        """Release hook state once a turn or one-shot notice settles."""

        self._port.forget(delivery_id)
        with self._lock:
            self._contexts.pop(delivery_id, None)

    def forget_missing_deliveries(
        self, recipient: str, present_delivery_ids: set[str]
    ) -> None:
        """Release holds whose inbox rows were acknowledged or expired."""

        present = frozenset(present_delivery_ids)
        self._port.forget_missing(recipient, present)
        with self._lock:
            missing = tuple(
                delivery_id
                for delivery_id, context in self._contexts.items()
                if context.recipient == recipient and delivery_id not in present
            )
            for delivery_id in missing:
                self._contexts.pop(delivery_id, None)

    def close(self, timeout: float = 5.0) -> bool:
        return self._port.close(timeout)

    def _context(self, delivery: HarnessDelivery) -> _DeliveryContext:
        with self._lock:
            context = self._contexts.get(delivery.delivery_id)
            if context is not None:
                return context
            while len(self._contexts) >= _CONTEXT_CAPACITY:
                self._contexts.pop(next(iter(self._contexts)))
            context = _DeliveryContext(
                recipient=delivery.recipient,
                sender=delivery.sender,
                conversation=delivery.conversation_id,
                delivery=delivery,
                config_deadline=self._monotonic() + self._config_wait_seconds,
            )
            self._contexts[delivery.delivery_id] = context
            return context

    def _emit_prepared(
        self, delivery: HarnessDelivery, context: _DeliveryContext
    ) -> None:
        if context.prepared_emitted:
            return
        context.prepared_emitted = True
        self._hooks.observe_delivery(
            event="delivery.prepared",
            actor=delivery.recipient,
            payload=delivery_event_payload(
                delivery_id=delivery.delivery_id,
                sender=delivery.sender,
                conversation=delivery.conversation_id,
                message=delivery.message,
            ),
        )

    def _intercept_document_factory(
        self, delivery: HarnessDelivery, registration: HookRegistration
    ) -> Callable[[], dict[str, object]]:
        # Resolved by the port on the invocation thread: gathering context
        # may touch disk and must never run on the delivery pump.
        def build() -> dict[str, object]:
            # The handler composes context for the actual delivery, so unlike
            # the bounded observe payload it receives the message verbatim --
            # the same trust the retired before-delivery exchange extended.
            return {
                "event": "delivery.prepared",
                "schemaVersion": HOOK_EVENT_SCHEMA_VERSION,
                "mode": "intercept",
                "emittedAtMs": self._hooks.clock_ms(),
                "seq": self._hooks.sequence(),
                "source": "daemon.delivery",
                "actor": delivery.recipient,
                "payload": {
                    "deliveryId": delivery.delivery_id,
                    "sender": delivery.sender,
                    "conversation": delivery.conversation_id,
                    "message": delivery.message,
                    "collectionMethod": "daemon",
                    "fidelity": "observed",
                },
                "options": dict(registration.options),
                "context": self._request_context(delivery, registration),
            }

        return build

    def _request_context(
        self, delivery: HarnessDelivery, registration: HookRegistration
    ) -> dict[str, object]:
        if self._intercept_context is None:
            return {}
        try:
            return dict(
                self._intercept_context(
                    delivery.recipient, dict(registration.options)
                )
            )
        except Exception as error:  # noqa: BLE001 - context is advisory
            self._log(
                "warn",
                "hooks.intercept_context_failed",
                deliveryId=delivery.delivery_id,
                agent=delivery.recipient,
                errorType=type(error).__name__,
                detail=str(error)[:300],
            )
            return {}

    def _injection_supported(self, actor: str) -> bool:
        if self._supports_injection is None:
            return True
        try:
            if self._supports_injection(actor):
                return True
            detail = "harness kind cannot inject prompt context"
        except Exception as error:  # noqa: BLE001 - intercept fails open
            detail = f"{type(error).__name__}: {error}"
        with self._lock:
            already_logged = actor in self._unsupported_logged
            if not already_logged:
                if len(self._unsupported_logged) >= _UNSUPPORTED_LOG_CAPACITY:
                    self._unsupported_logged.clear()
                self._unsupported_logged.add(actor)
        if not already_logged:
            self._log(
                "warn",
                "hooks.intercept_unsupported",
                agent=actor,
                detail=detail[:300],
            )
        return False

    def _log(self, level: str, event: str, **fields: object) -> None:
        if self._logger is not None:
            self._logger(level, "daemon", event, **fields)


DEFAULT_INTERCEPT_RECENT_RECAPS = 5
MAX_INTERCEPT_RECENT_RECAPS = 50


def consumer_intercept_context(
    consumers: Mapping[str, object] | None,
) -> InterceptContextProvider | None:
    """Adapt the ``recap`` bus consumer's serving into intercept context.

    Unified-hooks seam (H2 x H3): the shell registers ``recap`` consumers on
    the daemon bus; a consumer that serves ``recent_recaps(actor, count)``
    enriches each intercept request with ``context.recentRecaps``.  No such
    consumer (the plain registration) returns ``None`` — the empty-context
    fail-open default.  A raising reader also fails open inside
    ``DeliveryHookService._request_context``; the duck dispatch keeps the
    daemon free of any biz import.
    """

    reader = getattr((consumers or {}).get("recap"), "recent_recaps", None)
    if not callable(reader):
        return None

    def provide(actor: str, options: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = options.get("recentRecaps", DEFAULT_INTERCEPT_RECENT_RECAPS)
        count = (
            raw
            if (
                isinstance(raw, int)
                and not isinstance(raw, bool)
                and 1 <= raw <= MAX_INTERCEPT_RECENT_RECAPS
            )
            else DEFAULT_INTERCEPT_RECENT_RECAPS
        )
        return {"recentRecaps": reader(actor, count)}

    return provide


__all__ = [
    "CONFIG_WAIT_SECONDS",
    "DEFAULT_INTERCEPT_RECENT_RECAPS",
    "DeliveryHookService",
    "InterceptContextProvider",
    "MAX_INTERCEPT_RECENT_RECAPS",
    "consumer_intercept_context",
    "harness_supports_delivery_intercept",
]
