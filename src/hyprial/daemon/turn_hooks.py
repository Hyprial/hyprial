"""Neutral per-agent delivery hooks and append-only turn records."""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from hyprial.inbox.api import DeliveryLifecycle, InboxMessage, InboxPort
from hyprial.uri import parse_agent_uri

from .api import HarnessDelivery, HarnessResult, HarnessResultStatus


HOOK_CONFIG_NAME = "turn-hooks.json"
HOOK_REQUEST_MARKER = "turnHookRequest"
DEFAULT_TIMEOUT_MS = 3_000
MAX_TIMEOUT_MS = 30_000
MAX_RECENT_RECAPS = 100
OUTPUT_EXCERPT_CHARS = 1_000
CONFIG_REFRESH_SECONDS = 1.0
CONFIG_ERROR_LOG_INTERVAL_SECONDS = 60.0
DEFAULT_RECAP_QUEUE_SIZE = 256
RECAP_OVERFLOW_LOG_INTERVAL_SECONDS = 60.0
_HOOK_CONVERSATION_PREFIX = "turn-hook:"


class HookInvoker(Protocol):
    """One opaque request/reply exchange with a configured actor."""

    def invoke(
        self,
        handler: str,
        event: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class TurnHookConfig:
    events: frozenset[str]
    handler: str | None
    timeout_ms: int
    recent_recaps: int
    recap: bool

    @classmethod
    def from_json(cls, value: object) -> TurnHookConfig:
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise ValueError("turn hook config requires schemaVersion 1")
        raw_events = value.get("events", [])
        if not isinstance(raw_events, list) or not all(
            isinstance(item, str) for item in raw_events
        ):
            raise ValueError("turn hook events must be a string list")
        events = frozenset(raw_events)
        unknown = events - {"before-delivery", "after-turn"}
        if unknown:
            raise ValueError(f"unknown turn hook events: {sorted(unknown)!r}")
        handler = value.get("handler")
        if handler is not None and (
            not isinstance(handler, str) or parse_agent_uri(handler) is None
        ):
            raise ValueError("turn hook handler must be a canonical agent URI")
        if events and handler is None:
            raise ValueError("turn hook events require a handler")
        timeout_ms = value.get("timeoutMs", DEFAULT_TIMEOUT_MS)
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or not 1 <= timeout_ms <= MAX_TIMEOUT_MS
        ):
            raise ValueError(
                f"turn hook timeoutMs must be between 1 and {MAX_TIMEOUT_MS}"
            )
        recent_recaps = value.get("recentRecaps", 0)
        if (
            not isinstance(recent_recaps, int)
            or isinstance(recent_recaps, bool)
            or not 0 <= recent_recaps <= MAX_RECENT_RECAPS
        ):
            raise ValueError(
                f"turn hook recentRecaps must be between 0 and {MAX_RECENT_RECAPS}"
            )
        recap = value.get("recap", False)
        if not isinstance(recap, bool):
            raise ValueError("turn hook recap must be boolean")
        return cls(events, handler, timeout_ms, recent_recaps, recap)


@dataclass(slots=True)
class _PendingBefore:
    recipient: str
    deadline: float
    outcome: queue.Queue[tuple[str, object, float]]
    prepared: HarnessDelivery | None = None
    dispatched: bool = False


@dataclass(slots=True)
class _CachedConfiguration:
    home: Path | None
    path: Path | None
    mtime_ns: int | None
    size: int | None
    config: TurnHookConfig | None
    refresh_after: float


@dataclass(frozen=True, slots=True)
class _RecapWrite:
    home: Path
    value: Mapping[str, Any]
    delivery: HarnessDelivery
    event: str
    on_written: Callable[[], None] | None = None


