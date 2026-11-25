from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters import dingtalk as dingtalk_provider
from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AudienceSnapshot,
    AuthorizedChatTarget,
    ChatRef,
    CustodyReceipt,
    FrozenJsonMap,
    OnboardingAttempt,
    SealedProviderMediaRef,
    TextBlock,
    VerifiedCallbackRequest,
    freeze_json_v1,
)
from hyprial.daemon.impl.adapters.dingtalk import (
    DingTalkCredentials,
    DingTalkProtocolError,
    DingTalkReplyRouteStore,
    DingTalkStreamRuntime,
    classify_dingtalk_card_step,
    decode_dingtalk_message,
)


class _Transport:
    def __init__(self) -> None:
        self.started = 0
        self.acks: list[tuple[str, str]] = []
        self.replies: list[tuple[str, dict[str, object]]] = []

    async def start(self, credentials: DingTalkCredentials, receiver: object) -> None:
        self.started += 1
        self.receiver = receiver

    async def stop(self) -> None:
        return None

    async def ack(self, delivery_id: str, status: str) -> None:
        self.acks.append((delivery_id, status))

    async def reply(self, webhook: str, payload: dict[str, object]) -> dict[str, object]:
        self.replies.append((webhook, payload))
        return {"errcode": 0, "messageId": "dt-out-1"}


def _instance(generation: int = 4) -> AdapterInstanceRef:
    return AdapterInstanceRef(
        "dt-instance", "dingtalk", "stream", "dt", "org-a", "fp-dt", generation
    )


def _credentials(generation: int = 4) -> DingTalkCredentials:
    return DingTalkCredentials(
        instance_id="dt-instance",
        organization_id="org-a",
        generation=generation,
        client_id="ding-app",
        client_secret="receiver-secret",
    )


def _message() -> dict[str, object]:
    return {
        "msgId": "dt-message-1",
        "eventId": "dt-event-1",
        "conversationId": "cid-1",
        "conversationType": "2",
        "senderStaffId": "staff-1",
        "senderNick": "Alice",
        "isInAtList": True,
        "msgtype": "text",
        "text": {"content": "hello"},
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=secret",
    }


def test_stream_ack_is_withheld_until_matching_custody_commit() -> None:
    async def scenario() -> None:
        transport = _Transport()
        routes = DingTalkReplyRouteStore(now_ms=lambda: 1_000)
        runtime = DingTalkStreamRuntime(_credentials(), transport, routes=routes)
        await runtime.start(_instance(), object())
        unit = runtime.receive_stream("delivery-1", _message())
        assert transport.acks == []

        with pytest.raises(DingTalkProtocolError, match="custody"):
            await runtime.commit_checkpoint(
                unit.checkpoint, CustodyReceipt("bad", "bad", 1_001)
            )
        assert transport.acks == []
        await runtime.commit_checkpoint(
            unit.checkpoint,
            CustodyReceipt(
                "journal-1", unit.envelope.observation.observation_digest, 1_002
            ),
        )
        assert transport.acks == []
        await runtime.commit_checkpoint(
            unit.checkpoint,
            CustodyReceipt(
                "journal-1", unit.envelope.observation.observation_digest, 1_002
            ),
        )
        assert transport.acks == []

    asyncio.run(scenario())


def test_session_webhook_is_short_lived_sealed_and_never_provenance() -> None:
    routes = DingTalkReplyRouteStore(now_ms=lambda: 1_000, ttl_ms=500)
    decoded = decode_dingtalk_message(
        _instance(), _message(), routes=routes, raw_source_ref="sealed:dt:1"
    )
    route_ref = decoded.provenance.session_webhook_ref
    assert route_ref is not None
    assert "session=secret" not in repr(decoded)
    assert routes.resolve(route_ref, instance_id="dt-instance", generation=4) == (
        "https://oapi.dingtalk.com/robot/sendBySession?session=secret"
    )
    routes.set_now_ms(lambda: 1_501)
    with pytest.raises(DingTalkProtocolError, match="expired"):
        routes.resolve(route_ref, instance_id="dt-instance", generation=4)


