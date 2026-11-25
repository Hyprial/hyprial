from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from hyprial.daemon.impl.adapters.contracts import (
    AdapterCloseOutcome,
    AdapterHealth,
    AdapterInstanceRef,
    AuthorizedChatTarget,
    CustodyReceipt,
    IngressObservation,
    IngressUnit,
    HydratedEvent,
    IngressSink,
    NativeCheckpoint,
    NativeSubmissionOutcome,
    NativeTargetResolution,
    OperationNode,
    PlatformEventEnvelope,
    RuntimeSession,
    VerifiedCallbackRequest,
    canonical_json_v1,
    freeze_json_v1,
)

from .codec import decode_weixin_event, sanitize_weixin_payload
from .errors import WeixinProtocolError, WeixinRequestNotSentError


@dataclass(frozen=True, slots=True)
class WeixinCredentials:
    instance_id: str
    organization_id: str
    generation: int
    account_id: str
    token: str
    api_base_url: str

    def __post_init__(self) -> None:
        from .codec import normalize_weixin_api_base_url

        if not all(
            isinstance(value, str) and value
            for value in (
                self.instance_id,
                self.organization_id,
                self.account_id,
                self.token,
                self.api_base_url,
            )
        ):
            raise WeixinProtocolError("missing-credentials")
        if self.generation < 1:
            raise WeixinProtocolError("invalid-generation")
        object.__setattr__(self, "api_base_url", normalize_weixin_api_base_url(self.api_base_url))

    def __repr__(self) -> str:
        return (
            "WeixinCredentials(instance_id={!r}, organization_id={!r}, generation={!r}, "
            "account_id={!r}, token=<redacted>, api_base_url={!r})"
        ).format(
            self.instance_id,
            self.organization_id,
            self.generation,
            self.account_id,
            self.api_base_url,
        )


class WeixinTransport(Protocol):
    async def notify_start(self, credentials: WeixinCredentials) -> None: ...

    async def notify_stop(self, credentials: WeixinCredentials) -> None: ...

    async def get_updates(
        self, credentials: WeixinCredentials, cursor: str, *, limit: int
    ) -> Mapping[str, object]: ...

    async def send_message(
        self, credentials: WeixinCredentials, request: Mapping[str, object]
    ) -> Mapping[str, object]: ...


@dataclass(slots=True)
class _Pending:
    digest: str
    page_cursor: str
    index: int
    total: int
    committed: bool = False