class InboxTurnHookInvoker:
    """Exchange hook events through the ordinary actor inbox."""

    def __init__(
        self,
        inbox: InboxPort,
        *,
        callback_actor: str,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if parse_agent_uri(callback_actor) is None:
            raise ValueError("hook callback actor must be a canonical agent URI")
        # Production passes the actor-owned DeliveryCustodyFacade.  Keep the
        # name aligned with that composed port rather than implying direct
        # InboxService ownership at this call site.
        self._port = inbox
        self._callback_actor = callback_actor
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._active_conversations: set[str] = set()
        self._active_lock = threading.Lock()

    def invoke(
        self,
        handler: str,
        event: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> str | None:
        exchange_id = uuid4().hex
        conversation_id = f"{_HOOK_CONVERSATION_PREFIX}{exchange_id}"
        message = InboxMessage(
            message_id=f"turn-hook-{exchange_id}",
            conversation_id=conversation_id,
            sender=self._callback_actor,
            recipient=handler,
            payload=json.dumps(
                {
                    "message": json.dumps(
                        event,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    HOOK_REQUEST_MARKER: True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode(),
            intent="request",
            # A bounded hook exchange is meaningful only while both actors are
            # online.  Do not create a durable retry stream for a handler that
            # cannot answer inside this invocation's deadline.
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            created_at_ms=self._clock_ms(),
            idempotency_key=f"turn-hook:{exchange_id}",
        )
        with self._active_lock:
            self._active_conversations.add(conversation_id)
        try:
            submitted = self._port.submit(message)
        except Exception:
            with self._active_lock:
                self._active_conversations.discard(conversation_id)
            raise
        if not submitted.accepted:
            with self._active_lock:
                self._active_conversations.discard(conversation_id)
            raise RuntimeError(submitted.code or "hook request was rejected")
        deadline = time.monotonic() + timeout_seconds
        try:
            while time.monotonic() < deadline:
                reply = self._reply_and_sweep_stale(conversation_id, handler)
                if reply is not None:
                    text = _message_text(reply)
                    self._port.ack(self._callback_actor, reply.message_id)
                    return text
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        finally:
            with self._active_lock:
                self._active_conversations.discard(conversation_id)

        # Retire a local request row that missed its deadline.  ONLINE_ONLY
        # prevents offline retry accumulation; this ack bounds the same-daemon
        # handler case, and the final sweep catches a reply that raced timeout.
        try:
            self._port.ack(handler, message.message_id)
        except Exception:
            # Cleanup is advisory after the deadline; the timeout remains the
            # invocation's authoritative outcome and stale replies still get
            # their own best-effort sweep.
            pass
        try:
            self._reply_and_sweep_stale(conversation_id, handler)
        except Exception:
            # A cleanup failure must not replace the bounded timeout result.
            pass
        raise TimeoutError("hook handler timed out")

    def _reply_and_sweep_stale(
        self, conversation_id: str, handler: str
    ) -> InboxMessage | None:
        messages = self._port.pending_messages(self._callback_actor)
        with self._active_lock:
            active = frozenset(self._active_conversations)
        reply: InboxMessage | None = None
        for item in messages:
            if not item.conversation_id.startswith(_HOOK_CONVERSATION_PREFIX):
                continue
            if (
                item.conversation_id == conversation_id
                and item.sender == handler
                and item.conversation_id in active
            ):
                reply = item
                continue
            if item.conversation_id not in active:
                self._port.ack(self._callback_actor, item.message_id)
        return reply


class TurnHookService:
    """Load opt-in config, prepare deliveries, and persist neutral recaps."""

    def __init__(
        self,
        *,
        home_for_agent: Callable[[str], Path | None],
        invoker: HookInvoker,
        logger: Callable[..., None] | None = None,
        before_delivery_supported: Callable[[str], bool] | None = None,
        config_path_for_agent: Callable[[str], Path] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        clock_ms: Callable[[], int] | None = None,
        recap_queue_size: int = DEFAULT_RECAP_QUEUE_SIZE,
    ) -> None:
        if recap_queue_size < 1:
            raise ValueError("recap_queue_size must be positive")
        self._home_for_agent = home_for_agent
        self._invoker = invoker
        self._logger = logger
        self._before_delivery_supported = before_delivery_supported
        self._config_path_for_agent = config_path_for_agent
        self._monotonic = monotonic
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._pending: dict[str, _PendingBefore] = {}
        self._pending_lock = threading.Lock()
        self._append_locks: dict[Path, threading.Lock] = {}
        self._append_locks_lock = threading.Lock()
        self._config_cache: dict[str, _CachedConfiguration] = {}
        self._config_cache_lock = threading.Lock()
        self._config_error_logged_at: dict[str, float] = {}
        self._recap_writes: queue.Queue[_RecapWrite] = queue.Queue(
            maxsize=recap_queue_size
        )
        self._recap_writer: threading.Thread | None = None
        self._recap_writer_lock = threading.Lock()
        self._recap_wake = threading.Event()
        self._recap_closing = False
        self._recap_overflow_count = 0
        self._recap_overflow_logged_at: float | None = None

    def prepare_delivery(self, delivery: HarnessDelivery) -> HarnessDelivery | None:
        """Return a deliverable value, or ``None`` while its bounded hook runs."""

        home, config = self._configuration(delivery.recipient)
        if home is None or config is None or "before-delivery" not in config.events:
            return delivery
        with self._pending_lock:
            pending = self._pending.get(delivery.delivery_id)
            if pending is None:
                outcome: queue.Queue[tuple[str, object, float]] = queue.Queue(
                    maxsize=1
                )
                pending = _PendingBefore(
                    recipient=delivery.recipient,
                    deadline=self._monotonic() + config.timeout_ms / 1000,
                    outcome=outcome,
                )
                self._pending[delivery.delivery_id] = pending
                event = {
                    "event": "before-delivery",
                    "agent": delivery.recipient,
                    "delivery": {
                        "id": delivery.delivery_id,
                        "sender": delivery.sender,
                        "conversation": delivery.conversation_id,
                        "message": delivery.message,
                    },
                    "recentRecaps": self._recent_recaps(
                        home, config.recent_recaps
                    ),
                }
                self._start_invocation(
                    config,
                    event,
                    outcome,
                    thread_name=f"hyprial-hook-before-{delivery.delivery_id[:12]}",
                )
                return None
            if pending.dispatched:
                # mark_dispatched is called only after prepare_delivery
                # returned an accepted projection.  A transient config read
                # failure can fail open with the original delivery before the
                # pending hook has prepared one, so this branch must too.
                return pending.prepared if pending.prepared is not None else delivery
            if pending.prepared is not None:
                return pending.prepared
            try:
                kind, value, completed_at = pending.outcome.get_nowait()
            except queue.Empty:
                if self._monotonic() < pending.deadline:
                    return None
                self._log_outcome("timeout", delivery=delivery)
                pending.prepared = delivery
                return delivery
            if completed_at > pending.deadline:
                self._log_outcome("timeout", delivery=delivery)
                pending.prepared = delivery
                return delivery
            if kind == "text" and isinstance(value, str) and value:
                self._log_outcome("text", delivery=delivery)
                pending.prepared = replace(delivery, hook_text=value)
            elif kind == "text":
                self._log_outcome("empty", delivery=delivery)
                pending.prepared = delivery
            elif kind == "timeout":
                self._log_outcome("timeout", delivery=delivery)
                pending.prepared = delivery
            else:
                self._log_outcome("error", delivery=delivery, detail=str(value))
                pending.prepared = delivery
            return pending.prepared

    def mark_dispatched(self, delivery_id: str) -> None:
        with self._pending_lock:
            pending = self._pending.get(delivery_id)
            if pending is not None:
                pending.dispatched = True

    def forget_delivery(self, delivery_id: str) -> None:
        """Release before-hook state once a turn or one-shot notice settles."""

        with self._pending_lock:
            self._pending.pop(delivery_id, None)

    def forget_missing_deliveries(
        self, recipient: str, present_delivery_ids: set[str]
    ) -> None:
        """Release holds whose inbox rows were acknowledged or expired."""

        with self._pending_lock:
            missing = tuple(
                delivery_id
                for delivery_id, pending in self._pending.items()
                if pending.recipient == recipient
                and delivery_id not in present_delivery_ids
            )
            for delivery_id in missing:
                self._pending.pop(delivery_id, None)

    @property
    def recap_overflow_count(self) -> int:
        with self._recap_writer_lock:
            return self._recap_overflow_count

    def observe_turn(
        self,
        delivery: HarnessDelivery,
        result: HarnessResult,
        *,
        started_at_ms: int,
        ended_at_ms: int,
        tool_names: tuple[str, ...] = (),
    ) -> None:
        """Append an opted-in record and dispatch its optional observer."""

        # A failed or interrupted turn can be retried from the same durable
        # inbox row.  Keep its prepared hook projection until runtime settles
        # the row terminally or proves that it disappeared; otherwise failure
        # backoff would cause the policy hook to run again for one delivery.
        if result.status is HarnessResultStatus.COMPLETED:
            self.forget_delivery(delivery.delivery_id)
        if delivery.hook_request:
            return
        home, config = self._configuration(delivery.recipient)
        if home is None or config is None or not config.recap:
            return
        record = {
            "schemaVersion": 1,
            "event": "turn",
            "deliveryId": _safe_identifier(delivery.delivery_id),
            "sender": _safe_identifier(delivery.sender),
            "conversation": _safe_identifier(delivery.conversation_id),
            "startedAtMs": int(started_at_ms),
            "endedAtMs": int(ended_at_ms),
            "status": result.status.value,
            "toolNames": _safe_tool_names(tool_names),
            "outputExcerpt": _safe_excerpt(result.output),
        }
        def dispatch_after_turn() -> None:
            if "after-turn" not in config.events:
                return
            outcome: queue.Queue[tuple[str, object, float]] = queue.Queue(maxsize=1)
            self._start_invocation(
                config,
                {"event": "after-turn", "agent": delivery.recipient, "turn": record},
                outcome,
                thread_name=f"hyprial-hook-after-{delivery.delivery_id[:12]}",
            )

            def store_annotation() -> None:
                kind, value, _completed_at = outcome.get()
                if kind == "text" and isinstance(value, str) and value:
                    self._enqueue_recap(
                        _RecapWrite(
                            home=home,
                            value={
                                "schemaVersion": 1,
                                "event": "annotation",
                                "deliveryId": _safe_identifier(
                                    delivery.delivery_id
                                ),
                                "recordedAtMs": self._clock_ms(),
                                # Opaque means no semantic interpretation.  The
                                # same portability fence as turn excerpts still
                                # applies before this text becomes agent state.
                                "annotation": _safe_excerpt(value),
                            },
                            delivery=delivery,
                            event="after-turn",
                        )
                    )
                    self._log_outcome("text", delivery=delivery, event="after-turn")
                elif kind == "text":
                    self._log_outcome("empty", delivery=delivery, event="after-turn")
                elif kind == "timeout":
                    self._log_outcome("timeout", delivery=delivery, event="after-turn")
                else:
                    self._log_outcome(
                        "error",
                        delivery=delivery,
                        event="after-turn",
                        detail=str(value),
                    )

            threading.Thread(
                target=store_annotation,
                name=f"hyprial-hook-store-{delivery.delivery_id[:12]}",
                daemon=True,
            ).start()

        self._enqueue_recap(
            _RecapWrite(
                home=home,
                value=record,
                delivery=delivery,
                event="turn",
                on_written=dispatch_after_turn,
            )
        )

    def _configuration(
        self, agent: str
    ) -> tuple[Path | None, TurnHookConfig | None]:
        now = self._monotonic()
        with self._config_cache_lock:
            cached = self._config_cache.get(agent)
            if cached is not None and now < cached.refresh_after:
                return cached.home, cached.config
        home = cached.home if cached is not None else None
        path = cached.path if cached is not None else None
        if self._config_path_for_agent is not None:
            try:
                path = self._config_path_for_agent(agent)
            except Exception as error:  # noqa: BLE001 - hooks never own delivery
                self._log_config_error(agent, error)
                self._cache_configuration(agent, None, None, None, None, None, now)
                return None, None
        elif home is None:
            try:
                home = self._home_for_agent(agent)
            except Exception as error:  # noqa: BLE001 - hooks never own delivery
                self._log_config_error(agent, error)
                self._cache_configuration(agent, None, None, None, None, None, now)
                return None, None
            if home is None:
                self._cache_configuration(agent, None, None, None, None, None, now)
                return None, None
            path = home / "config" / HOOK_CONFIG_NAME
        assert path is not None
        try:
            stat = path.stat()
        except FileNotFoundError:
            self._cache_configuration(agent, home, path, None, None, None, now)
            return home, None
        except OSError as error:
            self._log_config_error(agent, error)
            self._cache_configuration(agent, home, path, None, None, None, now)
            return home, None
        with self._config_cache_lock:
            cached = self._config_cache.get(agent)
            if (
                cached is not None
                and cached.path == path
                and cached.mtime_ns == stat.st_mtime_ns
                and cached.size == stat.st_size
            ):
                cached.refresh_after = now + CONFIG_REFRESH_SECONDS
                return cached.home, cached.config
        if home is None:
            try:
                home = self._home_for_agent(agent)
            except Exception as error:  # noqa: BLE001 - hooks never own delivery
                self._log_config_error(agent, error)
                self._cache_configuration(
                    agent, None, path, stat.st_mtime_ns, stat.st_size, None, now
                )
                return None, None
            if home is None:
                self._cache_configuration(
                    agent, None, path, stat.st_mtime_ns, stat.st_size, None, now
                )
                return None, None
        try:
            config = TurnHookConfig.from_json(
                json.loads(path.read_text(encoding="utf-8"))
            )
            self._validate_handler(agent, config)
            if (
                "before-delivery" in config.events
                and self._before_delivery_supported is not None
            ):
                support_error: BaseException | None = None
                try:
                    supported = self._before_delivery_supported(agent)
                except Exception as error:  # noqa: BLE001 - fail open
                    support_error = error
                    supported = False
                if not supported:
                    self._log_before_delivery_unsupported(
                        agent,
                        support_error
                        or ValueError(
                            "before-delivery hooks are unsupported for this "
                            "harness kind"
                        ),
                    )
                    config = replace(
                        config, events=config.events - {"before-delivery"}
                    )
        except Exception as error:  # noqa: BLE001 - config cannot own the tick
            self._log_config_error(agent, error)
            config = None
        self._cache_configuration(
            agent, home, path, stat.st_mtime_ns, stat.st_size, config, now
        )
        return home, config

    def _cache_configuration(
        self,
        agent: str,
        home: Path | None,
        path: Path | None,
        mtime_ns: int | None,
        size: int | None,
        config: TurnHookConfig | None,
        now: float,
    ) -> None:
        with self._config_cache_lock:
            self._config_cache[agent] = _CachedConfiguration(
                home,
                path,
                mtime_ns,
                size,
                config,
                now + CONFIG_REFRESH_SECONDS,
            )

    def _validate_handler(self, agent: str, config: TurnHookConfig) -> None:
        handler = config.handler
        if handler is None:
            return
        if handler == agent:
            raise ValueError("turn hook handler must differ from the hooked agent")
        try:
            if self._config_path_for_agent is not None:
                handler_path = self._config_path_for_agent(handler)
            else:
                handler_home = self._home_for_agent(handler)
                if handler_home is None:
                    return
                handler_path = handler_home / "config" / HOOK_CONFIG_NAME
            handler_value = json.loads(handler_path.read_text(encoding="utf-8"))
            handler_config = TurnHookConfig.from_json(handler_value)
        except Exception:  # noqa: BLE001 - foreign handlers have no local path
            # Back-pointer validation is a local safety check.  A canonical
            # handler on another machine is valid, but the local registry path
            # resolver rejects it; that says nothing about the remote config.
            return
        if handler_config.handler == agent:
            raise ValueError(
                "turn hook handler configuration points back to the hooked agent"
            )

    def _log_before_delivery_unsupported(
        self, agent: str, error: BaseException
    ) -> None:
        if self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "turn_hook.before_delivery_unsupported",
                agent=agent,
                errorType=type(error).__name__,
                detail=str(error)[:300],
            )

    def _start_invocation(
        self,
        config: TurnHookConfig,
        event: Mapping[str, Any],
        outcome: queue.Queue[tuple[str, object, float]],
        *,
        thread_name: str,
    ) -> None:
        assert config.handler is not None

        def invoke() -> None:
            try:
                text = self._invoker.invoke(
                    config.handler,
                    event,
                    timeout_seconds=config.timeout_ms / 1000,
                )
            except TimeoutError as error:
                outcome.put(("timeout", error, self._monotonic()))
            except Exception as error:  # noqa: BLE001 - hook failure is fail-open
                outcome.put(("error", error, self._monotonic()))
            else:
                outcome.put(("text", text, self._monotonic()))

        threading.Thread(target=invoke, name=thread_name, daemon=True).start()

    def _append(self, home: Path, value: Mapping[str, Any]) -> None:
        path = home / "state" / "recaps.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._append_locks_lock:
            lock = self._append_locks.setdefault(path, threading.Lock())
        encoded = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode()
        with lock:
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "ab", closefd=True) as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())

    def _enqueue_recap(self, item: _RecapWrite) -> bool:
        with self._recap_writer_lock:
            if self._recap_closing:
                closed = True
            else:
                closed = False
                try:
                    self._recap_writes.put_nowait(item)
                except queue.Full:
                    now = self._monotonic()
                    self._recap_overflow_count += 1
                    previous = self._recap_overflow_logged_at
                    should_log = (
                        previous is None
                        or now - previous >= RECAP_OVERFLOW_LOG_INTERVAL_SECONDS
                    )
                    if should_log:
                        self._recap_overflow_logged_at = now
                    dropped = self._recap_overflow_count
                else:
                    self._recap_wake.set()
                    if self._recap_writer is None or not self._recap_writer.is_alive():
                        self._recap_writer = threading.Thread(
                            target=self._write_recaps,
                            name="hyprial-recap-writer",
                            daemon=True,
                        )
                        self._recap_writer.start()
                    return True
        if closed:
            if self._logger is not None:
                self._logger(
                    "info",
                    "daemon",
                    "turn_hook.recap_after_close",
                    agent=item.delivery.recipient,
                    deliveryId=item.delivery.delivery_id,
                )
            return False
        if should_log and self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "turn_hook.recap_overflow",
                agent=item.delivery.recipient,
                deliveryId=item.delivery.delivery_id,
                dropped=dropped,
            )
        return False

    def close(self, timeout: float = 5.0) -> bool:
        """Drain queued recap writes and stop the writer within ``timeout``."""

        if timeout < 0:
            raise ValueError("recap writer close timeout must not be negative")
        with self._recap_writer_lock:
            self._recap_closing = True
            writer = self._recap_writer
            self._recap_wake.set()
        if writer is None:
            return self._recap_writes.unfinished_tasks == 0
        writer.join(timeout)
        with self._recap_writer_lock:
            stopped = not writer.is_alive()
            if stopped and self._recap_writer is writer:
                self._recap_writer = None
        return stopped and self._recap_writes.unfinished_tasks == 0

    def _write_recaps(self) -> None:
        while True:
            try:
                item = self._recap_writes.get_nowait()
            except queue.Empty:
                self._recap_wake.clear()
                with self._recap_writer_lock:
                    if self._recap_closing:
                        self._recap_writer = None
                        return
                    if not self._recap_writes.empty():
                        continue
                self._recap_wake.wait(0.5)
                with self._recap_writer_lock:
                    if self._recap_writes.empty():
                        self._recap_writer = None
                        return
                continue
            try:
                self._append(item.home, item.value)
            except OSError as error:
                self._log_outcome(
                    "error",
                    delivery=item.delivery,
                    event=item.event,
                    detail=str(error),
                )
            else:
                if item.on_written is not None:
                    item.on_written()
            finally:
                self._recap_writes.task_done()

    @staticmethod
    def _recent_recaps(home: Path, count: int) -> list[object]:
        if count == 0:
            return []
        path = home / "state" / "recaps.jsonl"
        try:
            with path.open(encoding="utf-8") as source:
                lines = tuple(deque(source, maxlen=count))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError):
            return []
        values: list[object] = []
        for line in lines:
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return values

    def _log_config_error(self, agent: str, error: BaseException) -> None:
        now = self._monotonic()
        with self._config_cache_lock:
            previous = self._config_error_logged_at.get(agent)
            if (
                previous is not None
                and now - previous < CONFIG_ERROR_LOG_INTERVAL_SECONDS
            ):
                return
            self._config_error_logged_at[agent] = now
        if self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "turn_hook.config_error",
                agent=agent,
                errorType=type(error).__name__,
                detail=str(error)[:300],
            )

    def _log_outcome(
        self,
        outcome: str,
        *,
        delivery: HarnessDelivery,
        event: str = "before-delivery",
        detail: str | None = None,
    ) -> None:
        if self._logger is not None:
            self._logger(
                "info" if outcome in {"text", "empty"} else "warn",
                "daemon",
                f"turn_hook.{outcome}",
                event=event,
                deliveryId=delivery.delivery_id,
                agent=delivery.recipient,
                **({"detail": detail[:300]} if detail else {}),
            )


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_ABSOLUTE_PATH = re.compile(
    r"(?<![\w:])(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+|[A-Za-z]:\\[^\s]+)"
)


