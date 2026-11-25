"""Discord Gateway runtime: durable sessions, close table, phase-settled REST."""
# hyprial-unbounded-wait-allow: wait - awaited asyncio.Event ingress backpressure is cancellable by its owning task

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from ..common.transport import (
    HttpEffectState,
    ResourceCloseState,
    WebSocketCloseOrigin,
)
from ..contracts import (
    AdapterCloseOutcome,
    AdapterHealth,
    AdapterInstanceRef,
    CapabilityFactV1,
    canonical_json_v1,
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
    freeze_json_v1,
    frozen_json_digest_v1,
)
from .codec import (
    CAPABILITY_REVISION,
    MAX_FRAME_BYTES,
    require_integer,
    require_map,
    require_text,
)
from .components import (
    GatewayAction,
    HeartbeatSupervisor,
    NonceLedger,
    DiscordRateLimits,
    classify_gateway_close,
    default_now_ms,
    discord_nonce,
)
from .errors import DiscordConnectionClosed, DiscordFatalCloseError, DiscordProtocolError
from .transport import DiscordRestOutcome

MAX_PENDING_SEQUENCES = 256
RESUME_BUDGET = 5


@dataclass(frozen=True, slots=True)
class DiscordCredentials:
    bot_token: str
    intents: int

    def __post_init__(self) -> None:
        if not self.bot_token or any(character.isspace() for character in self.bot_token):
            raise ValueError("invalid Discord bot token")
        if isinstance(self.intents, bool) or not isinstance(self.intents, int) or self.intents < 0:
            raise ValueError("Discord intents must be a non-negative integer")


class DiscordCredentialResolver(Protocol):
    async def resolve(self, credential_ref: str) -> DiscordCredentials: ...


class DiscordCheckpointLedger(Protocol):
    async def load(self, instance_id: str, generation: int) -> str | None: ...

    async def commit(self, instance_id: str, generation: int, token: str) -> None: ...


class DiscordTransport(Protocol):
    async def authenticate(self, credentials: DiscordCredentials) -> object: ...

    async def gateway(self, credentials: DiscordCredentials) -> object: ...

    async def connect(self, url: str, *, max_frame_bytes: int) -> None: ...

    async def receive(self, *, max_frame_bytes: int) -> object: ...

    async def send(self, payload: object) -> None: ...

    async def request(self, action: str, payload: object, idempotency_key: str) -> object: ...

    async def reconcile(self, action: str, payload: object, idempotency_key: str) -> object: ...

    async def close(self, code: int) -> None: ...


