"""Durable sender-side custody for direct agent-bot DM attempts."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError
from hyprial.daemon.impl.network.route_delivery import (
    find_gateway,
    map_lark_send_error,
)
from hyprial.kernel import DaemonRequestError, ipc_errors


AGENT_BOT_ATTEMPT_RETENTION_DAYS = 30
AGENT_BOT_ATTEMPT_MAX_ENTRIES = 10_000
LARK_DEDUPE_WINDOW_MS = 60 * 60 * 1000
_RETENTION_MS = AGENT_BOT_ATTEMPT_RETENTION_DAYS * 24 * 60 * 60 * 1000

ATTEMPT_ACCEPTED = "accepted"
ATTEMPT_DEFINITELY_NOT_SENT = "definitely-not-sent"
ATTEMPT_IN_FLIGHT = "in-flight"
ATTEMPT_UNCERTAIN = "uncertain"
_LOCKING_STATES = frozenset(
    {ATTEMPT_ACCEPTED, ATTEMPT_IN_FLIGHT, ATTEMPT_UNCERTAIN}
)

_INVALID_RECEIVE_ID_CODE = 230001
_USER_UNREACHABLE_CODE = 230013
_RATE_LIMIT_CODES = frozenset({230020, 99991400})
_MAPPED_PRE_SEND_CODES = frozenset(
    {"ROUTE_APP_BOT_ABILITY_DISABLED", "ROUTE_APP_SCOPE_REQUIRED"}
)


@dataclass(frozen=True, slots=True)
class AgentBotAttempt:
    adapter: str
    lark_idempotency_key: str
    state: str
    delivery: dict[str, Any] | None = None
    reason: str | None = None
    recorded_at_ms: int = 0
    # When the first native send began; the Lark uuid dedupe window runs from
    # here, not from a later settle (which can trail the POST by the call
    # timeout).  None for a fallback that never reached a send.
    first_send_at_ms: int | None = None
    # The sender that began the attempt: audit only.  A replay is decided by
    # the record whatever its caller, so a changed sender cannot double-send.
    sender: str | None = None

    @property
    def locks_fallback(self) -> bool:
        return self.state in _LOCKING_STATES


class AgentBotAttemptLedger:
    """Bounded atomic JSON ledger, separate from receiver user delivery."""

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = AGENT_BOT_ATTEMPT_MAX_ENTRIES,
        retention_ms: int = _RETENTION_MS,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if max_entries < 1 or retention_ms < 1:
            raise ValueError("agent-bot attempt retention bounds must be positive")
        self.path = Path(path)
        self._max_entries = max_entries
        self._retention_ms = retention_ms
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._lock = threading.RLock()
        # Exact per-key locks, refcounted and dropped when idle: a lock held
        # across one bounded native send must never block another key.
        self._key_locks: dict[str, list[Any]] = {}
        self._key_locks_guard = threading.Lock()

    @contextmanager
    def key_lock(self, key: str) -> Iterator[None]:
        """Serialize one operation key across read, decision, send, and settle."""

        with self._key_locks_guard:
            entry = self._key_locks.setdefault(key, [threading.RLock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._key_locks_guard:
                entry[1] -= 1
                if entry[1] == 0:
                    self._key_locks.pop(key, None)

    def now_ms(self) -> int:
        return self._clock_ms()

    @contextmanager
    def _readable(self) -> Iterator[None]:
        """An unreadable ledger is a typed refusal, never a raw error.

        It propagates out of message.send before any fallback: the prior
        attempts are unknown, so the user-proxy path must not be tried either.
        """

        try:
            yield
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise DaemonRequestError(
                ipc_errors.USER_DELIVERY_UNAVAILABLE,
                "agent-bot attempt ledger unreadable; no direct or fallback "
                "delivery was attempted",
                {"ledger": self.path.name, "errorType": type(error).__name__},
            ) from error

    def get(self, key: str) -> AgentBotAttempt | None:
        with self._lock:
            with self._readable():
                loaded = self._load()
                values = self._retained(loaded)
                raw = values.get(key)
                attempt = self._attempt(raw) if raw is not None else None
            if values != loaded:
                # The prune is housekeeping: a failed write (ENOSPC) must not
                # turn a record that was read into "unreadable", which would
                # send an ineligible replay to the proxy.  "Unreadable" means
                # only that the read or the parse failed.
                try:
                    self._save(values)
                except OSError:
                    pass
            return attempt

    def begin(
        self,
        key: str,
        *,
        adapter: str,
        lark_idempotency_key: str,
        sender: str | None = None,
    ) -> tuple[AgentBotAttempt, bool]:
        with self._lock, self._readable():
            loaded = self._load()
            values = self._retained(loaded)
            raw = values.get(key)
            if raw is not None:
                existing = self._attempt(raw)
                if existing.locks_fallback:
                    if values != loaded:
                        self._save(values)
                    return existing, False
            recorded_at_ms = self._clock_ms()
            attempt = AgentBotAttempt(
                adapter=adapter,
                lark_idempotency_key=lark_idempotency_key,
                state=ATTEMPT_IN_FLIGHT,
                recorded_at_ms=recorded_at_ms,
                first_send_at_ms=recorded_at_ms,
                sender=sender,
            )
            values[key] = self._raw(attempt, recorded_at_ms)
            self._save(self._retained(values))
            return attempt, True

    def settle(
        self,
        key: str,
        *,
        adapter: str,
        lark_idempotency_key: str,
        state: str,
        delivery: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> AgentBotAttempt:
        if state not in {
            ATTEMPT_ACCEPTED,
            ATTEMPT_DEFINITELY_NOT_SENT,
            ATTEMPT_UNCERTAIN,
        }:
            raise ValueError("invalid agent-bot attempt settlement")
        with self._lock, self._readable():
            values = self._retained(self._load())
            previous = values.get(key) or {}
            first_send = previous.get("firstSendAtMs")
            attempt = AgentBotAttempt(
                adapter=adapter,
                lark_idempotency_key=lark_idempotency_key,
                state=state,
                delivery=dict(delivery) if delivery is not None else None,
                reason=reason,
                recorded_at_ms=self._clock_ms(),
                first_send_at_ms=(
                    first_send
                    if isinstance(first_send, int) and not isinstance(first_send, bool)
                    else None
                ),
                sender=(
                    str(previous["sender"])
                    if previous.get("sender") is not None
                    else None
                ),
            )
            values[key] = self._raw(attempt, attempt.recorded_at_ms)
            self._save(self._retained(values))
        return attempt

    def _retained(
        self, values: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        cutoff = self._clock_ms() - self._retention_ms
        retained: dict[str, dict[str, Any]] = {}
        for key, raw in values.items():
            if not isinstance(raw, dict):
                raise TypeError("agent-bot attempt ledger item must be an object")
            timestamp = raw.get("recordedAtMs")
            if not isinstance(timestamp, int) or isinstance(timestamp, bool):
                raise TypeError("agent-bot attempt timestamp must be an integer")
            if timestamp >= cutoff:
                retained[key] = raw
        excess = len(retained) - self._max_entries
        if excess > 0:
            evictable = [
                key
                for key, raw in retained.items()
                # Only rows that lock nothing may go: an accepted row is what
                # turns a replay into a duplicate instead of a second DM, and
                # in-flight/uncertain rows block fallback.  Retention by age
                # still bounds the file.
                if raw.get("state") == ATTEMPT_DEFINITELY_NOT_SENT
            ]
            oldest = sorted(
                evictable,
                key=lambda key: (retained[key]["recordedAtMs"], key),
            )[:excess]
            for key in oldest:
                retained.pop(key)
        return retained

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
            raise ValueError("unsupported agent-bot attempt ledger schema")
        attempts = raw.get("attempts")
        if not isinstance(attempts, dict):
            raise TypeError("agent-bot attempt ledger attempts must be an object")
        return attempts

    def _save(self, values: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {"schemaVersion": 1, "attempts": values},
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _attempt(raw: object) -> AgentBotAttempt:
        if not isinstance(raw, dict):
            raise TypeError("agent-bot attempt ledger item must be an object")
        delivery = raw.get("delivery")
        if delivery is not None and not isinstance(delivery, dict):
            raise TypeError("agent-bot attempt delivery must be an object")
        return AgentBotAttempt(
            adapter=str(raw["adapter"]),
            lark_idempotency_key=str(raw["larkIdempotencyKey"]),
            state=str(raw["state"]),
            delivery=dict(delivery) if delivery is not None else None,
            reason=str(raw["reason"]) if raw.get("reason") is not None else None,
            recorded_at_ms=int(raw["recordedAtMs"]),
            first_send_at_ms=(
                int(raw["firstSendAtMs"])
                if raw.get("firstSendAtMs") is not None
                else None
            ),
            sender=str(raw["sender"]) if raw.get("sender") is not None else None,
        )

    @staticmethod
    def _raw(attempt: AgentBotAttempt, recorded_at_ms: int) -> dict[str, Any]:
        return {
            "adapter": attempt.adapter,
            "larkIdempotencyKey": attempt.lark_idempotency_key,
            "state": attempt.state,
            "delivery": attempt.delivery,
            "reason": attempt.reason,
            "recordedAtMs": recorded_at_ms,
            "firstSendAtMs": attempt.first_send_at_ms,
            "sender": attempt.sender,
        }


def lark_pre_send_refusal_reason(
    error: LarkApiError, *, adapter: str, open_id: str
) -> str | None:
    """Return a reason only for documented pre-acceptance Lark refusals."""

    mapped = map_lark_send_error(
        error,
        adapter=adapter,
        route="owner-dm",
        chat_id=open_id,
    )
    if mapped.code in _MAPPED_PRE_SEND_CODES:
        return mapped.code
    if error.code == _USER_UNREACHABLE_CODE:
        return "LARK_USER_NOT_REACHABLE"
    if error.code in _RATE_LIMIT_CODES:
        return "LARK_RATE_LIMITED_BEFORE_ACCEPTANCE"
    message = (error.lark_message or "").lower().replace("-", "_")
    if error.code == _INVALID_RECEIVE_ID_CODE and "receive_id" in message:
        return "LARK_INVALID_RECEIVE_ID"
    return None


class AgentBotDeliverySupport:
    """Small helpers shared by the message-send agent-bot branch."""

    def _agent_bot_sender(
        self, caller: Any, *, trusted_origin: str | None
    ) -> tuple[Any | None, str]:
        if caller.via != "session" and not (
            caller.via == "daemon" and trusted_origin == "daemon"
        ):
            return None, "sender-kind-ineligible"
        if caller.sender != caller.subject:
            return None, "delegated-sender-ineligible"
        agent = self.agents.get(caller.subject)
        if (
            agent is None
            or agent.uri != caller.sender
            or agent.owner != self.owner
            or agent.machine != self.node_id
        ):
            return None, "sender-not-local-agent"
        return agent, "eligible"

    def _record_agent_bot_fallback(
        self,
        target_key: str,
        *,
        adapter: str | None,
        reason: str,
        lark_idempotency_key: str | None = None,
    ) -> None:
        recorded_adapter = adapter or ""
        self._agent_bot_attempts.settle(
            target_key,
            adapter=recorded_adapter,
            lark_idempotency_key=lark_idempotency_key
            or f"{target_key}:agent-bot:{recorded_adapter or 'unresolved'}",
            state=ATTEMPT_DEFINITELY_NOT_SENT,
            reason=reason,
        )

    def _agent_bot_adapter(
        self, agent: Any, *, locked_adapter: str | None = None
    ) -> tuple[str | None, str]:
        lark_names = set(self._lark_gateway_names())
        lark_pins = tuple(
            adapter for adapter in agent.pinned_adapters if adapter in lark_names
        )
        if locked_adapter is not None:
            if locked_adapter in lark_pins:
                return locked_adapter, "eligible-replay"
            return None, "recorded-adapter-unpinned"
        if not lark_pins:
            return None, "sender-has-no-lark-pin"
        if len(lark_pins) != 1:
            return None, "multiple-adapter-pins"
        return lark_pins[0], "eligible"

    def _running_agent_bot_gateway(self, adapter: str) -> tuple[Any | None, str]:
        if self._adapters is None or adapter not in self._lark_gateway_names():
            return None, "adapter-not-configured"
        if self._lark_client is None:
            return None, "adapter-status-unavailable"
        try:
            # The adapter projection, as every application reader uses it.
            projection = self._lark_client.read_adapter(adapter)
        except Exception:  # noqa: BLE001 - no send was attempted
            return None, "adapter-status-unavailable"
        if projection is None or getattr(projection, "online", None) is not True:
            return None, "adapter-not-running"
        try:
            gateway_config = find_gateway(
                self.load_persistent_configuration().channels,
                adapter,
            )
            return self._route_lark_gateway(gateway_config), "available"
        except Exception:  # noqa: BLE001 - gateway construction precedes send
            return None, "outbound-gateway-unavailable"

    def _agent_bot_outcome_unknown(
        self,
        *,
        target: str,
        target_key: str,
        lark_idempotency_key: str,
        message_id: str,
        sender: str,
        owner: str,
        adapter: str,
        reason: str,
        conversation_id: str,
        record_uncertain: bool = True,
    ) -> dict[str, Any]:
        if record_uncertain:
            self._agent_bot_attempts.settle(
                target_key,
                adapter=adapter,
                lark_idempotency_key=lark_idempotency_key,
                state=ATTEMPT_UNCERTAIN,
                reason=reason,
            )
        self._log_agent_bot_attempt(
            message_id=message_id,
            sender=sender,
            owner=owner,
            adapter=adapter,
            outcome="uncertain",
            reason=reason,
            conversation_id=conversation_id,
        )
        return {
            "target": target,
            "messageId": message_id,
            "accepted": False,
            "queued": False,
            "outcomeKnown": False,
            "code": ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
            "via": "agent-bot",
            "adapter": adapter,
        }

    def _log_agent_bot_attempt(
        self,
        *,
        message_id: str,
        sender: str,
        owner: str,
        adapter: str | None,
        outcome: str,
        reason: str,
        conversation_id: str,
    ) -> None:
        self._log(
            "info",
            "daemon",
            "user-delivery.agent-bot",
            messageId=message_id,
            conversationId=conversation_id,
            sender=sender,
            owner=owner,
            adapter=adapter,
            outcome=outcome,
            reason=reason,
        )

    def _accepted_agent_bot_delivery(
        self,
        *,
        target: str,
        target_key: str,
        message_id: str,
        native_message_id: object,
        sender: str,
        owner: str,
        adapter: str,
        lark_idempotency_key: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        delivery = {
            "target": target,
            "messageId": message_id,
            "accepted": True,
            "queued": False,
            "receiverAdapter": True,
            "nativeMessageId": str(native_message_id),
            "via": "agent-bot",
            "adapter": adapter,
        }
        try:
            self._agent_bot_attempts.settle(
                target_key,
                adapter=adapter,
                lark_idempotency_key=lark_idempotency_key,
                state=ATTEMPT_ACCEPTED,
                delivery=delivery,
            )
        except Exception as error:  # noqa: BLE001 - accepted native result wins
            self._log(
                "warn",
                "daemon",
                "user-delivery.agent-bot-settle-failed",
                messageId=message_id,
                conversationId=conversation_id,
                sender=sender,
                owner=owner,
                adapter=adapter,
                errorType=type(error).__name__,
            )
        self._log_agent_bot_attempt(
            message_id=message_id,
            sender=sender,
            owner=owner,
            adapter=adapter,
            outcome="delivered",
            reason="accepted",
            conversation_id=conversation_id,
        )
        return delivery

    def _log_definite_agent_bot_replay(
        self,
        *,
        message_id: str,
        sender: str,
        owner: str,
        adapter: str,
        conversation_id: str,
    ) -> None:
        self._log_agent_bot_attempt(
            message_id=message_id,
            sender=sender,
            owner=owner,
            adapter=adapter,
            outcome="fallback",
            reason="definitely-not-sent-replay",
            conversation_id=conversation_id,
        )
