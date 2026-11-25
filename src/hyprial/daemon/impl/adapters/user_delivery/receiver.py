# Decision notes (R2, design user-im-binding-removal-tier2 8.1 and 9.1 item 2):
# - This module now serves only another owner's ``user:`` target: the sender
#   puts the request on Zenoh and waits up to 3 s for the receipt of the
#   machine that owns that person; this node's own owner is decided on the
#   sending node (inbox_surface/send_targets/user.py).
# - The receiving side hands the request to the owner push the wiring passes
#   (I7a: inbox_surface.send._MessageSendMixin._push_owner_copy, which
#   posts to this node's user agent cell), else refuses TARGET_UNRESOLVED.  The squire DM by open id,
#   the adapter registry it used and the identity resolver are gone.
# - S3c (2026-11-24): the profile is gone; ``owns`` is the predicate the
#   wiring passes (inbox_surface.send._MessageSendMixin._accepts_owner_copy:
#   this node's owner, and its user agent cell names a push target).
# - Codes: through the delivery actor a node that does not accept (S3c note
#   above) answers nothing, so the sender's receipt wait ends with
#   USER_DELIVERY_TIMEOUT; a direct handle() without an accepting owner or a
#   user agent cell target is TARGET_UNRESOLVED (definitely not sent); a push refusal is that refusal, and a push whose
#   outcome is unknown is USER_DELIVERY_FAILED that stays recorded.  The
#   legacy constants stay registered: old receivers still send them and old
#   ledger rows hold them.  The wire frames are unchanged.
# Decision record (W2B-2, 2026-11-21):
# - UserDeliveryLedger._save hardens the temporary ledger file via
#   hyprial.kernel.harden_private_path before atomic replacement.
"""Receiver-owned ``user:`` routing over Zenoh to the owner's user agent cell."""

from __future__ import annotations

import json
import os
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from hyprial.daemon.impl.transport import KeySpace, Registration, TransportSession
from hyprial.kernel import canonical_user_uri, harden_private_path, ipc_errors, parse_user_uri

from hyprial.daemon.impl.adapters.user_delivery.actor  import UserDeliveryActor
from hyprial.kernel import AdmissionResult

_SENDER_DISPLAY_MAX_CHARS = 80


def display_sender(sender: str) -> str:
    """Render an attribution sender for a human-facing message line.

    ``sender`` arrives in an unauthenticated user-delivery frame, so it must
    not be able to fake message structure: control/format characters and
    line breaks are removed, whitespace collapsed and the length capped.  A
    well-formed identity is shown as is; anything that was changed or holds
    whitespace is quoted, so it cannot pass for the line around it.
    """

    cleaned = "".join(
        " " if unicodedata.category(char) in {"Zl", "Zp"} or char in "\r\n\t"
        else char
        for char in sender
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Co", "Cn"}
        or char in "\r\n\t"
    )
    cleaned = " ".join(cleaned.replace("「", "'").replace("」", "'").split())
    if len(cleaned) > _SENDER_DISPLAY_MAX_CHARS:
        cleaned = cleaned[: _SENDER_DISPLAY_MAX_CHARS - 1] + "…"
    if not cleaned:
        return "「未知发送方」"
    if cleaned != sender or " " in cleaned:
        return f"「{cleaned}」"
    return cleaned


NO_USER_AGENT_TARGET_MESSAGE = (
    "目标用户接收机上的 user agent cell 没有推送目标；消息未推送。请在其接收机上运行 "
    "hyprial agent cell show hyprial-user，为它加入 adapter 成员后用 hyprial agent cell apply 写回"
)

# A receiver only needs idempotency across realistic transport retries, not
# forever. Thirty days covers delayed/offline replay; the count cap bounds the
# full-file rewrite even when a busy receiver reaches that window first.
USER_DELIVERY_LEDGER_RETENTION_DAYS = 30
USER_DELIVERY_LEDGER_MAX_ENTRIES = 10_000


# Refusal codes that are only ever raised before anything is queued or sent.
# Builds before 0.5.1 stored the live-proxy refusal (message.send raising
# SENDER_UNRESOLVED for a bare service sender) without definitely_not_sent, so
# the flag alone cannot heal those rows: they replayed as duplicates and the
# watchdogs, whose durable batch keeps one idempotency key, retried the same
# key forever.  Codes that can follow a native send attempt
# stay recorded, so a retry can never double-send.
_PRE_SEND_REFUSAL_CODES = frozenset({
    ipc_errors.SENDER_UNRESOLVED,
    ipc_errors.AGENT_ADDRESS_RETIRED,
    ipc_errors.INVALID_ARGUMENT,
    ipc_errors.AMBIGUOUS_TARGET,
})