def test_group_alias_and_quote_provenance_are_preserved() -> None:
    message = _message()
    message["text"] = {
        "content": "answer",
        "isReplyMsg": True,
        "repliedMsg": {
            "msgId": "dt-parent",
            "senderStaffId": "staff-2",
            "msgtype": "text",
            "content": {"text": "question"},
        },
    }
    decoded = decode_dingtalk_message(
        _instance(), message, routes=DingTalkReplyRouteStore(), raw_source_ref="sealed:dt:2"
    )
    assert decoded.chat_kind == "group"
    assert decoded.addressed is True
    assert decoded.provenance.quote_message_id == "dt-parent"
    assert {alias.normalized_id for alias in decoded.aliases} >= {
        "dt-message-1",
        "dt-event-1",
    }


def test_generation_fence_precedes_sdk_start() -> None:
    async def scenario() -> None:
        transport = _Transport()
        runtime = DingTalkStreamRuntime(_credentials(generation=3), transport)
        with pytest.raises(DingTalkProtocolError, match="generation"):
            await runtime.start(_instance(generation=4), object())
        assert transport.started == 0

    asyncio.run(scenario())


def test_card_multistep_timeout_is_unknown_and_reconcile_only() -> None:
    create = classify_dingtalk_card_step(
        step="create", result={"outTrackId": "track-1"}, operation_started=True
    )
    assert create.state == "accepted"
    assert create.provider_message_id == "track-1"
    update = classify_dingtalk_card_step(
        step="update", result=TimeoutError(), operation_started=True, out_track_id="track-1"
    )
    assert update.state == "unknown"
    assert update.definitely_not_sent is False
    assert update.reason_code == "dingtalk-card-update-unknown:track-1"


def test_exact_sdk_0243_handler_returns_sdk_managed_ack_after_sink_custody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("dingtalk_stream", reason="install the adapter-dingtalk extra for SDK tests")

    async def scenario() -> None:
        from dingtalk_stream import CallbackMessage
        from dingtalk_stream.frames import Headers

        class AckMessage:
            STATUS_OK = 200

        class ChatbotHandler:
            pass

        class Client:
            def __init__(self, credential: object) -> None:
                self.handler: object | None = None

            def register_callback_handler(self, topic: str, handler: object) -> None:
                self.handler = handler

            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                return None

        sdk = SimpleNamespace(
            AckMessage=AckMessage,
            ChatbotHandler=ChatbotHandler,
            ChatbotMessage=SimpleNamespace(TOPIC="topic"),
            Credential=lambda client_id, secret: (client_id, secret),
            DingTalkStreamClient=Client,
        )
        real_import = importlib.import_module
        monkeypatch.setattr(
            importlib,
            "import_module",
            lambda name: sdk if name == "dingtalk_stream" else real_import(name),
        )

        class Receiver:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []

            async def ingest_stream(self, delivery_id: str, payload: object) -> None:
                self.calls.append((delivery_id, payload))

        receiver = Receiver()
        transport = dingtalk_provider.DingTalkSdkTransport()
        await transport.start(_credentials(), receiver)
        callback = CallbackMessage()
        assert isinstance(callback.headers, Headers)
        callback.headers.message_id = "delivery-1"
        callback.data = _message()
        result = await transport._client.handler.process(callback)
        assert result == (200, "OK")
        assert receiver.calls == [("delivery-1", _message())]
        await transport.stop()

    asyncio.run(scenario())


