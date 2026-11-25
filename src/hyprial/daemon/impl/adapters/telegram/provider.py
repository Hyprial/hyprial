"""Telegram Bot API long-poll runtime with durable update offsets."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Mapping, Sequence
from typing import Protocol

from ..common.transport import HttpEffectState, ResourceCloseOutcome, ResourceCloseState
from .contracts import (
    TelegramCredentialResolver,
    TelegramCredentials,
    _integer,
    _map,
    _text,
)
from .errors import TelegramPollRetry
from .transport import TelegramSendOutcome

from ..contracts import (
    AdapterCloseOutcome,
    AdapterHealth,
    AdapterInstanceRef,
    AttachmentHandle,
    AudienceSnapshot,
    AuthorizedChatTarget,
    CapabilityFactV1,
    CapabilitySnapshot,
    ChatContext,
    ChatMessage,
    ChatRef,
    CustodyReceipt,
    HydratedEvent,
    IngressObservation,
    IngressSink,
    IngressUnit,
    IntentCapability,
    NativeAlias,
    NativeCheckpoint,
    NativeSubmissionOutcome,
    NativeTargetResolution,
    NormalizedIngress,
    OnboardingAttempt,
    OnboardingPreflight,
    OperationDraft,
    OperationDraftNode,
    OperationNode,
    PartSubmission,
    PlatformEventEnvelope,
    PlatformRefsV1,
    PlatformScopeV1,
    PlatformUnknownV1,
    PrincipalRef,
    ReplyReference,
    RuntimeSession,
    SourceProvenance,
    TelegramProvenanceV1,
    TextBlock,
    VerifiedCallbackRequest,
    VerifiedPlatformIdentity,
    canonical_json_v1,
    freeze_json_v1,
    frozen_json_digest_v1,
)

MAX_RESPONSE_BYTES = 1_048_576
MAX_PENDING_UPDATES = 256
_CAPABILITY_REVISION = "telegram-bot-api-v1"


class TelegramCheckpointLedger(Protocol):
    async def load(self, instance_id: str, generation: int) -> str | None: ...

    async def commit(self, instance_id: str, generation: int, token: str) -> None: ...


class TelegramTransport(Protocol):
    async def get_me(self, credentials: TelegramCredentials) -> object: ...

    async def get_webhook_info(self, credentials: TelegramCredentials) -> object: ...

    async def get_updates(
        self, credentials: TelegramCredentials, *, offset: int, limit: int
    ) -> object: ...

    async def send_message(
        self, credentials: TelegramCredentials, payload: object
    ) -> TelegramSendOutcome: ...

    async def aclose(self, deadline_ms: int) -> ResourceCloseOutcome: ...


def _principal(row: Mapping[str, object]) -> PrincipalRef:
    subject = _text(row.get("id"), "sender id")
    name_parts = tuple(
        value for key in ("first_name", "last_name", "username") if isinstance((value := row.get(key)), str)
    )
    return PrincipalRef(
        subject,
        None,
        None,
        " ".join(name_parts) or None,
        None,
        "unresolved",
        None,
        None,
        None,
        "telegram-bot-api",
    )


def _intents() -> tuple[IntentCapability, ...]:
    return (
        IntentCapability("chat.reply.source", ("inherit", "corresponding", "exact"), True),
        IntentCapability("chat.reply.private", ("inherit", "corresponding", "exact"), True),
        IntentCapability("chat.send.explicit", ("corresponding", "exact"), False),
    )


def _file_kind(media_type: str | None, field: str) -> str:
    if field == "photo" or (media_type and media_type.startswith("image/")):
        return "image"
    if field in ("audio", "voice") or (media_type and media_type.startswith("audio/")):
        return "audio"
    if field in ("video", "video_note") or (media_type and media_type.startswith("video/")):
        return "video"
    return "file"


def _message_parts(update: Mapping[str, object]) -> tuple[Mapping[str, object], str | None]:
    callback = update.get("callback_query")
    if isinstance(callback, Mapping):
        message = _map(callback.get("message"), "callback message")
        callback_id = _text(callback.get("id"), "callback id")
        return message, callback_id
    for field in ("message", "edited_message", "channel_post", "edited_channel_post"):
        if isinstance(update.get(field), Mapping):
            return _map(update[field], "message"), None
    raise ValueError("Telegram update has no supported message or callback")


class TelegramEventCodec:
    def __init__(self, instance: AdapterInstanceRef, bot_id: str) -> None:
        if instance.kind != "telegram" or instance.variant != "bot":
            raise ValueError("Telegram codec requires telegram/bot")
        self._instance = instance
        self._bot_id = bot_id

    def normalize(self, event: HydratedEvent) -> NormalizedIngress:
        return self.decode(event)[0]

    def decode(self, event: HydratedEvent) -> tuple[NormalizedIngress, TelegramProvenanceV1]:
        update = _map(event.body, "update")
        update_id = _integer(update.get("update_id"), "update id")
        message, callback_id = _message_parts(update)
        chat = _map(message.get("chat"), "chat")
        sender_row = (
            _map(update["callback_query"], "callback").get("from")
            if callback_id is not None
            else message.get("from")
        )
        sender = _principal(_map(sender_row, "sender"))
        chat_id = _text(chat.get("id"), "chat id")
        chat_type = _text(chat.get("type"), "chat type")
        message_id_raw = message.get("message_id")
        message_id = _integer(message_id_raw, "message id") if message_id_raw is not None else None
        topic_raw = message.get("message_thread_id")
        topic_id = _integer(topic_raw, "topic id") if topic_raw is not None else None
        file_ids: list[str] = []
        attachments: list[AttachmentHandle] = []
        for field in ("photo", "document", "audio", "voice", "video", "video_note", "animation"):
            raw = message.get(field)
            rows: Sequence[object]
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, Mapping)):
                rows = raw[-1:] if field == "photo" else raw
            elif isinstance(raw, Mapping):
                rows = (raw,)
            else:
                rows = ()
            for item in rows:
                row = _map(item, field)
                file_id = _text(row.get("file_id"), "file id")
                file_ids.append(file_id)
                media_type = row.get("mime_type") if isinstance(row.get("mime_type"), str) else None
                size = row.get("file_size") if isinstance(row.get("file_size"), int) else None
                attachments.append(
                    AttachmentHandle(
                        f"telegram:{self._instance.instance_id}:{file_id}",
                        _file_kind(media_type, field),
                        row.get("file_name") if isinstance(row.get("file_name"), str) else None,
                        media_type,
                        size,
                        None,
                        str(message_id or callback_id),
                        None,
                    )
                )
        kind = "dm" if chat_type == "private" else "thread" if topic_id is not None else "group"
        chat_ref = ChatRef(
            f"chat-ref:v1:telegram:{self._instance.instance_id}:{chat_id}:{topic_id or '-'}",
            self._instance,
            self._bot_id,
            chat_id,
            kind,
            str(topic_id) if topic_id is not None else None,
            str(topic_id) if topic_id is not None else None,
        )
        audience = AudienceSnapshot(
            "private" if kind == "dm" else "group", (sender,), "partial", None, 1
        )
        context = ChatContext(
            f"adapter:telegram:{self._instance.instance_id}:{chat_id}",
            chat_ref,
            chat.get("title") if isinstance(chat.get("title"), str) else None,
            sender,
            audience,
            (),
            _intents(),
            _CAPABILITY_REVISION,
        )
        provenance = SourceProvenance(
            f"ingress:telegram:{self._instance.instance_id}:{update_id}",
            (event.envelope.observation.observation_id,),
            event.envelope.observation.aliases,
            event.envelope.observation.received_at_ms,
            _integer(message.get("date", 0), "message date") * 1000,
            "native-control" if callback_id else "native-human",
            sender.provider_subject_id,
        )
        refs = PlatformRefsV1(
            event_id=str(update_id),
            message_id=str(message_id) if message_id is not None else None,
            reply_id=(
                str(_map(message["reply_to_message"], "reply").get("message_id"))
                if isinstance(message.get("reply_to_message"), Mapping)
                else None
            ),
            thread_id=str(topic_id) if topic_id is not None else None,
            control_id=callback_id,
            attachment_refs=tuple(file_ids),
        )
        platform = TelegramProvenanceV1(
            "hyprial.chat.platform.telegram/v1",
            PlatformScopeV1(
                self._instance.organization_id,
                self._instance.instance_id,
                self._instance.generation,
                self._bot_id,
                "telegram-cloud",
            ),
            refs,
            PlatformUnknownV1({}, event.envelope.observation.raw_safe_ref),
            self._bot_id,
            update_id,
            chat_id,
            chat_type,
            message_id,
            topic_id,
            callback_id,
            sender.provider_subject_id,
            tuple(file_ids),
        )
        reply_to = None
        if isinstance(message.get("reply_to_message"), Mapping):
            reply = _map(message["reply_to_message"], "reply")
            reply_id = _text(reply.get("message_id"), "reply message id")
            excerpt_text = str(reply.get("text") or reply.get("caption") or "")
            excerpt = (TextBlock("text", excerpt_text),) if excerpt_text else ()
            reply_to = ReplyReference(None, reply_id, None, excerpt, (), None if excerpt else "not-delivered")
        text = str(message.get("text") or message.get("caption") or "").strip()
        if callback_id is not None:
            callback = _map(update.get("callback_query"), "callback")
            text = str(callback.get("data") or text).strip()
        content = (TextBlock("text", text),) if text else ()
        return (
            NormalizedIngress(context, provenance, content, tuple(attachments), reply_to),
            platform,
        )

    def render(self, target: NativeTargetResolution, message: ChatMessage | object) -> OperationDraft:
        if target.status != "resolved" or target.native_chat_id is None:
            raise ValueError("Telegram render requires a resolved target")
        if target.target.chat_ref.instance != self._instance:
            raise ValueError("Telegram target instance mismatch")
        blocks = getattr(message, "content", None)
        if blocks is None and hasattr(message, "payload"):
            blocks = getattr(message.payload, "content", ())
        text = "\n".join(block.text for block in blocks or () if isinstance(block, TextBlock)).strip()
        attachments = tuple(getattr(message, "attachments", ()))
        reply_parameters = (
            {"reply_parameters": {"message_id": target.target.canonical_parent_id}}
            if target.target.canonical_parent_id is not None
            else {}
        )
        nodes: list[OperationDraftNode] = []
        for index, attachment in enumerate(attachments):
            method = "sendPhoto" if attachment.kind == "image" else "sendDocument"
            nodes.append(
                OperationDraftNode(
                    f"attachment-{index}",
                    "file-send",
                    (),
                    {
                        "provider": "telegram",
                        "organization_id": self._instance.organization_id,
                        "instance_id": self._instance.instance_id,
                        "generation": self._instance.generation,
                        "method": method,
                        "chat_id": target.native_chat_id,
                        "message_thread_id": target.native_thread_id,
                        "handle_id": attachment.handle_id,
                        "part_index": index,
                        **reply_parameters,
                    },
                )
            )
        if text or not nodes:
            nodes.append(
                OperationDraftNode(
                    "message",
                    "text-send",
                    (),
                    {
                        "provider": "telegram",
                        "organization_id": self._instance.organization_id,
                        "instance_id": self._instance.instance_id,
                        "generation": self._instance.generation,
                        "method": "sendMessage",
                        "chat_id": target.native_chat_id,
                        "message_thread_id": target.native_thread_id,
                        "text": text,
                        "part_index": len(nodes),
                        **reply_parameters,
                    },
                )
            )
        material = {"text": text, "attachments": [item.handle_id for item in attachments]}
        return OperationDraft(
            "telegram-render-v1", _CAPABILITY_REVISION, frozen_json_digest_v1(material), tuple(nodes)
        )


class TelegramBotRuntime:
    def __init__(
        self,
        transport: TelegramTransport,
        checkpoints: TelegramCheckpointLedger,
        credentials: TelegramCredentialResolver,
        receiver_ref: str,
    ) -> None:
        self._transport = transport
        self._checkpoints = checkpoints
        self._credentials = credentials
        self._receiver_ref = receiver_ref
        self._instance: AdapterInstanceRef | None = None
        self._credential: TelegramCredentials | None = None
        self._bot_id: str | None = None
        self._offset = 0
        self._pending: dict[str, str] = {}
        self._state = "configured"
        self._unknown_count = 0
        self._close_task: asyncio.Task[AdapterCloseOutcome] | None = None

    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession:
        if instance.kind != "telegram" or instance.variant != "bot":
            raise ValueError("Telegram runtime requires telegram/bot")
        self._state = "starting"
        credential = await self._credentials.resolve(self._receiver_ref)
        identity = _map(await self._transport.get_me(credential), "getMe response")
        if identity.get("ok") is not True:
            self._state = "blocked"
            raise ValueError("Telegram authentication failed")
        bot = _map(identity.get("result"), "bot identity")
        bot_id = _text(bot.get("id"), "bot id")
        fingerprint = hashlib.sha256(f"telegram\0telegram-cloud\0{bot_id}".encode()).hexdigest()
        if fingerprint != instance.external_fingerprint:
            self._state = "blocked"
            raise ValueError("Telegram bot does not match instance fingerprint")
        webhook = _map(await self._transport.get_webhook_info(credential), "webhook info")
        webhook_result = _map(webhook.get("result"), "webhook result")
        if webhook_result.get("url"):
            self._state = "blocked"
            raise ValueError("Telegram webhook is active; polling would double-consume updates")
        committed = await self._checkpoints.load(instance.instance_id, instance.generation)
        self._offset = int(committed) if committed is not None else 0
        self._instance = instance
        self._credential = credential
        self._bot_id = bot_id
        self._state = "online"
        session_id = hashlib.sha256(
            f"telegram\0{instance.instance_id}\0{instance.generation}\0{bot_id}".encode()
        ).hexdigest()
        return RuntimeSession(session_id, instance.instance_id, instance.generation, "pull")

    async def next_ingress(self) -> IngressUnit:
        instance = self._require_instance()
        if self._credential is None:
            raise RuntimeError("Telegram runtime is not authenticated")
        if len(self._pending) >= MAX_PENDING_UPDATES:
            self._state = "degraded"
            raise RuntimeError("Telegram pending update capacity reached")
        response = _map(
            await self._transport.get_updates(self._credential, offset=self._offset, limit=1),
            "getUpdates response",
        )
        if response.get("ok") is not True:
            raise RuntimeError("Telegram getUpdates failed")
        updates = response.get("result")
        if not isinstance(updates, Sequence) or isinstance(updates, (str, bytes)):
            raise ValueError("Telegram getUpdates result must be an array")
        if not updates:
            raise TelegramPollRetry("telegram-long-poll-empty")
        update = _map(updates[0], "update")
        update_id = _integer(update.get("update_id"), "update id")
        if update_id < self._offset:
            raise ValueError("Telegram returned an update below the committed offset")
        frozen = freeze_json_v1(update)
        if len(canonical_json_v1(frozen)) > MAX_RESPONSE_BYTES:
            raise ValueError("Telegram update exceeds bound")
        message, callback_id = _message_parts(update)
        message_id = message.get("message_id")
        chat_id = _text(_map(message.get("chat"), "chat").get("id"), "chat id")
        aliases = [
            NativeAlias(
                instance.instance_id,
                instance.generation,
                self._bot_id or "unavailable",
                "telegram-update",
                "control" if callback_id else "create",
                "cursor-item",
                str(update_id),
                "telegram-update-v1",
                platform="telegram",
                chat_scope=chat_id,
            )
        ]
        if message_id is not None:
            aliases.append(
                NativeAlias(
                    instance.instance_id,
                    instance.generation,
                    self._bot_id or "unavailable",
                    "telegram-message",
                    "create",
                    "message",
                    _text(message_id, "message id"),
                    "telegram-update-v1",
                    platform="telegram",
                    chat_scope=chat_id,
                )
            )
        token = str(update_id + 1)
        digest = frozen_json_digest_v1(frozen)
        observation = IngressObservation(
            f"observation:telegram:{instance.instance_id}:{update_id}",
            tuple(aliases),
            "poll",
            f"raw-source-ref:telegram:{instance.instance_id}:{update_id}",
            digest,
            _integer(message.get("date", 0), "message date") * 1000,
        )
        self._pending[token] = digest
        return IngressUnit(
            PlatformEventEnvelope(observation, frozen), NativeCheckpoint("poll-cursor", token, None), False
        )

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None:
        instance = self._require_instance()
        if checkpoint.kind != "poll-cursor" or checkpoint.token is None:
            raise ValueError("Telegram requires poll-cursor checkpoints")
        expected = self._pending.get(checkpoint.token)
        if expected is None:
            raise ValueError("Telegram checkpoint is not pending")
        if custody.envelope_digest != expected:
            raise ValueError("Telegram custody digest mismatch")
        await self._checkpoints.commit(instance.instance_id, instance.generation, checkpoint.token)
        self._offset = int(checkpoint.token)
        del self._pending[checkpoint.token]

    async def accept_callback(self, request: VerifiedCallbackRequest) -> IngressUnit:
        raise ValueError("Telegram polling runtime does not accept webhook callbacks")

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent:
        return HydratedEvent(envelope, envelope.payload)

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("Telegram history limit must be 1..100")
        return ()

    async def resolve(self, target: AuthorizedChatTarget) -> NativeTargetResolution:
        instance = self._require_instance()
        if target.chat_ref.instance != instance or target.chat_ref.account_scope != self._bot_id:
            return NativeTargetResolution(target, "unauthorized", None, None, "telegram-target-scope-mismatch")
        return NativeTargetResolution(
            target,
            "resolved",
            target.chat_ref.native_chat_id,
            target.chat_ref.native_thread_id,
            None,
        )

    async def submit(self, operation: OperationNode) -> NativeSubmissionOutcome:
        request = self._validate_request(operation)
        method = _text(request.get("method"), "method")
        part_index = _integer(request.get("part_index", 0), "part index")
        if getattr(operation, "kind", None) != "text-send" or method != "sendMessage":
            raise ValueError("Telegram runtime is text-only; media must use TelegramMedia.upload")
        if self._credential is None:
            raise RuntimeError("Telegram runtime is not authenticated")
        try:
            outcome = await self._transport.send_message(self._credential, request)
        except TimeoutError:
            self._unknown_count += 1
            return NativeSubmissionOutcome(
                (PartSubmission(part_index, "unknown", None, False, "telegram-submit-timeout"),), None
            )
        effect = outcome.effect
        if effect.state is HttpEffectState.ACCEPTED:
            submission = PartSubmission(
                part_index, "accepted", effect.native_id, False, None
            )
        elif effect.state is HttpEffectState.RETRYABLE:
            submission = PartSubmission(
                part_index,
                "failed-retryable",
                None,
                effect.definitely_not_sent,
                effect.reason_code,
            )
        elif effect.state is HttpEffectState.REJECTED:
            submission = PartSubmission(
                part_index,
                "failed-final",
                None,
                effect.definitely_not_sent,
                effect.reason_code,
            )
        else:
            self._unknown_count += 1
            submission = PartSubmission(
                part_index, "unknown", None, False, effect.reason_code
            )
        return NativeSubmissionOutcome(
            (submission,), outcome.retry_after_ms
        )

    async def reconcile(
        self, operation: OperationNode, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome:
        self._validate_request(operation)
        return prior

    def _validate_request(self, operation: OperationNode) -> Mapping[str, object]:
        instance = self._require_instance()
        if operation.sealed_request is None:
            raise ValueError("Telegram operation requires a sealed request")
        request = _map(operation.sealed_request, "sealed request")
        expected = {
            "provider": "telegram",
            "organization_id": instance.organization_id,
            "instance_id": instance.instance_id,
            "generation": instance.generation,
            "receiver_ref": self._receiver_ref,
        }
        for field, value in expected.items():
            if request.get(field) != value:
                raise ValueError(f"Telegram {field.replace('_', ' ')} mismatch")
        return request

    async def reconnect(self, reason: str) -> None:
        if not reason:
            raise ValueError("Telegram reconnect reason is required")
        self._state = "reconnecting"
        credential = await self._credentials.resolve(self._receiver_ref)
        identity = _map(await self._transport.get_me(credential), "getMe response")
        bot = _map(identity.get("result"), "bot identity")
        if _text(bot.get("id"), "bot id") != self._bot_id:
            self._state = "blocked"
            raise ValueError("Telegram identity changed during reconnect")
        webhook = _map(await self._transport.get_webhook_info(credential), "webhook info")
        if _map(webhook.get("result"), "webhook result").get("url"):
            self._state = "blocked"
            raise ValueError("Telegram webhook became active during reconnect")
        self._credential = credential
        self._state = "online"

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            self._state,
            _CAPABILITY_REVISION,
            len(self._pending),
            self._unknown_count,
            () if self._state == "online" else (f"telegram-{self._state}",),
        )

    async def _close(self, deadline_ms: int) -> AdapterCloseOutcome:
        transport = await self._transport.aclose(deadline_ms)
        self._state = "stopped"
        if transport.state is ResourceCloseState.UNKNOWN:
            return AdapterCloseOutcome(
                "unknown",
                len(self._pending),
                self._unknown_count,
                transport.reason_code or "telegram-close-unknown",
            )
        if self._pending or self._unknown_count:
            return AdapterCloseOutcome(
                "retained-pending", len(self._pending), self._unknown_count, "telegram-work-retained"
            )
        return AdapterCloseOutcome("closed", 0, 0, None)

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(deadline_ms))
        return await asyncio.shield(self._close_task)

    def _require_instance(self) -> AdapterInstanceRef:
        if self._instance is None:
            raise RuntimeError("Telegram runtime has not started")
        return self._instance


class TelegramOnboarding:
    def __init__(
        self,
        transport: TelegramTransport,
        credentials: TelegramCredentialResolver,
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
        credential_ref = value.get("credential_ref")
        if not isinstance(credential_ref, str) or not credential_ref:
            return OnboardingPreflight("blocked", ("telegram-credential-ref-required",))
        if value.get("mode", "poll") != "poll":
            return OnboardingPreflight("unsupported", ("telegram-webhook-runtime-not-selected",))
        credential = await self._credentials.resolve(credential_ref)
        response = _map(await self._transport.get_webhook_info(credential), "webhook info")
        result = _map(response.get("result"), "webhook result")
        if result.get("url"):
            return OnboardingPreflight("blocked", ("telegram-webhook-active",))
        return OnboardingPreflight("ready", ())

    async def authenticate(self, attempt: OnboardingAttempt) -> VerifiedPlatformIdentity:
        credential = await self._credentials.resolve(attempt.credential_ref)
        response = _map(await self._transport.get_me(credential), "getMe response")
        if response.get("ok") is not True:
            raise ValueError("Telegram authentication failed")
        bot = _map(response.get("result"), "bot identity")
        bot_id = _text(bot.get("id"), "bot id")
        username = _text(bot.get("username"), "bot username")
        return VerifiedPlatformIdentity(bot_id, bot_id, f"telegram-get-me:{bot_id}:{username}")

    async def fingerprint(self, identity: VerifiedPlatformIdentity) -> AdapterInstanceRef:
        fingerprint = hashlib.sha256(
            f"telegram\0telegram-cloud\0{identity.account_scope}".encode()
        ).hexdigest()
        return AdapterInstanceRef(
            self._instance_id,
            "telegram",
            "bot",
            self._local_name,
            self._organization_id,
            fingerprint,
            1,
        )

    async def probe_capabilities(self, identity: VerifiedPlatformIdentity) -> CapabilitySnapshot:
        observed_at_ms = time.time_ns() // 1_000_000
        facts = tuple(
            sorted(
                (
                    CapabilityFactV1("files", "native", "granted", {}, None),
                    CapabilityFactV1("proactive-send", "native", "granted", {}, None),
                    CapabilityFactV1("reactions", "native", "unknown", {}, "telegram-version-probe-required"),
                    CapabilityFactV1("topics", "native", "granted", {}, None),
                ),
                key=lambda fact: fact.name.encode(),
            )
        )
        return CapabilitySnapshot(
            self._instance_id,
            1,
            _CAPABILITY_REVISION,
            "telegram-get-me",
            observed_at_ms,
            observed_at_ms + 300_000,
            facts,
        )