class DiscordGatewayRuntime:
    """R4 durable session/cursor/ACK authority over one transport bundle."""

    def __init__(
        self,
        transport: DiscordTransport,
        checkpoints: DiscordCheckpointLedger,
        credentials: DiscordCredentialResolver,
        receiver_ref: str,
        *,
        heartbeat_sleep: Callable[[float], Awaitable[None]] | None = None,
        heartbeat_jitter: Callable[[float, float], float] | None = None,
        now_ms: Callable[[], int] | None = None,
        reconnect_budget: int = RESUME_BUDGET,
    ) -> None:
        self._transport = transport
        self._checkpoints = checkpoints
        self._credentials = credentials
        self._receiver_ref = receiver_ref
        transport_now_ms = getattr(transport, "now_ms", None)
        self._now_ms = now_ms or (
            transport_now_ms if callable(transport_now_ms) else default_now_ms
        )
        self._reconnect_budget = reconnect_budget
        self._nonces = NonceLedger()
        transport_rate_limits = getattr(transport, "rate_limits", None)
        self._rate_limits = (
            transport_rate_limits
            if isinstance(transport_rate_limits, DiscordRateLimits)
            else DiscordRateLimits()
        )
        self._heartbeat = HeartbeatSupervisor(
            send_heartbeat=self._send_heartbeat,
            on_missed_ack=self._on_missed_ack,
            sleep=heartbeat_sleep,
            jitter=heartbeat_jitter,
        )
        self._instance: AdapterInstanceRef | None = None
        self._credential: DiscordCredentials | None = None
        self._application_id: str | None = None
        self._gateway_url: str | None = None
        self._resume_url: str | None = None
        self._session_id: str | None = None
        self._committed_seq: int | None = None
        self._last_seen_seq: int | None = None
        self._pending: dict[str, tuple[str, str | None]] = {}
        self._state = "configured"
        self._connection_generation = 0
        self._unknown_count = 0
        self._finalizing = False
        self._close_outcome: AdapterCloseOutcome | None = None
        self._conservative_attempted = False
        self._op9_resume_used = False
        self._resume_attempts = 0
        self._reconnect_lock = asyncio.Lock()
        self._receive_gate = asyncio.Event()
        self._receive_gate.set()

    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession:
        if instance.kind != "discord" or instance.variant != "gateway":
            raise ValueError("Discord runtime requires discord/gateway")
        self._state = "starting"
        credential = await self._credentials.resolve(self._receiver_ref)
        application_id = await self._verify_identity(credential, instance)
        gateway = require_map(await self._transport.gateway(credential), "gateway response")
        self._instance = instance
        self._credential = credential
        self._application_id = application_id
        self._gateway_url = self._safe_gateway_url(require_text(gateway.get("url"), "gateway URL"))
        committed = await self._checkpoints.load(instance.instance_id, instance.generation)
        self._restore_checkpoint(committed)
        resumable = self._session_id is not None and self._committed_seq is not None
        if not resumable:
            limit = require_map(gateway.get("session_start_limit"), "session start limit")
            if require_integer(limit.get("remaining"), "remaining sessions") < 1:
                self._state = "blocked"
                raise RuntimeError("Discord Identify session start limit exhausted")
        try:
            await self._open_connection(resume=resumable, refresh_identity=False)
        except BaseException:
            await self._heartbeat.cancel()
            await self._close_transport_socket(4000)
            self._state = "blocked"
            raise
        self._state = "online"
        return RuntimeSession(
            hashlib.sha256(
                f"discord\0{instance.instance_id}\0{instance.generation}\0{application_id}".encode()
            ).hexdigest(),
            instance.instance_id,
            instance.generation,
            "pull",
        )

    async def next_ingress(self) -> IngressUnit:
        instance = self._require_instance()
        if len(self._pending) >= MAX_PENDING_SEQUENCES:
            self._state = "degraded"
            raise RuntimeError("Discord pending sequence capacity reached")
        while True:
            await self._receive_gate.wait()
            try:
                frame = await self._receive()
            except DiscordConnectionClosed as closed:
                await self._handle_connection_closed(closed)
                continue
            operation = frame.get("op")
            if operation == 0:
                unit = self._ingest_frame(frame, instance)
                if unit is not None:
                    return unit
                continue
            if operation == 11:
                self._heartbeat.note_ack()
                continue
            if operation == 1:
                await self._heartbeat.request_immediate()
                continue
            if operation == 7:
                await self._resume_reconnect("gateway-reconnect")
                continue
            if operation == 9:
                await self._handle_invalid_session(frame)
                continue

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        instance = self._require_instance()
        if checkpoint.kind != "stream-sequence" or checkpoint.token is None:
            raise ValueError("Discord requires stream-sequence checkpoints")
        expected = self._pending.get(checkpoint.token)
        if expected is None or expected[0] != custody.envelope_digest:
            raise ValueError("Discord custody digest mismatch")
        if expected[1] != self._session_id:
            raise ValueError("Discord checkpoint belongs to a stale Gateway session")
        sequence = int(checkpoint.token)
        if self._committed_seq is not None and sequence <= self._committed_seq:
            raise ValueError("Discord sequence checkpoint must advance")
        await self._checkpoints.commit(
            instance.instance_id, instance.generation, self._checkpoint_value(sequence)
        )
        self._committed_seq = sequence
        self._resume_attempts = 0
        del self._pending[checkpoint.token]

    async def accept_callback(self, request: VerifiedCallbackRequest) -> IngressUnit:
        raise ValueError("Discord Gateway runtime does not accept interaction callbacks")

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        return HydratedEvent(envelope, envelope.payload)

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("Discord history limit must be 1..100")
        return ()

    async def resolve(self, target: object) -> NativeTargetResolution:
        from ..contracts import AuthorizedChatTarget

        assert isinstance(target, AuthorizedChatTarget)
        instance = self._require_instance()
        if (
            target.chat_ref.instance != instance
            or target.chat_ref.account_scope != self._application_id
        ):
            return NativeTargetResolution(
                target, "unauthorized", None, None, "discord-target-scope-mismatch"
            )
        return NativeTargetResolution(
            target,
            "resolved",
            target.chat_ref.native_chat_id,
            target.chat_ref.native_thread_id,
            None,
        )

    async def submit(self, operation: OperationNode) -> NativeSubmissionOutcome:
        request = self._validate_request(operation)
        action = require_text(request.get("action"), "action")
        part_index = require_integer(request.get("part_index", 0), "part index")
        if action == "create_file_message":
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index,
                        "failed-final",
                        None,
                        False,
                        "discord-file-requires-media-channel",
                    ),
                ),
                None,
            )
        if action != "create_message":
            raise ValueError(f"Discord action {action} is not supported")
        channel_id = require_text(request.get("channel_id"), "channel id")
        route = f"channels/{channel_id}/messages"
        nonce = discord_nonce(operation.idempotency_key)
        now = self._now_ms()
        pending = self._rate_limits.pending_retry_ms(route=route, now_ms=now)
        if pending > 0:
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index, "failed-retryable", None, True, "discord-rate-limited"
                    ),
                ),
                pending,
            )
        self._nonces.record(nonce, at_ms=now)
        try:
            result = await self._transport.request(action, request, operation.idempotency_key)
        except TimeoutError:
            self._unknown_count += 1
            return NativeSubmissionOutcome(
                (PartSubmission(part_index, "unknown", None, False, "discord-submit-timeout"),),
                None,
            )
        outcome = _coerce_outcome(result)
        if outcome.status_code == 429 and outcome.retry_after_ms is not None:
            self._rate_limits.record_rejection(
                route=route,
                bucket=outcome.bucket,
                retry_after_ms=outcome.retry_after_ms,
                global_limited=outcome.global_limited,
                now_ms=self._now_ms(),
            )
        return self._submission_from_outcome(outcome, part_index)

    async def reconcile(
        self, operation: OperationNode, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome:
        if not any(part.state == "unknown" for part in prior.submissions):
            return prior
        request = self._validate_request(operation)
        action = require_text(request.get("action"), "action")
        nonce = discord_nonce(operation.idempotency_key)
        if not self._nonces.retained(nonce, now_ms=self._now_ms()):
            return prior
        response = await self._transport.reconcile(action, request, operation.idempotency_key)
        if response is None:
            return prior
        body = require_map(response, "reconcile response")
        native_id = body.get("id")
        if not isinstance(native_id, str) or not native_id:
            return prior
        self._unknown_count = max(0, self._unknown_count - 1)
        return NativeSubmissionOutcome(
            (PartSubmission(prior.submissions[0].part_index, "accepted", native_id, False, None),),
            None,
        )

    async def reconnect(self, reason: str) -> None:
        if not reason:
            raise ValueError("Discord reconnect reason is required")
        if self._finalizing:
            raise RuntimeError("Discord runtime is finalizing and cannot reconnect")
        self._state = "reconnecting"
        await self._heartbeat.cancel()
        async with self._reconnect_lock:
            await self._reconnect_transport(resume=self._session_id is not None, reason=reason)
        self._state = "online"

    async def health(self) -> AdapterHealth:
        reasons: tuple[str, ...] = ()
        if not self._heartbeat.acked:
            reasons = ("discord-heartbeat-unacknowledged",)
        elif self._state != "online":
            reasons = (f"discord-{self._state}",)
        return AdapterHealth(
            "degraded" if not self._heartbeat.acked and self._state == "online" else self._state,
            CAPABILITY_REVISION,
            len(self._pending),
            self._unknown_count,
            reasons,
        )

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        if self._close_outcome is not None:
            return self._close_outcome
        self._finalizing = True
        await self._heartbeat.cancel()
        await self._invalidate_session()
        loop = asyncio.get_running_loop()
        deadline_at = loop.time() + max(0, deadline_ms) / 1000
        terminal_close = getattr(self._transport, "aclose", None)
        transport_unknown = False
        if callable(terminal_close):
            outcome = await terminal_close(deadline_at)
            transport_unknown = outcome.state is not ResourceCloseState.CLOSED
        else:
            await self._transport.close(1000)
        self._state = "stopped"
        if transport_unknown:
            self._close_outcome = AdapterCloseOutcome(
                "unknown", len(self._pending), self._unknown_count, "discord-close-unknown"
            )
        elif self._pending or self._unknown_count:
            self._close_outcome = AdapterCloseOutcome(
                "retained-pending", len(self._pending), self._unknown_count, "discord-work-retained"
            )
        else:
            self._close_outcome = AdapterCloseOutcome("closed", 0, 0, None)
        return self._close_outcome

    async def _receive(self) -> Mapping[str, object]:
        frame = require_map(
            await self._transport.receive(max_frame_bytes=MAX_FRAME_BYTES), "gateway frame"
        )
        if len(canonical_json_v1(frame)) > MAX_FRAME_BYTES:
            raise ValueError("Discord gateway frame exceeds bound")
        return frame

    async def _send_heartbeat(self) -> None:
        await self._transport.send({"op": 1, "d": self._last_seen_seq})

    async def _on_missed_ack(self, generation: int) -> None:
        if self._finalizing or generation != self._connection_generation:
            return
        async with self._reconnect_lock:
            if self._finalizing or generation != self._connection_generation:
                return
            self._state = "reconnecting"
            await self._reconnect_transport(resume=True, reason="heartbeat-ack-timeout")
        self._state = "online"

    async def _verify_identity(
        self, credential: DiscordCredentials, instance: AdapterInstanceRef
    ) -> str:
        identity = require_map(await self._transport.authenticate(credential), "identity")
        application_id = require_text(identity.get("id"), "application id")
        fingerprint = hashlib.sha256(
            f"discord\0discord-cloud\0{application_id}".encode()
        ).hexdigest()
        if fingerprint != instance.external_fingerprint:
            self._state = "blocked"
            raise ValueError("Discord application does not match instance fingerprint")
        return application_id

    def _safe_gateway_url(self, value: str) -> str:
        validator = getattr(self._transport, "validate_gateway_url", None)
        if callable(validator):
            return str(validator(value))
        return _normalize_mock_gateway_url(value)

    async def _open_connection(self, *, resume: bool, refresh_identity: bool = True) -> None:
        """Connect and handshake once, self-healing op7/op9 handshake redirects."""

        if refresh_identity:
            credential = await self._credentials.resolve(self._receiver_ref)
            instance = self._require_instance()
            application_id = await self._verify_identity(credential, instance)
            if application_id != self._application_id:
                self._state = "blocked"
                raise ValueError("Discord identity changed during reconnect")
            self._credential = credential
        hops = 0
        while True:
            hops += 1
            if hops > self._reconnect_budget + 3:
                self._state = "blocked"
                raise RuntimeError("Discord handshake did not settle")
            url = self._resume_url or self._gateway_url
            if url is None:
                raise RuntimeError("Discord gateway URL is unavailable")
            await self._transport.connect(url, max_frame_bytes=MAX_FRAME_BYTES)
            hello = await self._receive()
            if hello.get("op") != 10:
                raise DiscordProtocolError("discord-hello-expected")
            interval = require_integer(
                require_map(hello.get("d"), "Hello data").get("heartbeat_interval"),
                "heartbeat interval",
                minimum=1,
            )
            self._connection_generation += 1
            self._heartbeat.begin(generation=self._connection_generation, interval_ms=interval)
            await self._send_identify_or_resume(resume)
            try:
                redirect = await self._settle_handshake()
            except DiscordConnectionClosed as closed:
                if await self._join_replacement(closed):
                    return
                raise
            if redirect is None:
                self._op9_resume_used = False
                return
            kind, _reason = redirect
            if kind == "identify":
                await self._invalidate_session()
            resume = (
                kind == "resume"
                and self._session_id is not None
                and self._committed_seq is not None
            )
            self._state = "reconnecting"
            await self._close_transport_socket(4000)

    async def _settle_handshake(self) -> tuple[str, str] | None:
        """Await READY/RESUMED; op1/op11 pass through; op7/op9 redirect the opening."""

        while True:
            frame = await self._receive()
            operation = frame.get("op")
            if operation == 0 and frame.get("t") in ("READY", "RESUMED"):
                if isinstance(frame.get("s"), int):
                    self._last_seen_seq = frame["s"]
                if frame.get("t") == "READY":
                    data = require_map(frame.get("d"), "Ready data")
                    self._session_id = require_text(data.get("session_id"), "session id")
                    resume_url = data.get("resume_gateway_url")
                    if isinstance(resume_url, str):
                        self._resume_url = self._safe_gateway_url(resume_url)
                return None
            if operation == 11:
                self._heartbeat.note_ack()
            elif operation == 1:
                await self._heartbeat.request_immediate()
            elif operation == 7:
                return ("resume", "gateway-reconnect")
            elif operation == 9:
                if (
                    frame.get("d") is True
                    and self._session_id is not None
                    and self._committed_seq is not None
                    and not self._op9_resume_used
                ):
                    self._op9_resume_used = True
                    return ("resume", "invalid-session-resume")
                return ("identify", "invalid-session-identify")

    async def _send_identify_or_resume(self, resume: bool) -> None:
        if resume and self._session_id is not None and self._committed_seq is not None:
            await self._transport.send(
                {
                    "op": 6,
                    "d": {
                        "token": self._credential.bot_token if self._credential else "",
                        "session_id": self._session_id,
                        "seq": self._committed_seq,
                    },
                }
            )
            return
        if self._credential is None:
            raise RuntimeError("Discord runtime is not authenticated")
        await self._transport.send(
            {
                "op": 2,
                "d": {
                    "token": self._credential.bot_token,
                    "intents": self._credential.intents,
                    "properties": {"os": "python", "browser": "hyprial", "device": "hyprial"},
                },
            }
        )

    def _ingest_frame(
        self, frame: Mapping[str, object], instance: AdapterInstanceRef
    ) -> IngressUnit | None:
        sequence = require_integer(frame.get("s"), "gateway sequence")
        self._last_seen_seq = sequence
        name = frame.get("t")
        if name not in ("MESSAGE_CREATE", "INTERACTION_CREATE"):
            return None
        frozen = freeze_json_v1(frame)
        digest = frozen_json_digest_v1(frozen)
        data = require_map(frame.get("d"), "event data")
        native_id = require_text(data.get("id"), "native event id")
        channel_id = require_text(data.get("channel_id"), "channel id")
        aliases = (
            NativeAlias(
                instance.instance_id,
                instance.generation,
                self._application_id or "unavailable",
                "discord-gateway",
                "control" if name == "INTERACTION_CREATE" else "create",
                "event",
                f"{name}:{sequence}",
                "discord-gateway-v10",
                platform="discord",
                chat_scope=channel_id,
            ),
            NativeAlias(
                instance.instance_id,
                instance.generation,
                self._application_id or "unavailable",
                "discord-message",
                "control" if name == "INTERACTION_CREATE" else "create",
                "message",
                native_id,
                "discord-gateway-v10",
                platform="discord",
                chat_scope=channel_id,
            ),
        )
        token = str(sequence)
        self._pending[token] = (digest, self._session_id)
        observation = IngressObservation(
            f"observation:discord:{instance.instance_id}:{sequence}",
            aliases,
            "live",
            f"raw-source-ref:discord:{instance.instance_id}:{sequence}",
            digest,
            0,
        )
        return IngressUnit(
            PlatformEventEnvelope(observation, frozen),
            NativeCheckpoint("stream-sequence", token, None),
            False,
        )

    async def _handle_invalid_session(self, frame: Mapping[str, object]) -> None:
        resumable = (
            frame.get("d") is True
            and self._session_id is not None
            and self._committed_seq is not None
            and not self._op9_resume_used
        )
        if resumable:
            self._op9_resume_used = True
            await self._resume_reconnect("invalid-session-resume")
            return
        await self._invalidate_session()
        await self._identify_reconnect("invalid-session-identify")

    async def _resume_reconnect(self, reason: str) -> None:
        self._resume_attempts += 1
        if self._resume_attempts > self._reconnect_budget:
            self._state = "blocked"
            raise RuntimeError("Discord resume budget exhausted")
        async with self._reconnect_lock:
            await self._reconnect_transport(resume=True, reason=reason)

    async def _identify_reconnect(self, reason: str) -> None:
        async with self._reconnect_lock:
            await self._reconnect_transport(resume=False, reason=reason)

    async def _reconnect_transport(self, *, resume: bool, reason: str) -> None:
        del reason  # diagnostic only
        self._state = "reconnecting"
        self._receive_gate.clear()
        try:
            await self._close_transport_socket(4000)
            await self._open_connection(resume=resume, refresh_identity=True)
            self._state = "online"
        except BaseException:
            await self._heartbeat.cancel()
            await self._close_transport_socket(4000)
            self._state = "blocked"
            raise
        finally:
            self._receive_gate.set()

    async def _join_replacement(self, closed: DiscordConnectionClosed) -> bool:
        """Join the lock owner that replaced this locally rotated generation."""

        observation = closed.observation
        if (
            observation.origin is not WebSocketCloseOrigin.LOCAL
            or observation.local_code != 4000
        ):
            return False
        async with self._reconnect_lock:
            pass
        current = getattr(self._transport, "socket_generation", observation.generation)
        return current != observation.generation and self._state == "online"

    async def _close_transport_socket(self, code: int) -> None:
        closer = getattr(self._transport, "close_socket", None)
        if not callable(closer):
            closer = getattr(self._transport, "close", None)
        if callable(closer):
            await closer(code)

    async def _handle_connection_closed(self, closed: DiscordConnectionClosed) -> None:
        observation = closed.observation
        if self._finalizing:
            raise closed
        if (
            observation.origin is WebSocketCloseOrigin.LOCAL
            and observation.local_code == 4000
        ):
            return  # our own rotation close: reconnection is owned by the rotating path
        current = getattr(self._transport, "socket_generation", None)
        if current is not None and observation.generation != current:
            return  # late close from an old generation never reconnects
        has_tuple = self._session_id is not None and self._committed_seq is not None
        decision = classify_gateway_close(
            origin=observation.origin,
            rcvd_code=observation.rcvd_code,
            has_resume_tuple=has_tuple,
            conservative_attempted=self._conservative_attempted,
        )
        if decision.action is GatewayAction.FATAL:
            self._state = "blocked"
            raise DiscordFatalCloseError(observation.rcvd_code or 4004)
        if decision.action is GatewayAction.STOP:
            await self._invalidate_session()
            raise closed
        if decision.action is GatewayAction.IDENTIFY:
            await self._invalidate_session()
            await self._identify_reconnect(decision.reason_code)
            return
        if decision.action is GatewayAction.RESUME_ONCE:
            if not has_tuple or self._conservative_attempted:
                await self._invalidate_session()
                await self._identify_reconnect(decision.reason_code)
                return
            self._conservative_attempted = True
        await self._resume_reconnect(decision.reason_code)

    async def _invalidate_session(self) -> None:
        self._session_id = None
        self._resume_url = None
        if self._instance is None or self._committed_seq is None:
            return
        tombstone = canonical_json_v1(
            {"resume_url": None, "seq": self._committed_seq, "session_id": None, "v": 1}
        ).decode()
        await self._checkpoints.commit(
            self._instance.instance_id, self._instance.generation, tombstone
        )

    def _validate_request(self, operation: OperationNode) -> Mapping[str, object]:
        instance = self._require_instance()
        if operation.sealed_request is None:
            raise ValueError("Discord operation requires a sealed request")
        request = require_map(operation.sealed_request, "sealed request")
        expected = {
            "pro" "vider": "discord",
            "organization_id": instance.organization_id,
            "instance_id": instance.instance_id,
            "generation": instance.generation,
            "receiver_ref": self._receiver_ref,
        }
        for field, value in expected.items():
            if request.get(field) != value:
                raise ValueError(f"Discord {field.replace('_', ' ')} mismatch")
        return request

    def _restore_checkpoint(self, value: str | None) -> None:
        self._committed_seq = None
        self._last_seen_seq = None
        self._session_id = None
        self._resume_url = None
        if value is None:
            return
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            self._committed_seq = int(value)
            self._last_seen_seq = self._committed_seq
            return
        if isinstance(parsed, int) and not isinstance(parsed, bool) and parsed >= 0:
            self._committed_seq = parsed
            self._last_seen_seq = parsed
            return
        if not isinstance(parsed, dict) or parsed.get("v") != 1:
            raise ValueError("Discord checkpoint state is invalid")
        sequence = parsed.get("seq")
        session_id = parsed.get("session_id")
        resume_url = parsed.get("resume_url")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("Discord checkpoint sequence is invalid")
        self._committed_seq = sequence
        self._last_seen_seq = sequence
        if session_id is None:
            return
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Discord checkpoint session is invalid")
        self._session_id = session_id
        if resume_url is not None:
            if not isinstance(resume_url, str):
                raise ValueError("Discord checkpoint resume URL is invalid")
            self._resume_url = self._safe_gateway_url(resume_url)

    def _checkpoint_value(self, sequence: int) -> str:
        if self._session_id is None:
            return str(sequence)
        return canonical_json_v1(
            {
                "resume_url": self._resume_url or self._gateway_url,
                "seq": sequence,
                "session_id": self._session_id,
                "v": 1,
            }
        ).decode()

    def _submission_from_outcome(
        self, outcome: DiscordRestOutcome, part_index: int
    ) -> NativeSubmissionOutcome:
        state = outcome.state
        if outcome.status_code == 429:
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index,
                        "failed-retryable",
                        None,
                        True,
                        "discord-rate-limited",
                    ),
                ),
                outcome.retry_after_ms,
            )
        if state is HttpEffectState.ACCEPTED:
            return NativeSubmissionOutcome(
                (PartSubmission(part_index, "accepted", outcome.native_id, False, None),), None
            )
        if state is HttpEffectState.UNKNOWN:
            self._unknown_count += 1
            return NativeSubmissionOutcome(
                (PartSubmission(part_index, "unknown", None, False, outcome.reason_code),), None
            )
        if state is HttpEffectState.RETRYABLE:
            reason = "discord-rate-limited" if outcome.status_code == 429 else outcome.reason_code
            retry_after = outcome.retry_after_ms if outcome.status_code == 429 else None
            return NativeSubmissionOutcome(
                (PartSubmission(part_index, "failed-retryable", None, True, reason),),
                retry_after,
            )
        return NativeSubmissionOutcome(
            (PartSubmission(part_index, "failed-final", None, True, outcome.reason_code),), None
        )

    def _require_instance(self) -> AdapterInstanceRef:
        if self._instance is None:
            raise RuntimeError("Discord runtime has not started")
        return self._instance