def _refused_before_send(value: dict[str, Any]) -> bool:
    if value.get("definitely_not_sent") is True:
        return True
    return (
        value.get("accepted") is not True
        and value.get("code") in _PRE_SEND_REFUSAL_CODES
    )
_USER_DELIVERY_LEDGER_RETENTION_MS = (
    USER_DELIVERY_LEDGER_RETENTION_DAYS * 24 * 60 * 60 * 1000
)


@dataclass(frozen=True, slots=True)
class UserDeliveryTarget:
    owner: str

    @property
    def uri(self) -> str:
        return canonical_user_uri(self.owner)

    @classmethod
    def parse(cls, value: str) -> UserDeliveryTarget:
        # The pure grammar lives in hyprial.uri (one reader); this class only
        # translates the rejection into its historical ValueError surface.
        owner = parse_user_uri(value)
        if owner is None:
            if not value.startswith("user:"):
                raise ValueError("user target must start with user:")
            raise ValueError("user target must contain a non-empty owner")
        return cls(owner)


def is_user_target(value: str) -> bool:
    return value.startswith("user:")


@dataclass(frozen=True, slots=True)
class UserDeliveryRequest:
    message_id: str
    idempotency_key: str
    owner: str
    sender: str
    message: str
    conversation_id: str
    attempt_id: str | None = None


@dataclass(frozen=True, slots=True)
class UserDeliveryResult:
    message_id: str
    accepted: bool
    native_message_id: str | None = None
    duplicate: bool = False
    code: str | None = None
    message: str | None = None
    definitely_not_sent: bool = False


@runtime_checkable
class UserDeliveryPort(Protocol):
    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult: ...


