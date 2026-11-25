"""QQ official-bot runtime and onboarding over the native python wire.

The runtime is the only reconnect and heartbeat owner; the transport layer has
no loops of its own. Sends are passive-only: every outbound effect must quote
an authoritative inbound native msg_id allocated through the durable store.
"""

# hyprial-unbounded-wait-allow: get - awaited asyncio.Queue ingress is cancellable by its owning task
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed

from hyprial.daemon.impl.adapters.common.transport import (
    HttpEffectState,
    observe_connection_closed,
)
from hyprial.daemon.impl.adapters.contracts import (
    AdapterCloseOutcome,
    AdapterHealth,
    AdapterInstanceRef,
    AuthorizedChatTarget,
    CapabilityFactV1,
    CapabilitySnapshot,
    CustodyReceipt,
    HydratedEvent,
    IngressObservation,
    IngressSink,
    IngressUnit,
    NativeAlias,
    NativeCheckpoint,
    NativeSubmissionOutcome,
    NativeTargetResolution,
    OnboardingAttempt,
    OnboardingPreflight,
    OperationNode,
    PartSubmission,
    PlatformEventEnvelope,
    RuntimeSession,
    VerifiedCallbackRequest,
    VerifiedPlatformIdentity,
    canonical_json_v1,
    freeze_json_v1,
    frozen_json_digest_v1,
)

from .codec import CAPABILITY_REVISION, thaw_json
from .components import QQWireComponents
from .errors import (
    QQFatalGatewayClose,
    QQGatewayClosedClean,
    QQMsgSeqLimit,
    QQProtocolError,
)
from .media import QQMedia
from .transport import (
    DEFAULT_INTENTS,
    MAX_RECONNECT_ATTEMPTS,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
    RECONNECT_DELAYS_MS,
    QQCloseAction,
    close_action,
    message_native_id,
    message_path,
)

MAX_PENDING_SEQUENCES = 256
RETRYABLE_BIZ_CODE = 22009


@dataclass(frozen=True, slots=True)
class QQCredentials:
    app_id: str
    app_secret: str

    def __post_init__(self) -> None:
        if not self.app_id or not self.app_secret:
            raise ValueError("QQ AppID and AppSecret are required")
        if any(character.isspace() for character in self.app_id):
            raise ValueError("QQ AppID is invalid")


class QQCredentialResolver(Protocol):
    async def resolve(self, credential_ref: str) -> QQCredentials: ...


class QQCheckpointLedger(Protocol):
    async def load(self, instance_id: str, generation: int) -> str | None: ...

    async def commit(self, instance_id: str, generation: int, token: str) -> None: ...


def _map(value: object, name: str) -> Mapping[str, object]:
    thawed = thaw_json(value)
    if not isinstance(thawed, Mapping):
        raise ValueError(f"QQ {name} must be an object")
    return thawed


def _text(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"QQ {name} is required")
    result = str(value)
    if not result:
        raise ValueError(f"QQ {name} is required")
    return result


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"QQ {name} must be an integer >= {minimum}")
    return value


def _fingerprint(app_id: str, bot_id: str) -> str:
    return hashlib.sha256(f"qq\0qq-official\0{app_id}\0{bot_id}".encode()).hexdigest()


