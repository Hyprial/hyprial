"""Receiver-owned ``user:`` routing over Zenoh and a Squire adapter."""

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
from hyprial.kernel import ipc_errors
from hyprial.kernel import canonical_user_uri, parse_user_uri

from hyprial.identity  import UserProfileStore
from hyprial.daemon.impl.squire.actors.delivery  import UserDeliveryActor
from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.adapters.lark.outbound.gateway import GatewayIoAuthority

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


UNCONFIGURED_SQUIRE_MESSAGE = (
    "目标用户尚未配置侍从；请在其接收机上运行 hyprial squire setup 完成 "
    "--channel 与 --owner-open-id 绑定（详见 docs/reference/p4-squire.md），或改用协调者转达"
)
UNAVAILABLE_ADAPTER_MESSAGE = (
    "目标用户的侍从 adapter 当前不可用；请确认接收机 channels.json 配置了对应 "
    "adapter 且 secret 存在（hyprial adapter start <name> 启动入站，出站注册在 daemon "
    "加载 profile 时自动完成），或改用协调者转达"
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
# (TARGET_SQUIRE_ADAPTER_UNAVAILABLE, outcome-unknown, PROXY_SEND_REFUSED)
# stay recorded, so a retry can never double-send.
_PRE_SEND_REFUSAL_CODES = frozenset({
    ipc_errors.SENDER_UNRESOLVED,
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
class OwnerDmAdapter(Protocol):
    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str: ...


@runtime_checkable
class UserDeliveryPort(Protocol):
    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult: ...


class UserAdapterRegistry:
    """Runtime map from profile adapter URI to its receiving user's adapter."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._adapters: dict[str, OwnerDmAdapter] = {}
        self._retired: dict[int, GatewayIoAuthority] = {}
        self._closed = False

    def register(self, adapter_uri: str, adapter: OwnerDmAdapter) -> None:
        if not adapter_uri:
            raise ValueError("adapter URI must not be empty")
        if not isinstance(adapter, OwnerDmAdapter):
            raise TypeError("adapter must implement send_owner_dm")
        self._drain_retired(0.0)
        with self._lock:
            previous = self._adapters.get(adapter_uri)
            already_owned = any(value is adapter for value in self._adapters.values())
            retires_previous = (
                isinstance(previous, GatewayIoAuthority)
                and previous is not adapter
                and not any(
                    key != adapter_uri and value is previous
                    for key, value in self._adapters.items()
                )
            )
            rejected = self._closed or (retires_previous and len(self._retired) >= 128)
            if not rejected:
                self._adapters[adapter_uri] = adapter
                if isinstance(previous, GatewayIoAuthority) and not any(
                    value is previous for value in self._adapters.values()
                ):
                    self._retired[id(previous)] = previous
        if rejected:
            if isinstance(adapter, GatewayIoAuthority) and not already_owned:
                adapter.close(0.0)
            raise RuntimeError(
                "user adapter registry closed or retirement capacity exhausted"
            )
        self._drain_retired(0.0)

    def unregister(self, adapter_uri: str) -> None:
        with self._lock:
            previous = self._adapters.pop(adapter_uri, None)
            if isinstance(previous, GatewayIoAuthority) and not any(
                value is previous for value in self._adapters.values()
            ):
                self._retired[id(previous)] = previous
        self._drain_retired(0.0)

    def get(self, adapter_uri: str) -> OwnerDmAdapter | None:
        with self._lock:
            return self._adapters.get(adapter_uri)

    def _drain_retired(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            retired = tuple(self._retired.items())
        for token, owner in retired:
            if owner.close(max(0.0, deadline - time.monotonic())):
                with self._lock:
                    self._retired.pop(token, None)
        with self._lock:
            return not self._retired

    def close(self, timeout: float = 5.0) -> bool:
        with self._lock:
            self._closed = True
            for adapter in self._adapters.values():
                if isinstance(adapter, GatewayIoAuthority):
                    self._retired[id(adapter)] = adapter
            self._adapters.clear()
        return self._drain_retired(timeout)


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
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
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
    """Resolve only local preferred profiles, then invoke their own adapter."""

    def __init__(
        self,
        *,
        node_id: str,
        profiles: UserProfileStore,
        adapters: UserAdapterRegistry,
        ledger: UserDeliveryLedger,
        reload_adapters: Callable[[], None] | None = None,
        delivery_agent_delivery: (
            Callable[[str, UserDeliveryRequest], UserDeliveryResult | None] | None
        ) = None,
        logger: Callable[..., None] | None = None,
        identity_resolver: Any | None = None,
    ) -> None:
        self.node_id = node_id
        self.profiles = profiles
        self.adapters = adapters
        self.ledger = ledger
        self.reload_adapters = reload_adapters
        self.delivery_agent_delivery = delivery_agent_delivery
        self.logger = logger
        self.identity_resolver = identity_resolver
        self._lock = threading.RLock()

    def owns(self, owner: str) -> bool:
        # `resolve`, not `get_by_owner`: callers hold either vocabulary -- an
        # owner key from `resolve_node_owner()`, or an owner typed by a human.
        resolve = getattr(self.profiles, "resolve_current", self.profiles.resolve)
        profile = resolve(owner)
        return (
            profile is not None
            and profile.preferred_receiver.machine == self.node_id
        )

    def handle(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        with self._lock:
            return self._handle_owned(request)

    def _handle_owned(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        result = self._deliver_unrecorded(request)
        return self.ledger.record(request.idempotency_key, result)

    def _deliver_unrecorded(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        """Stateless flow used after UserDeliveryActor claims the idempotency key.

        The composed receiver holds actor ports for the ledger and native SDK;
        independent requests do not share this compatibility receiver lock.
        """
        previous = self.ledger.get(request.idempotency_key)
        if previous is not None:
            duplicate = self.ledger.mark_duplicate(request.idempotency_key)
            assert duplicate is not None
            return duplicate

        profile = self.profiles.resolve(request.owner)
        if profile is None or profile.preferred_receiver.machine != self.node_id:
            return self._refusal(
                request,
                code=ipc_errors.TARGET_SQUIRE_UNCONFIGURED,
                message=UNCONFIGURED_SQUIRE_MESSAGE,
            )
        fallback_reason = "not-configured"
        if profile.delivery_agent is not None:
            fallback_reason = "not-live"
            if self.delivery_agent_delivery is not None:
                proxy_result = self.delivery_agent_delivery(
                    profile.delivery_agent, request
                )
                if proxy_result is not None:
                    self._log_path(
                        "user-delivery.proxy",
                        request,
                        deliveryAgent=profile.delivery_agent,
                        accepted=proxy_result.accepted,
                    )
                    return proxy_result
        adapter_uri = profile.squire_adapter
        from hyprial.daemon.impl.identity import (
            IdentityResolver,
            IdentityResolverError,
        )

        try:
            open_id = (
                self.identity_resolver.profile_open_id(profile, adapter_uri)
                if self.identity_resolver is not None and adapter_uri is not None
                else IdentityResolver._legacy_profile_open_id(profile, adapter_uri)
                if adapter_uri is not None
                else None
            )
        except IdentityResolverError as error:
            # A conflicted or unreadable owner binding is a refusal before any
            # send, never an exception out of the delivery path.
            self._log_path("user-delivery.owner-account-unresolved", request, code=error.code)
            return self._refusal(
                request,
                code=error.code,
                message=f"the owner's squire account cannot be resolved: {error}",
            )
        if adapter_uri is None or open_id is None:
            return self._refusal(
                request,
                code=ipc_errors.TARGET_SQUIRE_UNCONFIGURED,
                message=UNCONFIGURED_SQUIRE_MESSAGE,
            )
        adapter = self.adapters.get(adapter_uri)
        if adapter is None and self.reload_adapters is not None:
            # Profiles and channels.json may have been completed (for
            # example by `hyprial squire setup`) after this daemon started.
            # Refresh the registry once before declaring the adapter
            # unavailable so a fresh install never needs a daemon restart
            # to gain outbound delivery.
            self.reload_adapters()
            adapter = self.adapters.get(adapter_uri)
        if adapter is None:
            return self._refusal(
                request,
                code=ipc_errors.TARGET_SQUIRE_ADAPTER_UNAVAILABLE,
                message=UNAVAILABLE_ADAPTER_MESSAGE,
            )
        rendered = (
            f"来自我的侍从，转述自 {display_sender(request.sender)}：\n\n{request.message}"
        )
        self._log_path(
            "user-delivery.squire",
            request,
            fallbackReason=fallback_reason,
            adapter=adapter_uri,
        )
        try:
            send = getattr(adapter, "send_owner_dm_settled", adapter.send_owner_dm)
            native_message_id = send(
                open_id,
                rendered,
                idempotency_key=request.idempotency_key,
            )
        except Exception as error:  # noqa: BLE001 - adapter failure boundary
            return self._refusal(
                request,
                code=ipc_errors.TARGET_SQUIRE_ADAPTER_UNAVAILABLE,
                message=f"{UNAVAILABLE_ADAPTER_MESSAGE} ({error})",
                definitely_not_sent=False,
            )
        result = UserDeliveryResult(
            message_id=request.message_id,
            accepted=True,
            native_message_id=native_message_id,
        )
        return result

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
    ) -> None:
        self._keys = keys or KeySpace()
        self._receiver = receiver
        self._authority = UserDeliveryActor(receiver)
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
            message="timeout waiting for the receiver's squire receipt",
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