def test_dingtalk_protocols_routes_restart_and_authenticated_probe_are_closed() -> None:
    async def scenario() -> None:
        durable_routes: dict[str, object] = {}
        routes = DingTalkReplyRouteStore(now_ms=lambda: 1_000, state=durable_routes)
        transport = _Transport()
        sink = SimpleNamespace(staged=[])

        async def stage(unit: object) -> CustodyReceipt:
            sink.staged.append(unit)
            return CustodyReceipt(
                "journal-1", unit.envelope.observation.observation_digest, 1_001
            )

        sink.stage = stage
        runtime = DingTalkStreamRuntime(_credentials(), transport, routes=routes)
        await runtime.start(_instance(), sink)
        unit = await runtime.ingest_stream("delivery-1", _message())
        assert sink.staged == [unit]
        assert transport.acks == []
        restarted_routes = DingTalkReplyRouteStore(now_ms=lambda: 1_100, state=durable_routes)
        route_ref = dict(unit.envelope.payload)["sessionWebhookRef"]
        assert restarted_routes.resolve(
            route_ref, instance_id="dt-instance", generation=4
        ).startswith("https://oapi.dingtalk.com/")

        codec = dingtalk_provider.DingTalkCodec(_instance(), routes=routes)
        normalized = codec.normalize(await runtime.hydrate(unit.envelope))
        assert normalized.content == (TextBlock("text", "hello"),)
        assert await runtime.history(None, 10) == (unit,)
        target = AuthorizedChatTarget(
            "target-1",
            ChatRef("ref-1", _instance(), "fp-dt", "cid-1", "group", None, None),
            AudienceSnapshot("group", (), "complete", None, 1),
            "reply",
            "canonical-1",
        )
        resolution = await runtime.resolve(target)
        draft = codec.render(
            resolution, SimpleNamespace(content=(TextBlock("text", "reply"),))
        )
        template = dict(draft.nodes[0].template)
        assert template["sessionWebhookRef"] == route_ref
        operation = SimpleNamespace(
            sealed_request=freeze_json_v1(
                {
                    "instanceId": "dt-instance",
                    "generation": 4,
                    "organizationId": "org-a",
                    **template,
                }
            ),
            idempotency_key="idem-1",
        )
        outcome = await runtime.submit(operation)
        assert outcome.submissions[0].state == "accepted"
        assert transport.replies == [
            (
                "https://oapi.dingtalk.com/robot/sendBySession?session=secret",
                {"msgtype": "text", "text": {"content": "reply"}},
            )
        ]
        assert await runtime.reconcile(operation, outcome) is outcome
        with pytest.raises(DingTalkProtocolError, match="pull-unavailable"):
            await runtime.next_ingress()
        with pytest.raises(DingTalkProtocolError, match="callback-unavailable"):
            await runtime.accept_callback(
                VerifiedCallbackRequest("dt-instance", 4, "proof", {}, {})
            )
        await runtime.reconnect("test")
        assert (await runtime.health()).state == "online"

        authority = SimpleNamespace()

        async def resolve_credentials(_ref: str) -> DingTalkCredentials:
            return _credentials()

        authority.resolve = resolve_credentials
        probe = SimpleNamespace()

        async def probe_capabilities(_credentials: DingTalkCredentials) -> dict[str, object]:
            return {
                "authenticated": True,
                "facts": {
                    "dm-reply": {"support": "native", "permission": "granted", "limits": {}},
                    "card": {"support": "degraded", "permission": "unknown", "limits": {}},
                },
            }

        probe.probe_capabilities = probe_capabilities
        onboarding = dingtalk_provider.DingTalkOnboarding(
            authority, probe_transport=probe, dependency_available=True, now_ms=lambda: 1_000
        )
        identity = await onboarding.authenticate(
            OnboardingAttempt("attempt-1", {"variant": "stream"}, "cred")
        )
        fingerprint = await onboarding.fingerprint(identity)
        assert (fingerprint.instance_id, fingerprint.generation) == ("dt-instance", 4)
        assert (await onboarding.probe_capabilities(identity)).source == (
            "dingtalk-stream-authenticated-probe"
        )
        media = dingtalk_provider.DingTalkMedia()
        with pytest.raises(DingTalkProtocolError, match="media-fetch-unavailable"):
            await media.fetch(SealedProviderMediaRef("dt-instance", 4, "digest", "handle"), 10)
        with pytest.raises(DingTalkProtocolError, match="media-upload-unavailable"):
            await media.upload(operation, resolution, SimpleNamespace())
        assert (await runtime.close(2_000)).state == "closed"
        await onboarding.revoke(identity)
        with pytest.raises(DingTalkProtocolError, match="unrecognized"):
            await onboarding.fingerprint(identity)

    asyncio.run(scenario())


def test_dingtalk_onboarding_accepts_public_frozen_json_map() -> None:
    async def scenario() -> None:
        onboarding = dingtalk_provider.DingTalkOnboarding(
            SimpleNamespace(), dependency_available=True
        )
        config = FrozenJsonMap(
            (("credentialRef", "cred"), ("variant", "stream"))
        )
        assert (await onboarding.inspect(config)).state == "ready"

    asyncio.run(scenario())