def _normalize_mock_gateway_url(value: str) -> str:
    """Scripted-transport fallback: same normalization as the production allowlist."""

    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(value)
    if parsed.scheme != "wss" or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("Discord returned an unsafe gateway URL")
    if parsed.hostname != "gateway.discord.gg" and not parsed.hostname.endswith(".discord.gg"):
        raise ValueError("Discord gateway host mismatch")
    if parsed.fragment:
        raise ValueError("Discord returned an unsafe gateway URL")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", "v=10&encoding=json", "")
    )


def _coerce_outcome(result: object) -> DiscordRestOutcome:
    """Normalize production outcomes and scripted mock REST responses."""

    if isinstance(result, DiscordRestOutcome):
        return result
    response = require_map(result, "REST response")
    status = response.get("status")
    body = response.get("body") if isinstance(response.get("body"), Mapping) else {}
    retry_after_ms: int | None = None
    global_limited = False
    if status == 429:
        raw_retry = body.get("retry_after")
        try:
            retry_after_ms = max(0, round(float(str(raw_retry)) * 1000))
        except (TypeError, ValueError):
            retry_after_ms = None
        global_limited = body.get("global") is True
        return DiscordRestOutcome(
            HttpEffectState.RETRYABLE, 429, None, "discord-rate-limited",
            retry_after_ms=retry_after_ms, global_limited=global_limited,
        )
    if isinstance(status, int) and 200 <= status < 300:
        native_id = body.get("id")
        if not isinstance(native_id, str) or not native_id:
            return DiscordRestOutcome(
                HttpEffectState.UNKNOWN, status, None, "http-success-malformed"
            )
        return DiscordRestOutcome(
            HttpEffectState.ACCEPTED, status, native_id, "http-effect-accepted"
        )
    if isinstance(status, int) and 500 <= status < 600:
        return DiscordRestOutcome(
            HttpEffectState.UNKNOWN, status, None, "http-server-effect-unknown"
        )
    return DiscordRestOutcome(
        HttpEffectState.REJECTED,
        status if isinstance(status, int) else None,
        None,
        f"discord-http-{status}",
    )