class UserDeliveryLedger:
    """Private persistent result ledger for receiver-side idempotency."""

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = USER_DELIVERY_LEDGER_MAX_ENTRIES,
        retention_ms: int = _USER_DELIVERY_LEDGER_RETENTION_MS,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if max_entries < 1 or retention_ms < 1:
            raise ValueError("user-delivery retention bounds must be positive")
        self.path = Path(path)
        self._lock = threading.RLock()
        self._max_entries = max_entries
        self._retention_ms = retention_ms
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def get(self, idempotency_key: str) -> UserDeliveryResult | None:
        with self._lock:
            raw = self._retained(self._load()).get(idempotency_key)
            return self._result(raw) if raw is not None else None

    def by_message_id(self, message_id: str) -> UserDeliveryResult | None:
        with self._lock:
            for raw in self._retained(self._load()).values():
                result = self._result(raw)
                if result.message_id == message_id:
                    return result
        return None

    def mark_duplicate(self, idempotency_key: str) -> UserDeliveryResult | None:
        with self._lock:
            loaded = self._load()
            values = self._retained(loaded)
            raw = values.get(idempotency_key)
            if raw is None:
                if values != loaded:
                    self._save(values)
                return None
            result = replace(self._result(raw), duplicate=True)
            values[idempotency_key] = {
                **asdict(result),
                "recordedAtMs": raw["recordedAtMs"],
            }
            self._save(values)
            return result

    def record(
        self, idempotency_key: str, result: UserDeliveryResult
    ) -> UserDeliveryResult:
        with self._lock:
            loaded = self._load()
            values = self._retained(loaded)
            if result.definitely_not_sent:
                if values != loaded:
                    self._save(values)
                return result
            existing = values.get(idempotency_key)
            if existing is not None:
                if values != loaded:
                    self._save(values)
                return self._result(existing)
            values[idempotency_key] = {
                **asdict(result),
                "recordedAtMs": self._clock_ms(),
            }
            values = self._retained(values)
            self._save(values)
            return result

    def _retained(
        self, values: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        now_ms = self._clock_ms()
        cutoff_ms = now_ms - self._retention_ms
        retained: dict[str, dict[str, Any]] = {}
        for key, value in values.items():
            if not isinstance(value, dict):
                raise TypeError("user-delivery ledger item must be an object")
            # A refusal with proof that nothing was queued is not an
            # idempotent outcome.  Drop legacy rows on every read path so an
            # existing production ledger heals without migration.
            if _refused_before_send(value):
                continue
            timestamp = value.get("recordedAtMs")
            if not isinstance(timestamp, int) or isinstance(timestamp, bool):
                # Schema-v1 rows written before bounded retention have no
                # timestamp. Give them one full window on their next rewrite.
                timestamp = now_ms
            if timestamp >= cutoff_ms:
                retained[key] = {**value, "recordedAtMs": timestamp}
        excess = len(retained) - self._max_entries
        if excess > 0:
            oldest = sorted(
                retained,
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
            raise ValueError("unsupported user-delivery ledger schema")
        values = raw.get("deliveries")
        if not isinstance(values, dict):
            raise TypeError("user-delivery ledger deliveries must be an object")
        return values

    def _save(self, values: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {"schemaVersion": 1, "deliveries": values},
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            harden_private_path(temporary, mode=0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _result(value: object) -> UserDeliveryResult:
        if not isinstance(value, dict):
            raise TypeError("user-delivery ledger item must be an object")
        return UserDeliveryResult(
            message_id=str(value["message_id"]),
            accepted=value.get("accepted") is True,
            native_message_id=_optional_string(value.get("native_message_id")),
            duplicate=value.get("duplicate") is True,
            code=_optional_string(value.get("code")),
            message=_optional_string(value.get("message")),
            definitely_not_sent=value.get("definitely_not_sent") is True,
        )


class ReceiverUserDelivery:
    """Hand an owned person's delivery to this node's owner push (the user agent cell)."""

    def __init__(
        self,
        *,
        node_id: str,
        ledger: UserDeliveryLedger,
        owns: Callable[[str], bool],
        owner_push: (
            Callable[[UserDeliveryRequest], UserDeliveryResult | None] | None
        ) = None,
        logger: Callable[..., None] | None = None,
    ) -> None:
        self.node_id = node_id
        self._owns = owns
        self.ledger = ledger
        self.owner_push = owner_push
        self.logger = logger
        self._lock = threading.RLock()

    def owns(self, owner: str) -> bool:
        return self._owns(owner)

    def handle(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        with self._lock:
            return self._handle_owned(request)

    def _handle_owned(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        result = self._deliver_unrecorded(request)
        return self.ledger.record(request.idempotency_key, result)

    def _deliver_unrecorded(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        """Stateless flow used after UserDeliveryActor claims the idempotency key."""

        previous = self.ledger.get(request.idempotency_key)
        if previous is not None:
            duplicate = self.ledger.mark_duplicate(request.idempotency_key)
            assert duplicate is not None
            return duplicate
        pushed = (
            self.owner_push(request)
            if self.owner_push is not None and self.owns(request.owner)
            else None
        )
        if pushed is None:
            return self._refusal(
                request, code=ipc_errors.TARGET_UNRESOLVED, message=NO_USER_AGENT_TARGET_MESSAGE
            )
        self._log_path("user-delivery.proxy", request, accepted=pushed.accepted)
        return pushed

    def _log_path(
        self, event: str, request: UserDeliveryRequest, **fields: object
    ) -> None:
        if self.logger is None:
            return
        self.logger(
            "info",
            event,
            messageId=request.message_id,
            conversationId=request.conversation_id,
            sender=request.sender,
            owner=request.owner,
            **fields,
        )

    def _refusal(
        self, request: UserDeliveryRequest, *, code: str, message: str,
        definitely_not_sent: bool = True,
    ) -> UserDeliveryResult:
        return UserDeliveryResult(
            message_id=request.message_id,
            accepted=False,
            code=code,
            message=message,
            definitely_not_sent=definitely_not_sent,
        )


def encode_user_delivery(request: UserDeliveryRequest) -> bytes:
    return json.dumps(
        {"schema": "hyprial-user-delivery/v1", **asdict(request)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def decode_user_delivery(payload: bytes) -> UserDeliveryRequest:
    raw = json.loads(payload)
    if not isinstance(raw, dict) or raw.get("schema") != "hyprial-user-delivery/v1":
        raise ValueError("unsupported user-delivery frame schema")
    return UserDeliveryRequest(
        message_id=_string(raw.get("message_id"), "message_id"),
        idempotency_key=_string(raw.get("idempotency_key"), "idempotency_key"),
        owner=_string(raw.get("owner"), "owner"),
        sender=_string(raw.get("sender"), "sender"),
        message=_string(raw.get("message"), "message"),
        conversation_id=_string(raw.get("conversation_id"), "conversation_id"),
        attempt_id=_optional_string(raw.get("attempt_id")),
    )


def encode_user_result(result: UserDeliveryResult) -> bytes:
    return json.dumps(
        {"schema": "hyprial-user-delivery-result/v1", **asdict(result)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def decode_user_result(payload: bytes) -> UserDeliveryResult:
    raw = json.loads(payload)
    if not isinstance(raw, dict) or raw.get("schema") != "hyprial-user-delivery-result/v1":
        raise ValueError("unsupported user-delivery result schema")
    return UserDeliveryResult(
        message_id=_string(raw.get("message_id"), "message_id"),
        accepted=raw.get("accepted") is True,
        native_message_id=_optional_string(raw.get("native_message_id")),
        duplicate=raw.get("duplicate") is True,
        code=_optional_string(raw.get("code")),
        message=_optional_string(raw.get("message")),
        definitely_not_sent=raw.get("definitely_not_sent") is True,
    )


class ZenohUserDeliveryEndpoint:
    """Receiver daemon endpoint; non-owning daemons ignore the delivery."""

    def __init__(
        self,
        session: TransportSession,
        receiver: ReceiverUserDelivery,
        *,
        keys: KeySpace | None = None,
        actor_host=None,
    ) -> None:
        self._keys = keys or KeySpace()
        self._receiver = receiver
        self._authority = UserDeliveryActor(receiver, actor_host=actor_host)
        self._registrations: list[Registration] = []
        try:
            # Expose ingress last: every accepted request needs an observable
            # receipt and an owner whose shutdown the application can reach.
            self._registrations.append(session.declare_queryable(
                self._keys.user_receipt_any(), self._receipt
            ))
            self._registrations.append(session.subscribe(
                self._keys.user_delivery_any(), self._on_delivery
            ))
        except BaseException as error:
            for registration in reversed(self._registrations):
                try:
                    registration.close()
                except Exception as cleanup_error:
                    error.add_note(f"user delivery registration cleanup: {type(cleanup_error).__name__}")
            if not self._authority.close():
                error.add_note("user delivery accepted effects still draining")
            raise

    def _on_delivery(self, sample: object) -> None:
        request = decode_user_delivery(sample.payload)
        admission = self._authority.admit(request)
        if admission is not AdmissionResult.ACCEPTED:
            raise RuntimeError(f"user delivery ingress rejected: {admission.value}")

    def _receipt(self, selector: str) -> bytes | None:
        key = selector.split("?", 1)[0]
        attempt_id = self._keys.decode_identity(key.rsplit("/", 1)[-1])
        result = self._authority.receipt(attempt_id)
        return encode_user_result(result) if result is not None else None

    def close(self) -> None:
        for registration in reversed(self._registrations):
            registration.close()
        if not self._authority.close():
            raise TimeoutError("user delivery effects still hold accepted requests")


class ZenohUserDeliveryTransport:
    """Sender daemon transport waiting for the receiver daemon's true result."""

    def __init__(
        self,
        session: TransportSession,
        *,
        keys: KeySpace | None = None,
        receipt_timeout: float = 3.0,
        logger: Any | None = None,
    ) -> None:
        self._session = session
        self._keys = keys or KeySpace()
        self._receipt_timeout = receipt_timeout
        self._logger = logger

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        # Events carry identifiers and outcomes only: never the message body
        # (it may quote privileged content) and never any credential material.
        self._safe_log(
            "info",
            "user-delivery.attempted",
            messageId=request.message_id,
            owner=request.owner,
            sender=request.sender,
            conversationId=request.conversation_id,
        )
        attempt_id = uuid4().hex
        outgoing = replace(request, attempt_id=attempt_id)
        self._session.put(
            self._keys.user_delivery(request.owner, request.message_id),
            encode_user_delivery(outgoing),
        )
        receipt_key = self._keys.user_receipt(request.sender, attempt_id)
        deadline = time.monotonic() + self._receipt_timeout
        while time.monotonic() < deadline:
            timeout = min(0.2, max(0.01, deadline - time.monotonic()))
            for sample in self._session.get(receipt_key, timeout=timeout):
                result = decode_user_result(sample.payload)
                self._safe_log(
                    "info" if result.accepted else "error",
                    "user-delivery.settled",
                    messageId=request.message_id,
                    owner=request.owner,
                    sender=request.sender,
                    accepted=result.accepted,
                    code=result.code,
                )
                return result
        result = UserDeliveryResult(
            message_id=request.message_id,
            accepted=False,
            code=ipc_errors.USER_DELIVERY_TIMEOUT,
            message="timeout waiting for the receiver's receipt",
        )
        self._safe_log(
            "error",
            "user-delivery.settled",
            messageId=request.message_id,
            owner=request.owner,
            sender=request.sender,
            accepted=False,
            code=result.code,
        )
        return result

    def _safe_log(self, level: str, event: str, **fields: object) -> None:
        if self._logger is None:
            return
        try:
            log = getattr(self._logger, "log", None)
            if callable(log):
                log(level, event, **fields)
            else:
                self._logger(level, event, **fields)
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - logging must never break delivery
            pass


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value, "optional value")