class WeixinRuntime:
    def __init__(
        self,
        credentials: WeixinCredentials,
        transport: WeixinTransport,
        *,
        initial_cursor: str = "",
        now_ms: object | None = None,
        page_limit: int = 500,
    ) -> None:
        if not isinstance(credentials, WeixinCredentials):
            raise TypeError("WeixinRuntime requires receiver-owned Weixin credentials")
        self._credentials = credentials
        self._transport = transport
        self._cursor = initial_cursor
        self._now_ms = now_ms if callable(now_ms) else lambda: int(time.time() * 1000)
        self._page_limit = min(max(page_limit, 1), 500)
        self._instance: AdapterInstanceRef | None = None
        self._sink: object | None = None
        self._buffer: list[tuple[dict[str, object], str, int, int]] = []
        self._pending: dict[str, _Pending] = {}
        self._history: list[IngressUnit] = []
        self._failures = 0
        self._state = "configured"

    @property
    def committed_cursor(self) -> str:
        return self._cursor

    def _fence(self, instance: AdapterInstanceRef) -> None:
        if instance.kind != "weixin" or instance.variant != "personal":
            raise WeixinProtocolError("variant-mismatch")
        if instance.instance_id != self._credentials.instance_id:
            raise WeixinProtocolError("instance-mismatch")
        if instance.organization_id != self._credentials.organization_id:
            raise WeixinProtocolError("organization-mismatch")
        if instance.generation != self._credentials.generation:
            raise WeixinProtocolError("generation-mismatch")

    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession:
        self._fence(instance)
        self._state = "starting"
        await self._transport.notify_start(self._credentials)
        self._instance = instance
        self._sink = sink
        self._state = "online"
        return RuntimeSession(
            f"weixin:{instance.instance_id}:{instance.generation}",
            instance.instance_id,
            instance.generation,
            "pull",
        )

    async def _load_page(self) -> None:
        if self._instance is None:
            raise WeixinProtocolError("runtime-not-started")
        try:
            response = dict(
                await self._transport.get_updates(
                    self._credentials, self._cursor, limit=self._page_limit
                )
            )
        except Exception:
            self._failures += 1
            self._state = "blocked" if self._failures >= 3 else "reconnecting"
            raise
        code = response.get("ret", response.get("errcode", 0))
        if code not in (0, "0", None):
            self._failures += 1
            if str(code) == "-14" or self._failures >= 3:
                self._state = "blocked"
            raise WeixinProtocolError(f"updates-rejected:{code}")
        self._failures = 0
        self._state = "online"
        cursor = response.get("get_updates_buf", self._cursor)
        if not isinstance(cursor, str):
            raise WeixinProtocolError("invalid-cursor")
        messages = response.get("msgs", [])
        if not isinstance(messages, list) or len(messages) > self._page_limit:
            raise WeixinProtocolError("invalid-update-page")
        copied = [dict(message) for message in messages if isinstance(message, Mapping)]

        def order(item: tuple[int, dict[str, object]]) -> tuple[int, int]:
            index, message = item
            raw = message.get("seq", message.get("create_time_ms"))
            try:
                return int(raw), index
            except (TypeError, ValueError):
                return index, index

        copied = [message for _, message in sorted(enumerate(copied), key=order)]
        self._buffer.extend(
            (message, cursor, index, len(copied))
            for index, message in enumerate(copied)
        )

    async def next_ingress(self) -> IngressUnit:
        if not self._buffer:
            await self._load_page()
        if not self._buffer:
            raise WeixinProtocolError("empty-update-page")
        message, page_cursor, index, total = self._buffer.pop(0)
        assert self._instance is not None
        decoded = decode_weixin_event(
            self._instance,
            message,
            raw_source_ref="sealed-weixin:" + hashlib.sha256(
                json.dumps(message, sort_keys=True, default=str).encode()
            ).hexdigest(),
        )
        frozen = freeze_json_v1(sanitize_weixin_payload(message))
        digest = hashlib.sha256(
            json.dumps(message, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        observation = IngressObservation(
            f"weixin-observation:{uuid.uuid4().hex}",
            decoded.aliases,
            "poll",
            decoded.provenance.unknown.raw_source_ref,
            digest,
            self._now_ms(),
        )
        token = f"weixin-checkpoint:{uuid.uuid4().hex}"
        self._pending[token] = _Pending(digest, page_cursor, index, total)
        unit = IngressUnit(
            PlatformEventEnvelope(observation, frozen),
            NativeCheckpoint("poll-cursor", token, None),
            False,
        )
        self._history.append(unit)
        return unit

    async def accept_callback(self, request: VerifiedCallbackRequest) -> IngressUnit:
        raise WeixinProtocolError("callback-unavailable-for-pull-runtime")

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        if not isinstance(envelope, PlatformEventEnvelope):
            raise WeixinProtocolError("event-envelope-required")
        return HydratedEvent(envelope, envelope.payload)

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise WeixinProtocolError("invalid-history-limit")
        start = 0
        if after is not None:
            for index, unit in enumerate(self._history):
                if unit.checkpoint == after:
                    start = index + 1
                    break
            else:
                raise WeixinProtocolError("history-checkpoint-unknown")
        return tuple(self._history[start : start + limit])

    async def resolve(self, target: AuthorizedChatTarget) -> NativeTargetResolution:
        if self._instance is None:
            raise WeixinProtocolError("runtime-not-started")
        if target.chat_ref.instance != self._instance:
            return NativeTargetResolution(
                target, "unauthorized", None, None, "weixin-target-instance-mismatch"
            )
        if target.chat_ref.kind == "thread":
            return NativeTargetResolution(
                target, "unsupported", None, None, "weixin-thread-unsupported"
            )
        return NativeTargetResolution(
            target,
            "resolved",
            target.chat_ref.native_chat_id,
            target.chat_ref.native_thread_id,
            None,
        )

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        pending = self._pending.get(checkpoint.token or "")
        if pending is None:
            raise WeixinProtocolError("unknown-checkpoint")
        if custody.envelope_digest != pending.digest:
            raise WeixinProtocolError("custody-digest-mismatch")
        if pending.committed:
            return
        pending.committed = True
        page = [item for item in self._pending.values() if item.page_cursor == pending.page_cursor]
        if len(page) == pending.total and all(item.committed for item in page):
            self._cursor = pending.page_cursor
            for token, item in tuple(self._pending.items()):
                if item.page_cursor == pending.page_cursor:
                    del self._pending[token]

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            self._state,
            "weixin-ilink/2.4.6",
            len(self._buffer) + len(self._pending),
            0,
            (() if self._state == "online" else (f"weixin-{self._state}",)),
        )

    async def submit(self, operation: OperationNode) -> NativeSubmissionOutcome:
        from .codec import classify_weixin_submission

        if self._instance is None:
            raise WeixinProtocolError("runtime-not-started")
        if operation.sealed_request is None:
            raise WeixinProtocolError("sealed-request-required")
        request = json.loads(canonical_json_v1(operation.sealed_request))
        if not isinstance(request, dict):
            raise WeixinProtocolError("sealed-request-invalid")
        if request.get("instanceId") != self._instance.instance_id:
            raise WeixinProtocolError("instance-mismatch")
        if request.get("generation") != self._instance.generation:
            raise WeixinProtocolError("generation-mismatch")
        if request.get("organizationId") != self._instance.organization_id:
            raise WeixinProtocolError("organization-mismatch")
        request["idempotencyKey"] = operation.idempotency_key
        try:
            result = await self._transport.send_message(self._credentials, request)
        except WeixinRequestNotSentError as exc:
            submission = classify_weixin_submission(exc, operation_started=False)
        except Exception as exc:
            submission = classify_weixin_submission(exc, operation_started=True)
        else:
            submission = classify_weixin_submission(result, operation_started=True)
        return NativeSubmissionOutcome((submission,), None)

    async def reconcile(
        self, operation: OperationNode, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome:
        # iLink exposes no strong query by client id in the reviewed protocol.
        # Unknown custody therefore remains unknown instead of being resent.
        if any(item.state == "unknown" for item in prior.submissions):
            return prior
        return prior

    async def reconnect(self, reason: str) -> None:
        if self._instance is None:
            raise WeixinProtocolError("runtime-not-started")
        if not isinstance(reason, str) or not reason:
            raise WeixinProtocolError("reconnect-reason-required")
        self._state = "reconnecting"
        await self._transport.notify_stop(self._credentials)
        await self._transport.notify_start(self._credentials)
        self._state = "online"

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        if self._instance is not None:
            await self._transport.notify_stop(self._credentials)
        self._state = "stopped"
        retained = len(self._buffer) + len(self._pending)
        return AdapterCloseOutcome(
            "closed" if retained == 0 else "retained-pending",
            retained,
            0,
            None if retained == 0 else "weixin-ingress-pending",
        )


__all__ = ["WeixinCredentials", "WeixinRuntime", "WeixinTransport"]