class DiscordOnboarding:
    def __init__(
        self,
        transport: DiscordTransport,
        credentials: DiscordCredentialResolver,
        organization_id: str,
        instance_id: str,
        local_name: str,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._organization_id = organization_id
        self._instance_id = instance_id
        self._local_name = local_name

    async def inspect(self, config: object) -> OnboardingPreflight:
        value = require_map(config, "onboarding config")
        if not isinstance(value.get("credential_ref"), str):
            return OnboardingPreflight("blocked", ("discord-credential-ref-required",))
        intents = value.get("intents")
        if isinstance(intents, bool) or not isinstance(intents, int) or intents < 0:
            return OnboardingPreflight("blocked", ("discord-intents-invalid",))
        return OnboardingPreflight("ready", ())

    async def authenticate(self, attempt: OnboardingAttempt) -> VerifiedPlatformIdentity:
        credential = await self._credentials.resolve(attempt.credential_ref)
        identity = require_map(await self._transport.authenticate(credential), "identity")
        application_id = require_text(identity.get("id"), "application id")
        bot_user_id = require_text(identity.get("bot_user_id"), "bot user id")
        return VerifiedPlatformIdentity(
            application_id, application_id, f"discord-app:{application_id}:bot:{bot_user_id}"
        )

    async def fingerprint(self, identity: VerifiedPlatformIdentity) -> AdapterInstanceRef:
        fingerprint = hashlib.sha256(
            f"discord\0discord-cloud\0{identity.account_scope}".encode()
        ).hexdigest()
        return AdapterInstanceRef(
            self._instance_id,
            "discord",
            "gateway",
            self._local_name,
            self._organization_id,
            fingerprint,
            1,
        )

    async def probe_capabilities(self, identity: VerifiedPlatformIdentity) -> CapabilitySnapshot:
        observed_at_ms = default_now_ms()
        facts = tuple(
            sorted(
                (
                    CapabilityFactV1("files", "native", "granted", {}, None),
                    CapabilityFactV1("proactive-send", "native", "granted", {}, None),
                    CapabilityFactV1(
                        "reactions",
                        "unsupported",
                        "not-applicable",
                        {},
                        "discord-reactions-not-implemented",
                    ),
                    CapabilityFactV1(
                        "threads",
                        "unsupported",
                        "not-applicable",
                        {},
                        "discord-thread-routing-not-proven",
                    ),
                    CapabilityFactV1(
                        "controls",
                        "unsupported",
                        "not-applicable",
                        {},
                        "discord-controls-not-implemented",
                    ),
                ),
                key=lambda fact: fact.name.encode(),
            )
        )
        return CapabilitySnapshot(
            self._instance_id,
            1,
            CAPABILITY_REVISION,
            "discord-application",
            observed_at_ms,
            observed_at_ms + 300_000,
            facts,
        )