def _safe_excerpt(value: str) -> str:
    excerpt = value[:OUTPUT_EXCERPT_CHARS]
    excerpt = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", excerpt)
    return _ABSOLUTE_PATH.sub("<path>", excerpt)


def _safe_identifier(value: str) -> str:
    return _safe_excerpt(value)[:300]


def _safe_tool_names(values: tuple[str, ...]) -> list[str]:
    output: list[str] = []
    for value in values:
        candidate = value.strip()[:100]
        if not candidate or "/" in candidate or "\\" in candidate:
            continue
        if candidate not in output:
            output.append(candidate)
    return output


def _message_text(message: InboxMessage) -> str:
    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return message.payload.decode("utf-8", errors="replace")
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"]
    return message.payload.decode("utf-8", errors="replace")


def is_hook_request(message: InboxMessage) -> bool:
    """Identify the mechanism's own request so it cannot recursively hook."""

    if HOOK_REQUEST_MARKER.encode() not in message.payload:
        return False
    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(body, dict) and body.get(HOOK_REQUEST_MARKER) is True


def harness_supports_before_delivery(harness: str) -> bool:
    """Whether the declared headless mechanism can inject prompt context."""

    # Deferred to avoid the daemon/harness import cycle during module loading.
    from hyprial.harnesses.capabilities import Capability, support

    declared = support(
        harness,
        headless=True,
        capability=Capability.HEADLESS_EXEC,
    )
    return declared.mechanism != "python_worker"


__all__ = [
    "HOOK_CONFIG_NAME",
    "InboxTurnHookInvoker",
    "TurnHookConfig",
    "TurnHookService",
    "harness_supports_before_delivery",
    "is_hook_request",
]