class QQOfficialBotRuntime:
    """Pull-mode gateway runtime with custody-fenced checkpoints."""

    def __init__(
        self,
        components: QQWireComponents,
        checkpoints: QQCheckpointLedger,
        credentials: QQCredentialResolver,
        receiver_ref: str,
    ) -> None:
        self._components = components
        self._checkpoints = checkpoints
        self._credentials = credentials
        self._receiver_ref = receiver_ref
        self._instance: AdapterInstanceRef | None = None
        self._credential: QQCredentials | None = None
        self._gateway_url: str | None = None
        self._session_id: str | None = None
        self._committed_seq: int | None = None
        self._last_seen_seq: int | None = None
        self._pending: dict[str, tuple[str, str | None]] = {}
        self._state = "configured"
        self._unknown_count = 0
        self._heartbeat_acked = True
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._ingress_queue: asyncio.Queue[IngressUnit | BaseException] = asyncio.Queue(
            MAX_PENDING_SEQUENCES
        )
        self._connection_generation = 0
        self._reconnect_attempts = 0
        self._media: QQMedia | None = None

    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession:
        if instance.kind != "qq" or instance.variant != "official-bot":
            raise ValueError("QQ official runtime requires qq/official-bot")
        self._state = "starting"
        credential = await self._credentials.resolve(self._receiver_ref)
        try:
            await self._components.token.access_token()
            identity = await self._components.rest.get_json("/users/@me")
        except QQProtocolError:
            self._state = "blocked"
            raise
        identity_body = identity.body or {}
        bot_id_raw = identity_body.get("id")
        if identity.observation.state is not HttpEffectState.ACCEPTED or not isinstance(
            bot_id_raw, str
        ) or not bot_id_raw:
            self._state = "blocked"
            raise ValueError("QQ official authentication failed")
        if _fingerprint(credential.app_id, bot_id_raw) != instance.external_fingerprint:
            self._state = "blocked"
            raise ValueError("QQ official identity does not match instance fingerprint")
        loopback = self._components.loopback
        if loopback is not None and loopback.gateway_url is not None:
            self._gateway_url = loopback.gateway_url
        else:
            gateway = await self._components.rest.get_json("/gateway")
            url = (gateway.body or {}).get("url")
            if gateway.observation.state is not HttpEffectState.ACCEPTED or not isinstance(url, str):
                self._state = "blocked"
                raise ValueError("QQ official gateway discovery failed")
            self._gateway_url = self._safe_gateway_url(url)
        committed = await self._checkpoints.load(instance.instance_id, instance.generation)
        self._restore_checkpoint(committed)
        self._instance = instance
        self._credential = credential
        self._media = QQMedia(
            rest=self._components.rest,
            authority=self._components.media_authority,
            store=self._components.store,
            instance=instance,
            app_id=credential.app_id,
        )
        self._components.token.start_background()
        await self._connect_and_handshake()
        self._start_receiver()
        self._state = "online"
        return RuntimeSession(
            hashlib.sha256(
                f"qq\0{instance.instance_id}\0{instance.generation}\0{credential.app_id}".encode()
            ).hexdigest(),
            instance.instance_id,
            instance.generation,
            "pull",
        )

    def _safe_gateway_url(self, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "wss" or parsed.username or parsed.password or not parsed.hostname:
            raise ValueError("QQ returned an unsafe gateway URL")
        if not (parsed.hostname == "api.sgroup.qq.com" or parsed.hostname.endswith(".qq.com")):
            raise ValueError("QQ gateway host mismatch")
        return value

    async def _connect_and_handshake(self) -> None:
        if self._gateway_url is None:
            raise QQProtocolError("qq-gateway-url-unavailable")
        await self._components.gateway.connect(self._gateway_url)
        await self._handshake()

    async def _handshake(self) -> None:
        gateway = self._components.gateway
        token = self._components.token
        hello = await gateway.recv_json()
        if hello.get("op") != OP_HELLO:
            raise QQProtocolError("qq-gateway-hello-missing")
        interval_ms = _integer(
            _map(hello.get("d"), "Hello data").get("heartbeat_interval"),
            "heartbeat interval",
            minimum=1,
        )
        self._heartbeat_acked = True
        authorization = f"QQBot {await token.access_token()}"
        if self._session_id is not None and self._committed_seq is not None:
            await gateway.send_json(
                {
                    "op": OP_RESUME,
                    "d": {
                        "token": authorization,
                        "session_id": self._session_id,
                        "seq": self._committed_seq,
                    },
                }
            )
        else:
            await gateway.send_json(
                {
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": authorization,
                        "intents": DEFAULT_INTENTS,
                        "shard": [0, 1],
                    },
                }
            )
        while True:
            frame = await gateway.recv_json()
            op = frame.get("op")
            if op == OP_HEARTBEAT_ACK:
                self._heartbeat_acked = True
                continue
            if op == OP_HEARTBEAT:
                await gateway.send_json({"op": OP_HEARTBEAT, "d": self._last_seen_seq})
                continue
            if op == OP_INVALID_SESSION:
                await self._invalidate_session()
                raise QQProtocolError("qq-gateway-invalid-session")
            if op == OP_DISPATCH and frame.get("t") in ("READY", "RESUMED"):
                if isinstance(frame.get("s"), int):
                    self._last_seen_seq = frame["s"]
                if frame.get("t") == "READY":
                    self._session_id = _text(
                        _map(frame.get("d"), "Ready data").get("session_id"), "session id"
                    )
                self._start_heartbeat(interval_ms / 1000)
                return

    async def next_ingress(self) -> IngressUnit:
        item = await self._ingress_queue.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def _receive_loop(self) -> None:
        while True:
            try:
                frame = await self._components.gateway.recv_json()
            except ConnectionClosed as error:
                try:
                    await self._handle_connection_closed(error)
                except BaseException as reconnect_error:
                    await self._publish_receiver_error(reconnect_error)
                    return
                continue
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                await self._publish_receiver_error(error)
                return
            op = frame.get("op")
            if op == OP_HEARTBEAT_ACK:
                self._heartbeat_acked = True
                continue
            if op == OP_HEARTBEAT:
                self._heartbeat_acked = False
                await self._components.gateway.send_json(
                    {"op": OP_HEARTBEAT, "d": self._last_seen_seq}
                )
                continue
            if op == OP_RECONNECT:
                try:
                    await self._local_reconnect(clear_session=False)
                except BaseException as error:
                    await self._publish_receiver_error(error)
                    return
                continue
            if op == OP_INVALID_SESSION:
                try:
                    resumable = frame.get("d") is True
                    await self._local_reconnect(clear_session=not resumable)
                except BaseException as error:
                    await self._publish_receiver_error(error)
                    return
                continue
            if op != OP_DISPATCH:
                continue
            if isinstance(frame.get("s"), int):
                self._last_seen_seq = frame["s"]
            if frame.get("t") not in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
                continue
            try:
                unit = self._ingress_unit(frame)
                self._ingress_queue.put_nowait(unit)
            except asyncio.QueueFull:
                self._state = "degraded"
                await self._publish_receiver_error(
                    RuntimeError("QQ pending sequence capacity reached")
                )
                return
            except BaseException as error:
                await self._publish_receiver_error(error)
                return

    def _ingress_unit(self, frame: Mapping[str, object]) -> IngressUnit:
        instance = self._require_instance()
        if len(self._pending) >= MAX_PENDING_SEQUENCES:
            raise RuntimeError("QQ pending sequence capacity reached")
        sequence = _integer(frame.get("s"), "gateway sequence")
        body = _map(frame.get("d"), "event body")
        event_id = _text(body.get("event_id") or frame.get("id"), "event id")
        message_id = _text(body.get("id"), "message id")
        if frame.get("t") == "GROUP_AT_MESSAGE_CREATE":
            chat_scope = _text(
                body.get("group_openid") or body.get("group_open_id"),
                "group open id",
            )
        else:
            author = _map(body.get("author"), "author")
            chat_scope = _text(
                author.get("user_openid")
                or author.get("member_openid")
                or author.get("id"),
                "author id",
            )
        self._components.store.record_inbound(
            instance_id=instance.instance_id,
            generation=instance.generation,
            msg_id=message_id,
            received_at_ms=self._components.now_ms(),
        )
        frozen = freeze_json_v1(frame)
        digest = frozen_json_digest_v1(frozen)
        aliases = (
            NativeAlias(
                instance.instance_id,
                instance.generation,
                self._credential.app_id if self._credential else "unavailable",
                "qq-event",
                "create",
                "event",
                event_id,
                "qq-official-v1",
                platform="qq",
                chat_scope=chat_scope,
            ),
            NativeAlias(
                instance.instance_id,
                instance.generation,
                self._credential.app_id if self._credential else "unavailable",
                "qq-message",
                "create",
                "message",
                message_id,
                "qq-official-v1",
                platform="qq",
                chat_scope=chat_scope,
            ),
        )
        token = str(sequence)
        self._pending[token] = (digest, self._session_id)
        observation = IngressObservation(
            f"observation:qq:{instance.instance_id}:{sequence}",
            aliases,
            "live",
            f"raw-source-ref:qq:{instance.instance_id}:{event_id}",
            digest,
            0,
        )
        return IngressUnit(
            PlatformEventEnvelope(observation, frozen),
            NativeCheckpoint("stream-sequence", token, None),
            False,
        )

    async def _publish_receiver_error(self, error: BaseException) -> None:
        self._state = "blocked" if isinstance(error, QQFatalGatewayClose) else "degraded"
        await self._ingress_queue.put(error)

    def _start_receiver(self) -> None:
        current = self._receiver_task
        if current is not None and not current.done():
            raise QQProtocolError("qq-gateway-receiver-already-running")
        self._receiver_task = asyncio.create_task(
            self._receive_loop(), name=f"qq-receiver-{self._components.gateway.generation}"
        )

    async def _cancel_receiver(self) -> None:
        task = self._receiver_task
        self._receiver_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _handle_connection_closed(self, error: ConnectionClosed) -> None:
        observation = observe_connection_closed(
            error, generation=self._components.gateway.generation
        )
        code = observation.rcvd_code
        action = close_action(code)
        if action.fatal:
            self._state = "blocked"
            raise QQFatalGatewayClose(action.reason)
        if not action.reconnect:
            if code == 1000:
                raise QQGatewayClosedClean(action.reason)
            raise QQProtocolError(action.reason)
        await self._apply_reconnect(action)

    async def _local_reconnect(self, *, clear_session: bool, from_heartbeat: bool = False) -> None:
        action = QQCloseAction(True, 0, clear_session, "none", False, "qq-local-reconnect")
        await self._apply_reconnect(action, cancel_heartbeat=not from_heartbeat)

    async def _apply_reconnect(
        self, action: QQCloseAction, *, cancel_heartbeat: bool = True
    ) -> None:
        if cancel_heartbeat:
            await self._cancel_heartbeat()
        await self._close_current_socket()
        if action.clear_session:
            await self._invalidate_session()
        if action.refresh == "credentials":
            await self._refetch_credentials()
        elif action.refresh == "token":
            self._components.token.invalidate()
        self._reconnect_attempts += 1
        if self._reconnect_attempts > MAX_RECONNECT_ATTEMPTS:
            self._state = "blocked"
            raise QQProtocolError("qq-reconnect-exhausted")
        delay_ms = action.delay_ms
        if delay_ms is None:
            delay_ms = RECONNECT_DELAYS_MS[
                min(self._reconnect_attempts - 1, len(RECONNECT_DELAYS_MS) - 1)
            ]
        if delay_ms:
            self._state = "reconnecting"
            await self._components.sleep(delay_ms / 1000)
        await self._connect_and_handshake()
        self._reconnect_attempts = 0
        self._state = "online"

    async def _refetch_credentials(self) -> None:
        credential = await self._credentials.resolve(self._receiver_ref)
        if self._credential is not None and credential.app_id != self._credential.app_id:
            self._state = "blocked"
            raise QQProtocolError("qq-credentials-changed")
        self._credential = credential
        self._components.token.invalidate()

    async def _close_current_socket(self) -> None:
        gateway = self._components.gateway
        deadline = asyncio.get_running_loop().time() + 30
        try:
            await gateway.close_socket(code=4000, reason="hyprial reconnect", timeout_at=deadline)
        except Exception:
            gateway.forget_socket()

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        instance = self._require_instance()
        if checkpoint.kind != "stream-sequence" or checkpoint.token is None:
            raise ValueError("QQ requires stream-sequence checkpoints")
        expected = self._pending.get(checkpoint.token)
        if expected is None or expected[0] != custody.envelope_digest:
            raise ValueError("QQ custody digest mismatch")
        if expected[1] != self._session_id:
            raise ValueError("QQ checkpoint belongs to a stale Gateway session")
        sequence = int(checkpoint.token)
        if self._committed_seq is not None and sequence <= self._committed_seq:
            raise ValueError("QQ sequence checkpoint must advance")
        await self._checkpoints.commit(
            instance.instance_id, instance.generation, self._checkpoint_value(sequence)
        )
        self._committed_seq = sequence
        del self._pending[checkpoint.token]

    async def accept_callback(self, request: VerifiedCallbackRequest) -> IngressUnit:
        raise ValueError("QQ official Gateway runtime does not accept callbacks")

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        return HydratedEvent(envelope, envelope.payload)

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("QQ history limit must be 1..100")
        return ()

    async def resolve(self, target: AuthorizedChatTarget) -> NativeTargetResolution:
        instance = self._require_instance()
        if target.operation == "send":
            return NativeTargetResolution(
                target, "unsupported", None, None, "qq-proactive-unsupported"
            )
        if target.chat_ref.instance != instance or target.chat_ref.account_scope != (
            self._credential.app_id if self._credential else None
        ):
            return NativeTargetResolution(target, "unauthorized", None, None, "qq-target-scope-mismatch")
        if target.chat_ref.kind not in ("dm", "group"):
            return NativeTargetResolution(target, "unsupported", None, None, "qq-thread-unsupported")
        return NativeTargetResolution(target, "resolved", target.chat_ref.native_chat_id, None, None)

    async def submit(self, operation: OperationNode) -> NativeSubmissionOutcome:
        request = self._validate_request(operation)
        instance = self._require_instance()
        action = request.get("action")
        if action not in ("send_message", "send_media"):
            raise ValueError("QQ action must be send_message or send_media")
        part_index = _integer(request.get("part_index", 0), "part index")
        now_ms = self._components.now_ms()
        msg_id = request.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            return self._final(part_index, "qq-msg-id-required")
        status = self._components.store.inbound_status(
            instance_id=instance.instance_id,
            generation=instance.generation,
            msg_id=msg_id,
            now_ms=now_ms,
        )
        if status == "unknown":
            return self._final(part_index, "qq-msg-id-unknown")
        if status == "stale":
            return self._final(part_index, "qq-msg-id-stale")
        try:
            msg_seq = self._components.store.claim_msg_seq(
                instance_id=instance.instance_id,
                generation=instance.generation,
                msg_id=msg_id,
                operation_id=operation.idempotency_key,
                now_ms=now_ms,
            )
        except QQMsgSeqLimit:
            return self._final(part_index, "qq-msg-limit")
        scene = request.get("scene")
        target_id = request.get("target_id")
        if scene not in ("c2c", "group") or not isinstance(target_id, str) or not target_id:
            raise ValueError("QQ scene and target are required")
        if action == "send_media":
            assert self._media is not None
            part, delay = await self._media.send_for_runtime(
                request,
                msg_seq=msg_seq,
                operation_id=operation.idempotency_key,
                now_ms=now_ms,
            )
            if part.state == "unknown":
                self._unknown_count += 1
            return NativeSubmissionOutcome((part,), delay)
        result = await self._components.rest.post_json(
            message_path(scene, target_id),
            {
                "content": str(request.get("content") or ""),
                "msg_type": 0,
                "msg_seq": msg_seq,
                "msg_id": msg_id,
            },
            effecting=True,
            parse_success=message_native_id,
        )
        part, delay, result_code = self._map_result(result, part_index)
        self._components.store.record_attempt(
            instance_id=instance.instance_id,
            generation=instance.generation,
            operation_id=operation.idempotency_key,
            msg_id=msg_id,
            msg_seq=msg_seq,
            attempted_at_ms=now_ms,
            result=result_code,
        )
        if part.state == "unknown":
            self._unknown_count += 1
        return NativeSubmissionOutcome((part,), delay)

    def _map_result(
        self, result: object, part_index: int
    ) -> tuple[PartSubmission, int | None, str]:
        observation = result.observation
        body = result.body or {}
        if observation.state is HttpEffectState.ACCEPTED:
            return (
                PartSubmission(part_index, "accepted", observation.native_id, False, None),
                None,
                f"accepted:{observation.native_id}",
            )
        if observation.state is HttpEffectState.UNKNOWN:
            return (
                PartSubmission(part_index, "unknown", None, False, "qq-submit-unknown"),
                None,
                "unknown",
            )
        if observation.state is HttpEffectState.RETRYABLE:
            return (
                PartSubmission(part_index, "failed-retryable", None, True, "qq-pre-send-failure"),
                None,
                "retryable",
            )
        biz = body.get("code", body.get("err_code"))
        if biz == RETRYABLE_BIZ_CODE:
            return (
                PartSubmission(part_index, "failed-retryable", None, True, "qq-biz-22009"),
                None,
                "rejected-22009",
            )
        if observation.status_code == 429:
            delay = body.get("retry_after_ms")
            delay_ms = (
                delay
                if isinstance(delay, int) and not isinstance(delay, bool) and delay >= 0
                else None
            )
            return (
                PartSubmission(part_index, "failed-retryable", None, True, "qq-rate-limited"),
                delay_ms,
                "rejected-429",
            )
        return (
            PartSubmission(
                part_index, "failed-final", None, True, f"qq-http-{observation.status_code}"
            ),
            None,
            "rejected",
        )

    def _final(self, part_index: int, reason: str) -> NativeSubmissionOutcome:
        return NativeSubmissionOutcome(
            (PartSubmission(part_index, "failed-final", None, True, reason),), None
        )

    async def reconcile(
        self, operation: OperationNode, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome:
        if any(part.state == "unknown" for part in prior.submissions):
            instance = self._require_instance()
            self._components.store.record_attempt(
                instance_id=instance.instance_id,
                generation=instance.generation,
                operation_id=operation.idempotency_key,
                msg_id="unknown",
                msg_seq=0,
                attempted_at_ms=self._components.now_ms(),
                result="reconcile-unknown-kept",
            )
            self._unknown_count = max(0, self._unknown_count)
        return prior

    def _validate_request(self, operation: OperationNode) -> Mapping[str, object]:
        instance = self._require_instance()
        if operation.sealed_request is None:
            raise ValueError("QQ operation requires a sealed request")
        request = _map(operation.sealed_request, "sealed request")
        expected = {
            "provider": "qq",
            "organization_id": instance.organization_id,
            "instance_id": instance.instance_id,
            "generation": instance.generation,
            "receiver_ref": self._receiver_ref,
        }
        for field, value in expected.items():
            if request.get(field) != value:
                raise ValueError(f"QQ {field.replace('_', ' ')} mismatch")
        return request

    async def reconnect(self, reason: str) -> None:
        if not reason:
            raise ValueError("QQ reconnect reason is required")
        self._state = "reconnecting"
        await self._apply_reconnect(
            QQCloseAction(True, 0, False, "none", False, "qq-manual-reconnect")
        )

    async def health(self) -> AdapterHealth:
        reasons: tuple[str, ...] = ()
        if not self._heartbeat_acked:
            reasons = ("qq-heartbeat-unacknowledged",)
        elif self._state != "online":
            reasons = (f"qq-{self._state}",)
        return AdapterHealth(
            "degraded" if not self._heartbeat_acked and self._state == "online" else self._state,
            CAPABILITY_REVISION,
            len(self._pending),
            self._unknown_count,
            reasons,
        )

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_ms / 1000
        await self._cancel_receiver()
        await self._cancel_heartbeat()
        await self._components.token.stop()
        uncertain = False
        try:
            outcome = await self._components.gateway.close_socket(
                code=1000, reason="hyprial shutdown", timeout_at=deadline
            )
            if outcome.state.value != "closed" and outcome.state.value != "stale-generation":
                uncertain = True
        except Exception:
            uncertain = True
        final = await self._components.owner.aclose(deadline)
        if final.state.value == "unknown":
            uncertain = True
        self._state = "stopped"
        if uncertain:
            return AdapterCloseOutcome(
                "unknown", len(self._pending), self._unknown_count, "qq-close-unknown"
            )
        if self._pending or self._unknown_count:
            return AdapterCloseOutcome(
                "retained-pending", len(self._pending), self._unknown_count, "qq-work-retained"
            )
        return AdapterCloseOutcome("closed", 0, 0, None)

    def _restore_checkpoint(self, value: str | None) -> None:
        self._committed_seq = None
        self._last_seen_seq = None
        self._session_id = None
        if value is None:
            return
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            self._committed_seq = int(value)
            return
        if isinstance(parsed, int) and not isinstance(parsed, bool) and parsed >= 0:
            self._committed_seq = parsed
            self._last_seen_seq = parsed
            return
        if not isinstance(parsed, dict) or parsed.get("v") != 1:
            raise ValueError("QQ checkpoint state is invalid")
        sequence = parsed.get("seq")
        session_id = parsed.get("session_id")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("QQ checkpoint sequence is invalid")
        self._committed_seq = sequence
        self._last_seen_seq = sequence
        if session_id is None:
            return
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("QQ checkpoint session is invalid")
        self._session_id = session_id

    def _checkpoint_value(self, sequence: int) -> str:
        if self._session_id is None:
            return str(sequence)
        return canonical_json_v1({"seq": sequence, "session_id": self._session_id, "v": 1}).decode()

    async def _invalidate_session(self) -> None:
        self._session_id = None
        if self._instance is None or self._committed_seq is None:
            return
        tombstone = canonical_json_v1({"seq": self._committed_seq, "session_id": None, "v": 1}).decode()
        await self._checkpoints.commit(
            self._instance.instance_id, self._instance.generation, tombstone
        )

    def _start_heartbeat(self, interval_seconds: float) -> None:
        self._connection_generation += 1
        generation = self._connection_generation
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(generation, interval_seconds),
            name=f"qq-heartbeat-{generation}",
        )

    async def _heartbeat_loop(self, generation: int, interval_seconds: float) -> None:
        try:
            while generation == self._connection_generation:
                await asyncio.sleep(interval_seconds)
                if generation != self._connection_generation:
                    return
                if not self._heartbeat_acked:
                    self._connection_generation += 1
                    self._state = "reconnecting"
                    await self._cancel_receiver()
                    await self._local_reconnect(clear_session=False, from_heartbeat=True)
                    self._start_receiver()
                    return
                self._heartbeat_acked = False
                await self._components.gateway.send_json(
                    {"op": OP_HEARTBEAT, "d": self._last_seen_seq}
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            if generation == self._connection_generation:
                self._state = "degraded"

    async def _cancel_heartbeat(self) -> None:
        self._connection_generation += 1
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _require_instance(self) -> AdapterInstanceRef:
        if self._instance is None:
            raise RuntimeError("QQ runtime has not started")
        return self._instance


class QQOfficialBotOnboarding:
    def __init__(
        self,
        components: QQWireComponents,
        credentials: QQCredentialResolver,
        organization_id: str,
        instance_id: str,
        local_name: str,
    ) -> None:
        self._components = components
        self._credentials = credentials
        self._organization_id = organization_id
        self._instance_id = instance_id
        self._local_name = local_name

    async def inspect(self, config: object) -> OnboardingPreflight:
        value = _map(config, "onboarding config")
        if value.get("variant") == "qr":
            return OnboardingPreflight("unsupported", ("qq-qr-license-unavailable",))
        if value.get("variant", "official-bot") != "official-bot":
            return OnboardingPreflight("unsupported", ("qq-variant-unsupported",))
        if not isinstance(value.get("credential_ref"), str):
            return OnboardingPreflight("blocked", ("qq-credential-ref-required",))
        return OnboardingPreflight("ready", ())

    async def authenticate(self, attempt: OnboardingAttempt) -> VerifiedPlatformIdentity:
        credential = await self._credentials.resolve(attempt.credential_ref)
        await self._components.token.access_token()
        identity = await self._components.rest.get_json("/users/@me")
        body = identity.body or {}
        bot_id = body.get("id")
        if identity.observation.state is not HttpEffectState.ACCEPTED or not isinstance(
            bot_id, str
        ) or not bot_id:
            raise ValueError("QQ authentication failed")
        return VerifiedPlatformIdentity(
            credential.app_id, bot_id, f"qq-official:{credential.app_id}:{bot_id}"
        )

    async def fingerprint(self, identity: VerifiedPlatformIdentity) -> AdapterInstanceRef:
        return AdapterInstanceRef(
            self._instance_id,
            "qq",
            "official-bot",
            self._local_name,
            self._organization_id,
            _fingerprint(identity.account_scope, identity.stable_account_id),
            1,
        )

    async def probe_capabilities(self, identity: VerifiedPlatformIdentity) -> CapabilitySnapshot:
        observed_at_ms = time.time_ns() // 1_000_000
        facts = tuple(
            sorted(
                (
                    CapabilityFactV1(
                        "files", "native", "unknown", {}, "qq-live-upload-not-proven"
                    ),
                    CapabilityFactV1(
                        "proactive-send",
                        "unsupported",
                        "not-applicable",
                        {},
                        "qq-proactive-unsupported",
                    ),
                    CapabilityFactV1(
                        "qr-onboarding",
                        "unsupported",
                        "not-applicable",
                        {},
                        "qq-qr-license-unavailable",
                    ),
                    CapabilityFactV1(
                        "reactions", "unknown", "unknown", {}, "qq-reaction-not-proven"
                    ),
                    CapabilityFactV1(
                        "threads", "unsupported", "not-applicable", {}, "qq-thread-unsupported"
                    ),
                ),
                key=lambda fact: fact.name.encode(),
            )
        )
        return CapabilitySnapshot(
            self._instance_id,
            1,
            CAPABILITY_REVISION,
            "qq-official-identity",
            observed_at_ms,
            observed_at_ms + 300_000,
            facts,
        )


__all__ = [
    "QQCheckpointLedger",
    "QQCredentialResolver",
    "QQCredentials",
    "QQOfficialBotOnboarding",
    "QQOfficialBotRuntime",
]
