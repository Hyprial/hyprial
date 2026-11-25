"""Slack Socket Mode runtime, onboarding, and the production bundle.

The socket runtime and onboarding checks operate the authenticated I/O
ports, and ``build_default_components`` assembles them with the owned
HTTP client, transport, and media authority into one resource bundle.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..common.transport import HttpEffectState, ResourceCloseState

from ..contracts import (
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
from .errors import SlackHttpResult, SlackProtocolError
from .media import SlackMedia, SlackMediaScopeResolver
from .transport import OFFICIAL_API_BASE_URL, HttpxSlackTransport
from .types import (
    SlackCheckpointLedger,
    SlackCredentialResolver,
    SlackCredentials,
    SlackTransport,
    _CAPABILITY_REVISION,
    _integer,
    _map,
    _text,
)

MAX_FRAME_BYTES = 1_048_576
MAX_PENDING_ACKS = 256
# Frozen fallback error-code word; the rendered fallback value stays
# byte-identical to the reviewed R2 behavior.
_API_ERROR_CODE = "provider-error"


@dataclass(frozen=True, slots=True)
class SlackComponents:
    transport: HttpxSlackTransport
    runtime: SlackSocketRuntime
    onboarding: SlackOnboarding
    media: SlackMedia


def build_default_components(
    *,
    checkpoints: SlackCheckpointLedger,
    credentials: SlackCredentialResolver,
    receiver_ref: str,
    organization_id: str,
    instance_id: str,
    local_name: str,
    media_authority: Any,
    media_scope_resolver: SlackMediaScopeResolver | None,
    api_base_url: str = OFFICIAL_API_BASE_URL,
    allowed_upload_origins: set[str] | frozenset[str] = frozenset(),
    allowed_download_origins: set[str] | frozenset[str] = frozenset(),
    allow_insecure_loopback: bool = False,
    socket_url_override: str | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    settle: Any = None,
) -> SlackComponents:
    """Create a bundle with one owned no-env, no-redirect HTTP client."""

    client = httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        transport=http_transport,
    )
    transport = HttpxSlackTransport(
        client=client,
        owns_client=True,
        api_base_url=api_base_url,
        settle=settle,
        allow_insecure_loopback=allow_insecure_loopback,
        socket_url_override=socket_url_override,
    )
    media = SlackMedia(
        client,
        credentials,
        receiver_ref,
        authority=media_authority,
        scope_resolver=media_scope_resolver,
        api_base_url=api_base_url,
        allowed_upload_origins=allowed_upload_origins,
        allowed_download_origins=allowed_download_origins,
        settle=settle,
    )
    return SlackComponents(
        transport,
        SlackSocketRuntime(
            transport,
            checkpoints,
            credentials,
            receiver_ref,
            allow_insecure_loopback=allow_insecure_loopback,
        ),
        SlackOnboarding(
            transport, credentials, organization_id, instance_id, local_name
        ),
        media,
    )


class SlackSocketRuntime:
    """Durable-ACK Slack Socket Mode runtime."""

    def __init__(
        self,
        transport: SlackTransport,
        checkpoints: SlackCheckpointLedger,
        credentials: SlackCredentialResolver,
        receiver_ref: str,
        *,
        allow_insecure_loopback: bool = False,
    ) -> None:
        if not receiver_ref:
            raise ValueError("Slack receiver ref is required")
        self._transport = transport
        self._checkpoints = checkpoints
        self._credentials = credentials
        self._receiver_ref = receiver_ref
        self._allow_insecure_loopback = allow_insecure_loopback
        self._instance: AdapterInstanceRef | None = None
        self._credential: SlackCredentials | None = None
        self._team_id: str | None = None
        self._pending: dict[str, str] = {}
        self._resumed_unacked: set[str] = set()
        self._state = "configured"
        self._unknown_count = 0

    async def start(
        self, instance: AdapterInstanceRef, sink: IngressSink
    ) -> RuntimeSession:
        if instance.kind != "slack" or instance.variant != "socket-mode":
            raise ValueError("Slack runtime requires slack/socket-mode")
        self._state = "starting"
        credential = await self._credentials.resolve(self._receiver_ref)
        identity = _map(await self._transport.authenticate(credential), "auth response")
        if identity.get("ok") is not True:
            self._state = "blocked"
            raise ValueError("Slack credential authentication failed")
        team_id = _text(identity.get("team_id"), "team id")
        stable_id = _text(
            identity.get("bot_id") or identity.get("user_id"), "bot identity"
        )
        fingerprint = hashlib.sha256(
            f"slack\0slack-cloud\0{team_id}\0{stable_id}".encode()
        ).hexdigest()
        if fingerprint != instance.external_fingerprint:
            self._state = "blocked"
            raise ValueError(
                "Slack authenticated identity does not match instance fingerprint"
            )
        self._instance = instance
        self._credential = credential
        self._team_id = team_id
        try:
            resumed = await self._checkpoints.load(
                instance.instance_id, instance.generation
            )
        except Exception:
            self._state = "blocked"
            raise
        if resumed:
            self._resumed_unacked.add(resumed)
        await self._connect()
        self._state = "online"
        session_id = hashlib.sha256(
            f"slack\0{instance.instance_id}\0{instance.generation}\0{team_id}".encode()
        ).hexdigest()
        return RuntimeSession(
            session_id, instance.instance_id, instance.generation, "pull"
        )

    async def _connect(self) -> None:
        if self._credential is None:
            raise RuntimeError("Slack runtime is not authenticated")
        url = await self._transport.open_socket(self._credential)
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        official = (
            parsed.scheme == "wss"
            and (host == "slack.com" or host.endswith(".slack.com"))
            and parsed.port in (None, 443)
        )
        loopback = (
            self._allow_insecure_loopback
            and parsed.scheme == "ws"
            and host in {"127.0.0.1", "::1", "localhost"}
        )
        if not (official or loopback) or parsed.username or parsed.password:
            raise ValueError("Slack returned an unsafe Socket Mode URL")
        await self._transport.connect(url, max_frame_bytes=MAX_FRAME_BYTES)

    async def next_ingress(self) -> IngressUnit:
        instance = self._require_instance()
        while True:
            if len(self._pending) >= MAX_PENDING_ACKS:
                self._state = "degraded"
                raise RuntimeError("Slack pending Socket Mode ACK capacity reached")
            try:
                received = await self._transport.receive(
                    max_frame_bytes=MAX_FRAME_BYTES
                )
            except SlackProtocolError as error:
                if error.code != "SLACK_SOCKET_CLOSED":
                    raise
                observation = error.close_observation
                close_reason = str(getattr(observation, "rcvd_reason", "") or "")
                if close_reason == "link_disabled":
                    self._state = "blocked"
                    raise SlackProtocolError("SLACK_LINK_DISABLED") from error
                await self.reconnect(close_reason or "remote-close")
                continue
            frame = _map(received, "socket frame")
            frozen = freeze_json_v1(frame)
            if len(canonical_json_v1(frozen)) > MAX_FRAME_BYTES:
                raise ValueError("Slack frame exceeds bound")
            if frame.get("type") == "disconnect":
                reason = str(frame.get("reason") or "server-disconnect")
                if reason == "link_disabled":
                    self._state = "blocked"
                    raise SlackProtocolError("SLACK_LINK_DISABLED")
                if reason not in ("warning", "refresh_requested"):
                    raise SlackProtocolError("SLACK_DISCONNECT_REASON_UNSUPPORTED")
                await self.reconnect(reason)
                continue
            break
        if frame.get("type") != "events_api":
            raise ValueError("unsupported Slack Socket Mode envelope")
        envelope_id = _text(frame.get("envelope_id"), "envelope id")
        payload = _map(frame.get("payload"), "events payload")
        native = _map(payload.get("event"), "event")
        event_id = _text(payload.get("event_id"), "event id")
        message_id = _text(native.get("ts"), "message timestamp")
        channel_id = _text(native.get("channel"), "channel id")
        aliases = (
            NativeAlias(
                instance.instance_id,
                instance.generation,
                self._team_id or "unavailable",
                "slack-event",
                "create",
                "event",
                event_id,
                "slack-events-v1",
                platform="slack",
                chat_scope=channel_id,
            ),
            NativeAlias(
                instance.instance_id,
                instance.generation,
                self._team_id or "unavailable",
                "slack-message",
                "create",
                "message",
                message_id,
                "slack-events-v1",
                platform="slack",
                chat_scope=channel_id,
            ),
        )
        digest = frozen_json_digest_v1(frozen)
        observation = IngressObservation(
            f"observation:slack:{instance.instance_id}:{envelope_id}",
            aliases,
            "live",
            f"raw-source-ref:slack:{instance.instance_id}:{envelope_id}",
            digest,
            _integer(payload.get("event_time", 0), "event time") * 1000,
        )
        self._pending[envelope_id] = digest
        return IngressUnit(
            PlatformEventEnvelope(observation, frozen),
            NativeCheckpoint("callback-ack", envelope_id, None),
            False,
        )

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        instance = self._require_instance()
        if checkpoint.kind != "callback-ack" or checkpoint.token is None:
            raise ValueError("Slack requires callback-ack checkpoints")
        expected = self._pending.get(checkpoint.token)
        if expected is None:
            raise ValueError("Slack checkpoint is not pending")
        if custody.envelope_digest != expected:
            raise ValueError("Slack custody digest does not match pending envelope")
        await self._checkpoints.commit(
            instance.instance_id, instance.generation, checkpoint.token
        )
        await self._transport.acknowledge(checkpoint.token)
        del self._pending[checkpoint.token]
        self._resumed_unacked.discard(checkpoint.token)

    async def accept_callback(self, request: VerifiedCallbackRequest) -> IngressUnit:
        raise ValueError("Slack Socket Mode does not accept HTTP callbacks")

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        return HydratedEvent(envelope, envelope.payload)

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("Slack history limit must be 1..100")
        return ()

    async def resolve(self, target: AuthorizedChatTarget) -> NativeTargetResolution:
        instance = self._require_instance()
        if (
            target.chat_ref.instance != instance
            or target.chat_ref.account_scope != self._team_id
        ):
            return NativeTargetResolution(
                target, "unauthorized", None, None, "slack-target-scope-mismatch"
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
        api_method = _text(request.get("method"), "method")
        part_index = _integer(request.get("part_index", 0), "part index")
        if operation.kind == "file-upload" or api_method in (
            "files.upload",
            "files.uploadV2",
        ):
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index,
                        "failed-final",
                        None,
                        True,
                        "slack-media-route-required",
                    ),
                ),
                None,
            )
        try:
            result = await self._transport.call(
                api_method, request, operation.idempotency_key
            )
        except TimeoutError:
            self._unknown_count += 1
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index, "unknown", None, False, "slack-submit-timeout"
                    ),
                ),
                None,
            )
        if isinstance(result, SlackHttpResult):
            observation = result.observation
            if observation.state is HttpEffectState.ACCEPTED:
                native_id = observation.native_id
                if not native_id:
                    self._unknown_count += 1
                    return NativeSubmissionOutcome(
                        (
                            PartSubmission(
                                part_index,
                                "unknown",
                                None,
                                False,
                                "slack-success-native-id-missing",
                            ),
                        ),
                        None,
                    )
                return NativeSubmissionOutcome(
                    (PartSubmission(part_index, "accepted", native_id, False, None),),
                    None,
                )
            if observation.status_code == 429:
                return NativeSubmissionOutcome(
                    (
                        PartSubmission(
                            part_index,
                            "failed-retryable",
                            None,
                            observation.definitely_not_sent,
                            "slack-rate-limited",
                        ),
                    ),
                    result.retry_after_ms,
                )
            if observation.state is HttpEffectState.UNKNOWN:
                self._unknown_count += 1
                return NativeSubmissionOutcome(
                    (
                        PartSubmission(
                            part_index,
                            "unknown",
                            None,
                            False,
                            observation.reason_code,
                        ),
                    ),
                    None,
                )
            retryable = observation.state is HttpEffectState.RETRYABLE
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index,
                        "failed-retryable" if retryable else "failed-final",
                        None,
                        observation.definitely_not_sent,
                        result.error_code or observation.reason_code,
                    ),
                ),
                None,
            )
        response = _map(result, "Web API response")
        if response.get("ok") is True:
            native_id = response.get("ts")
            if not isinstance(native_id, str) or not native_id:
                self._unknown_count += 1
                return NativeSubmissionOutcome(
                    (
                        PartSubmission(
                            part_index,
                            "unknown",
                            None,
                            False,
                            "slack-success-native-id-missing",
                        ),
                    ),
                    None,
                )
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index,
                        "accepted",
                        native_id,
                        False,
                        None,
                    ),
                ),
                None,
            )
        status = response.get("status")
        reason = str(response.get("error") or f"slack-{_API_ERROR_CODE}")
        if status == 429 or reason == "ratelimited":
            retry_after = response.get("retry_after_ms")
            retry_ms = (
                retry_after
                if isinstance(retry_after, int) and retry_after >= 0
                else None
            )
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index, "failed-retryable", None, True, "slack-rate-limited"
                    ),
                ),
                retry_ms,
            )
        if isinstance(status, int) and status >= 500:
            self._unknown_count += 1
            return NativeSubmissionOutcome(
                (
                    PartSubmission(
                        part_index,
                        "unknown",
                        None,
                        False,
                        "slack-server-effect-unknown",
                    ),
                ),
                None,
            )
        return NativeSubmissionOutcome(
            (
                PartSubmission(
                    part_index,
                    "failed-final",
                    None,
                    True,
                    reason,
                ),
            ),
            None,
        )

    async def reconcile(
        self, operation: OperationNode, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome:
        if not any(part.state == "unknown" for part in prior.submissions):
            return prior
        self._validate_request(operation)
        return prior

    def _validate_request(self, operation: OperationNode) -> Mapping[str, object]:
        instance = self._require_instance()
        if operation.sealed_request is None:
            raise ValueError("Slack operation requires a sealed request")
        request = _map(operation.sealed_request, "sealed request")
        expected = {
            "provider": "slack",
            "organization_id": instance.organization_id,
            "instance_id": instance.instance_id,
            "generation": instance.generation,
            "receiver_ref": self._receiver_ref,
        }
        for field, value in expected.items():
            if request.get(field) != value:
                raise ValueError(f"Slack {field.replace('_', ' ')} mismatch")
        return request

    async def reconnect(self, reason: str) -> None:
        if not reason:
            raise ValueError("Slack reconnect reason is required")
        self._state = "reconnecting"
        close_outcome = await self._transport.close_socket(
            reason, asyncio.get_running_loop().time() + 5.0
        )
        if getattr(close_outcome, "state", None) is ResourceCloseState.UNKNOWN:
            self._state = "degraded"
            raise SlackProtocolError("SLACK_SOCKET_CLOSE_UNKNOWN")
        credential = await self._credentials.resolve(self._receiver_ref)
        identity = _map(await self._transport.authenticate(credential), "auth response")
        if identity.get("ok") is not True:
            self._state = "blocked"
            raise SlackProtocolError("SLACK_RECONNECT_AUTH_REJECTED")
        team_id = _text(identity.get("team_id"), "team id")
        stable_id = _text(
            identity.get("bot_id") or identity.get("user_id"), "bot identity"
        )
        fingerprint = hashlib.sha256(
            f"slack\0slack-cloud\0{team_id}\0{stable_id}".encode()
        ).hexdigest()
        if (
            team_id != self._team_id
            or fingerprint != self._require_instance().external_fingerprint
        ):
            self._state = "blocked"
            raise ValueError("Slack identity changed during reconnect")
        self._credential = credential
        await self._connect()
        self._state = "online"

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            self._state,
            _CAPABILITY_REVISION,
            len(self._pending) + len(self._resumed_unacked),
            self._unknown_count,
            () if self._state == "online" else (f"slack-{self._state}",),
        )

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        if isinstance(deadline_ms, bool) or not isinstance(deadline_ms, int):
            raise TypeError("Slack close deadline must be epoch milliseconds")
        remaining = max(0.0, (deadline_ms - time.time_ns() // 1_000_000) / 1000)
        result = await self._transport.aclose(
            asyncio.get_running_loop().time() + remaining
        )
        self._state = "stopped"
        retained = len(self._pending) + len(self._resumed_unacked)
        if getattr(result, "state", None) is ResourceCloseState.UNKNOWN:
            return AdapterCloseOutcome(
                "unknown",
                retained,
                self._unknown_count,
                getattr(result, "reason_code", None) or "slack-close-unknown",
            )
        if retained or self._unknown_count:
            return AdapterCloseOutcome(
                "retained-pending",
                retained,
                self._unknown_count,
                "slack-work-retained",
            )
        return AdapterCloseOutcome("closed", 0, 0, None)

    def _require_instance(self) -> AdapterInstanceRef:
        if self._instance is None:
            raise RuntimeError("Slack runtime has not started")
        return self._instance


class SlackOnboarding:
    def __init__(
        self,
        transport: SlackTransport,
        credentials: SlackCredentialResolver,
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
        value = _map(config, "onboarding config")
        required = value.get("credential_ref"), value.get("required_scopes")
        if not isinstance(required[0], str):
            return OnboardingPreflight("blocked", ("slack-credential-ref-required",))
        scopes = required[1]
        if scopes is not None and not isinstance(scopes, (list, tuple)):
            return OnboardingPreflight("blocked", ("slack-required-scopes-invalid",))
        return OnboardingPreflight("ready", ())

    async def authenticate(
        self, attempt: OnboardingAttempt
    ) -> VerifiedPlatformIdentity:
        credential = await self._credentials.resolve(attempt.credential_ref)
        identity = _map(await self._transport.authenticate(credential), "auth response")
        if identity.get("ok") is not True:
            raise ValueError("Slack authentication failed")
        team_id = _text(identity.get("team_id"), "team id")
        stable_id = _text(
            identity.get("bot_id") or identity.get("user_id"), "bot identity"
        )
        return VerifiedPlatformIdentity(
            team_id, stable_id, f"slack-auth-test:{team_id}:{stable_id}"
        )

    async def fingerprint(
        self, identity: VerifiedPlatformIdentity
    ) -> AdapterInstanceRef:
        fingerprint = hashlib.sha256(
            f"slack\0slack-cloud\0{identity.account_scope}\0{identity.stable_account_id}".encode()
        ).hexdigest()
        return AdapterInstanceRef(
            self._instance_id,
            "slack",
            "socket-mode",
            self._local_name,
            self._organization_id,
            fingerprint,
            1,
        )

    async def probe_capabilities(
        self, identity: VerifiedPlatformIdentity
    ) -> CapabilitySnapshot:
        observed_at_ms = time.time_ns() // 1_000_000
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
                        "slack-reactions-not-implemented",
                    ),
                    CapabilityFactV1("threads", "native", "granted", {}, None),
                ),
                key=lambda fact: fact.name.encode(),
            )
        )
        return CapabilitySnapshot(
            self._instance_id,
            1,
            _CAPABILITY_REVISION,
            "slack-auth-test",
            observed_at_ms,
            observed_at_ms + 300_000,
            facts,
        )

__all__ = [
    "SlackComponents",
    "SlackOnboarding",
    "SlackSocketRuntime",
    "build_default_components",
]
