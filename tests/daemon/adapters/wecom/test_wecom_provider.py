from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import os
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters import wecom as wecom_provider
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
from hyprial.daemon.impl.adapters.wecom import (
    WeComApplicationCredentials,
    WeComApplicationRuntime,
    WeComBotCredentials,
    WeComBotRuntime,
    WeComCallbackCrypto,
    WeComProtocolError,
    classify_wecom_application_send,
    decode_wecom_bot_frame,
)


class _BotTransport:
    def __init__(self) -> None:
        self.connected = 0
        self.acks: list[str] = []

    async def connect(self, credentials: WeComBotCredentials, receiver: object) -> None:
        self.connected += 1
        self.receiver = receiver

    async def disconnect(self) -> None:
        return None

    async def ack(self, req_id: str) -> None:
        self.acks.append(req_id)

    async def send(self, request: object) -> dict[str, object]:
        return {"headers": {"req_id": "wc-out-1"}, "errcode": 0}


class _AppTransport:
    def __init__(self) -> None:
        self.token_requests = 0
        self.sends: list[tuple[str, dict[str, object]]] = []

    async def get_access_token(self, corp_id: str, corp_secret: str) -> dict[str, object]:
        self.token_requests += 1
        return {"errcode": 0, "access_token": f"access-{self.token_requests}", "expires_in": 7200}

    async def send(self, access_token: str, payload: dict[str, object]) -> dict[str, object]:
        self.sends.append((access_token, payload))
        return {"errcode": 0, "msgid": "wc-out-1"}


def _bot_instance(generation: int = 3) -> AdapterInstanceRef:
    return AdapterInstanceRef(
        "wc-instance", "wecom", "bot", "wc", "org-a", "fp-wc", generation
    )


def _app_instance(generation: int = 8) -> AdapterInstanceRef:
    return AdapterInstanceRef(
        "wca-instance", "wecom", "application", "wca", "org-a", "fp-wca", generation
    )


def test_bot_and_application_credentials_are_closed_non_interchangeable_variants() -> None:
    bot = WeComBotCredentials(
        "wc-instance", "org-a", 3, "bot-id", "bot-secret"
    )
    app = WeComApplicationCredentials(
        "wca-instance",
        "org-a",
        8,
        "corp-id",
        "100001",
        "corp-secret",
        "callback-token",
        base64.b64encode(os.urandom(32)).decode().rstrip("="),
    )
    with pytest.raises(TypeError, match="bot credentials"):
        WeComBotRuntime(app, _BotTransport())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="application credentials"):
        WeComApplicationRuntime(bot, _AppTransport())  # type: ignore[arg-type]


def test_bot_ack_waits_for_custody_and_req_id_order_is_monotonic() -> None:
    async def scenario() -> None:
        transport = _BotTransport()
        credentials = WeComBotCredentials(
            "wc-instance", "org-a", 3, "bot-id", "bot-secret"
        )
        runtime = WeComBotRuntime(credentials, transport)
        await runtime.start(_bot_instance(), object())
        second = runtime.receive_frame(
            {"cmd": "aibot_msg_callback", "headers": {"req_id": "r-2"}, "body": _bot_body("m-2")}
        )
        first = runtime.receive_frame(
            {"cmd": "aibot_msg_callback", "headers": {"req_id": "r-1"}, "body": _bot_body("m-1")}
        )
        assert runtime.pending_units() == (second, first)
        assert transport.acks == []
        await runtime.commit_checkpoint(
            second.checkpoint,
            CustodyReceipt("j-2", second.envelope.observation.observation_digest, 10),
        )
        assert transport.acks == []
        await runtime.commit_checkpoint(
            first.checkpoint,
            CustodyReceipt("j-1", first.envelope.observation.observation_digest, 11),
        )
        assert transport.acks == []

    asyncio.run(scenario())


def _bot_body(message_id: str) -> dict[str, object]:
    return {
        "msgid": message_id,
        "chatid": "chat-1",
        "chattype": "single",
        "from": {"userid": "user-1"},
        "msgtype": "text",
        "text": {"content": "hello"},
    }


def test_bot_decode_preserves_req_chat_sender_and_rejects_secret_fields() -> None:
    decoded = decode_wecom_bot_frame(
        _bot_instance(),
        {"cmd": "aibot_msg_callback", "headers": {"req_id": "r-1"}, "body": _bot_body("m-1")},
        raw_source_ref="sealed:wc:m-1",
    )
    assert decoded.provenance.req_id == "r-1"
    assert decoded.provenance.chat_id == "chat-1"
    assert decoded.provenance.sender_id == "user-1"
    bad = _bot_body("m-2")
    bad["access_token"] = "leak"
    with pytest.raises(WeComProtocolError, match="secret"):
        decode_wecom_bot_frame(
            _bot_instance(),
            {"cmd": "aibot_msg_callback", "headers": {"req_id": "r-2"}, "body": bad},
            raw_source_ref="sealed:wc:m-2",
        )


def test_application_callback_signature_decryption_replay_and_generation_fence() -> None:
    key = base64.b64encode(bytes(range(32))).decode().rstrip("=")
    crypto = WeComCallbackCrypto("callback-token", key, "corp-id")
    plaintext = b"<xml><FromUserName>user-1</FromUserName><MsgId>42</MsgId></xml>"
    encrypted = crypto.encrypt_for_test(plaintext, random_prefix=b"0" * 16)
    signature = crypto.signature("1790000000", "nonce-1", encrypted)
    assert crypto.verify_and_decrypt(
        timestamp="1790000000", nonce="nonce-1", encrypted=encrypted, signature=signature
    ) == plaintext
    with pytest.raises(WeComProtocolError, match="replay"):
        crypto.verify_and_decrypt(
            timestamp="1790000000", nonce="nonce-1", encrypted=encrypted, signature=signature
        )
    with pytest.raises(WeComProtocolError, match="signature"):
        WeComCallbackCrypto("callback-token", key, "corp-id").verify_and_decrypt(
            timestamp="1790000000", nonce="nonce-1", encrypted=encrypted, signature="0" * 40
        )

    credentials = WeComApplicationCredentials(
        "wca-instance", "org-a", 7, "corp-id", "100001", "corp-secret", "callback-token", key
    )
    runtime = WeComApplicationRuntime(credentials, _AppTransport())
    with pytest.raises(WeComProtocolError, match="generation"):
        asyncio.run(runtime.start(_app_instance(generation=8), object()))


def test_application_token_cache_retries_invalid_token_once_only() -> None:
    class ExpiringTransport(_AppTransport):
        async def send(self, access_token: str, payload: dict[str, object]) -> dict[str, object]:
            self.sends.append((access_token, payload))
            if len(self.sends) == 1:
                return {"errcode": 42001, "errmsg": "token expired"}
            return {"errcode": 0, "msgid": "wc-out-2"}

    async def scenario() -> None:
        key = base64.b64encode(os.urandom(32)).decode().rstrip("=")
        transport = ExpiringTransport()
        credentials = WeComApplicationCredentials(
            "wca-instance", "org-a", 8, "corp-id", "100001", "corp-secret", "token", key
        )
        runtime = WeComApplicationRuntime(
            credentials,
            transport,
            now_ms=lambda: 1_000,
            trusted_source_ips=("127.0.0.1",),
        )
        await runtime.start(_app_instance(), object())
        result = await runtime.send_text("user-1", "hello", idempotency_key="idem-1")
        assert result.state == "accepted"
        assert transport.token_requests == 2
        assert [token for token, _ in transport.sends] == ["access-1", "access-2"]

    asyncio.run(scenario())


def test_application_callback_is_journal_safe_and_replay_idempotent() -> None:
    async def scenario() -> None:
        key = base64.b64encode(bytes(range(32))).decode().rstrip("=")
        credentials = WeComApplicationCredentials(
            "wca-instance", "org-a", 8, "corp-id", "100001", "corp-secret", "token", key
        )
        runtime = WeComApplicationRuntime(
            credentials, _AppTransport(), now_ms=lambda: 1_000, trusted_source_ips=("127.0.0.1",)
        )
        await runtime.start(_app_instance(), object())
        plaintext = (
            b"<xml><FromUserName><![CDATA[user-1]]></FromUserName>"
            b"<MsgId>42</MsgId><MsgType><![CDATA[text]]></MsgType>"
            b"<Content><![CDATA[hello]]></Content></xml>"
        )
        encrypted = runtime.crypto.encrypt_for_test(plaintext, random_prefix=b"1" * 16)
        signature = runtime.crypto.signature("1000", "nonce", encrypted)
        unit = runtime.accept_encrypted_callback(
            timestamp="1000",
            nonce="nonce",
            encrypted=encrypted,
            signature=signature,
            source_ip="127.0.0.1",
        )
        replay = runtime.accept_encrypted_callback(
            timestamp="1000",
            nonce="nonce",
            encrypted=encrypted,
            signature=signature,
            source_ip="127.0.0.1",
        )
        assert replay is unit
        assert encrypted not in repr(unit.envelope.payload)
        with pytest.raises(WeComProtocolError, match="custody"):
            await runtime.commit_checkpoint(
                unit.checkpoint, CustodyReceipt("bad", "bad", 1_001)
            )
        await runtime.commit_checkpoint(
            unit.checkpoint,
            CustodyReceipt("journal-1", unit.envelope.observation.observation_digest, 1_002),
        )
        assert runtime.callback_committed(unit.checkpoint) is True

    asyncio.run(scenario())


def test_application_send_timeout_after_request_is_unknown_not_retryable_success() -> None:
    result = classify_wecom_application_send(TimeoutError(), operation_started=True)
    assert result.state == "unknown"
    assert result.definitely_not_sent is False
    assert result.provider_message_id is None
    assert hashlib.sha256(result.reason_code.encode()).hexdigest()


def test_exact_wecom_sdk_102_dict_frame_and_no_manual_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        class Client:
            def __init__(self, options: object) -> None:
                self.handlers: dict[str, object] = {}

            def on(self, name: str, handler: object) -> object:
                self.handlers[name] = handler
                return handler

            async def connect(self) -> None:
                return None

            def disconnect(self) -> None:
                return None

            async def reply(self, frame: dict[str, object], body: dict[str, object]) -> dict[str, object]:
                assert frame["headers"] == {"req_id": "r-1"}
                return {"headers": {"req_id": "r-1"}, "errcode": 0}

            async def send_message(self, chat_id: str, body: dict[str, object]) -> dict[str, object]:
                return {"headers": {"req_id": "send-1"}, "errcode": 0}

        sdk = SimpleNamespace(
            WSClient=Client,
            WSClientOptions=lambda **values: values,
        )
        real_import = importlib.import_module
        monkeypatch.setattr(
            importlib,
            "import_module",
            lambda name: sdk if name == "aibot" else real_import(name),
        )

        class Receiver:
            def __init__(self) -> None:
                self.frames: list[dict[str, object]] = []

            async def ingest_frame(self, frame: dict[str, object]) -> None:
                self.frames.append(frame)

        receiver = Receiver()
        transport = wecom_provider.WeComSdkBotTransport()
        credentials = WeComBotCredentials("wc-instance", "org-a", 3, "bot-id", "secret")
        await transport.connect(credentials, receiver)
        frame = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r-1"},
            "body": _bot_body("m-1"),
        }
        await transport._client.handlers["message"](frame)
        assert receiver.frames == [frame]
        assert not hasattr(transport._client, "ack")
        reply = await transport.send({"body": {"msgtype": "text"}, "replyReqId": "r-1"})
        assert reply["headers"] == {"req_id": "r-1"}
        await transport.disconnect()

    asyncio.run(scenario())


def test_wecom_onboarding_requires_true_trusted_ip_and_authenticated_probe() -> None:
    async def scenario() -> None:
        authority = SimpleNamespace()
        bot_credentials = WeComBotCredentials("wc-instance", "org-a", 3, "bot-id", "secret")
        app_credentials = WeComApplicationCredentials(
            "wca-instance",
            "org-a",
            8,
            "corp-id",
            "100001",
            "corp-secret",
            "callback-token",
            base64.b64encode(bytes(range(32))).decode().rstrip("="),
        )

        async def resolve(
            credential_ref: str,
        ) -> WeComBotCredentials | WeComApplicationCredentials:
            return app_credentials if credential_ref == "app-cred" else bot_credentials

        authority.resolve = resolve
        probe = SimpleNamespace()

        async def probe_capabilities(_credentials: object) -> dict[str, object]:
            return {
                "authenticated": True,
                "facts": {
                    "dm-reply": {"support": "native", "permission": "granted", "limits": {}},
                    "file": {"support": "native", "permission": "granted", "limits": {"maxBytes": 10485760}},
                },
            }

        probe.probe_capabilities = probe_capabilities
        onboarding = wecom_provider.WeComOnboarding(
            authority,
            bot_probe=probe,
            application_probe=probe,
            dependency_available=True,
            now_ms=lambda: 1_000,
        )
        blocked = await onboarding.inspect(
            {
                "variant": "application",
                "credentialRef": "cred",
                "corpId": "corp-id",
                "agentId": "100001",
                "callbackBase": "https://callback.invalid/",
                "trustedIpConfigured": False,
            }
        )
        assert blocked.state == "blocked"
        assert (await onboarding.inspect({"variant": "bot", "credentialRef": "cred"})).state == "ready"
        identity = await onboarding.authenticate(
            OnboardingAttempt("attempt-1", {"variant": "bot"}, "cred")
        )
        fingerprint = await onboarding.fingerprint(identity)
        assert (fingerprint.instance_id, fingerprint.generation) == ("wc-instance", 3)
        snapshot = await onboarding.probe_capabilities(identity)
        assert snapshot.source == "wecom-bot-authenticated-probe"
        app_identity = await onboarding.authenticate(
            OnboardingAttempt("attempt-2", {"variant": "application"}, "app-cred")
        )
        app_fingerprint = await onboarding.fingerprint(app_identity)
        assert (app_fingerprint.instance_id, app_fingerprint.variant) == (
            "wca-instance",
            "application",
        )
        assert (await onboarding.probe_capabilities(app_identity)).source == (
            "wecom-application-authenticated-probe"
        )
        await onboarding.revoke(identity)
        with pytest.raises(WeComProtocolError, match="unrecognized"):
            await onboarding.fingerprint(identity)

        async def unauthenticated_probe(_credentials: object) -> dict[str, object]:
            return {"authenticated": False, "facts": {}}

        rejected_probe = SimpleNamespace(probe_capabilities=unauthenticated_probe)
        rejected = wecom_provider.WeComOnboarding(
            authority,
            bot_probe=rejected_probe,
            dependency_available=True,
            now_ms=lambda: 1_000,
        )
        rejected_identity = await rejected.authenticate(
            OnboardingAttempt("attempt-3", {"variant": "bot"}, "cred")
        )
        with pytest.raises(WeComProtocolError, match="unauthenticated"):
            await rejected.probe_capabilities(rejected_identity)

    asyncio.run(scenario())


def test_wecom_onboarding_accepts_public_frozen_json_map() -> None:
    async def scenario() -> None:
        onboarding = wecom_provider.WeComOnboarding(
            SimpleNamespace(), dependency_available=True
        )
        config = FrozenJsonMap(
            (("credentialRef", "cred"), ("variant", "bot"))
        )
        assert (await onboarding.inspect(config)).state == "ready"

    asyncio.run(scenario())


def test_wecom_bot_and_application_close_all_runtime_codec_media_methods() -> None:
    async def scenario() -> None:
        sink = SimpleNamespace(staged=[])

        async def stage(unit: object) -> CustodyReceipt:
            sink.staged.append(unit)
            return CustodyReceipt(
                f"journal-{len(sink.staged)}",
                unit.envelope.observation.observation_digest,
                1_001,
            )

        sink.stage = stage
        bot_transport = _BotTransport()
        bot = WeComBotRuntime(
            WeComBotCredentials("wc-instance", "org-a", 3, "bot-id", "secret"),
            bot_transport,
        )
        await bot.start(_bot_instance(), sink)
        bot_unit = await bot.ingest_frame(
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "r-1"},
                "body": _bot_body("m-1"),
            }
        )
        assert sink.staged == [bot_unit]
        assert await bot.history(None, 10) == (bot_unit,)
        bot_codec = wecom_provider.WeComBotCodec(_bot_instance())
        assert bot_codec.normalize(await bot.hydrate(bot_unit.envelope)).content == (
            TextBlock("text", "hello"),
        )
        target = AuthorizedChatTarget(
            "target-1",
            ChatRef("ref-1", _bot_instance(), "fp-wc", "chat-1", "dm", None, None),
            AudienceSnapshot("private", (), "complete", None, 1),
            "reply",
            "canonical-1",
        )
        resolution = await bot.resolve(target)
        assert bot_codec.render(
            resolution, SimpleNamespace(content=(TextBlock("text", "reply"),))
        ).nodes
        bot_operation = SimpleNamespace(
            sealed_request=freeze_json_v1(
                {
                    "instanceId": "wc-instance",
                    "generation": 3,
                    "organizationId": "org-a",
                    "chatId": "chat-1",
                    "body": {"msgtype": "text", "text": {"content": "reply"}},
                }
            ),
            idempotency_key="idem-bot",
        )
        bot_outcome = await bot.submit(bot_operation)
        assert bot_outcome.submissions[0].state == "accepted"
        assert await bot.reconcile(bot_operation, bot_outcome) is bot_outcome
        with pytest.raises(WeComProtocolError, match="pull-unavailable"):
            await bot.next_ingress()
        with pytest.raises(WeComProtocolError, match="callback-unavailable"):
            await bot.accept_callback(
                VerifiedCallbackRequest("wc-instance", 3, "proof", {}, {})
            )
        await bot.reconnect("test")
        assert (await bot.health()).state == "online"
        assert (await bot.close(2_000)).state == "closed"

        key = base64.b64encode(bytes(range(32))).decode().rstrip("=")
        app = WeComApplicationRuntime(
            WeComApplicationCredentials(
                "wca-instance", "org-a", 8, "corp-id", "100001", "secret", "token", key
            ),
            _AppTransport(),
            now_ms=lambda: 1_000,
            trusted_source_ips=("127.0.0.1",),
        )
        await app.start(_app_instance(), sink)
        plaintext = (
            b"<xml><FromUserName>user-1</FromUserName><MsgId>42</MsgId>"
            b"<MsgType>text</MsgType><Content>hello</Content></xml>"
        )
        encrypted = app.crypto.encrypt_for_test(plaintext, random_prefix=b"2" * 16)
        signature = app.crypto.signature("1000", "nonce", encrypted)
        app_unit = await app.accept_callback(
            VerifiedCallbackRequest(
                "wca-instance",
                8,
                "verified-ingress",
                {"timestamp": "1000", "nonce": "nonce", "signature": signature, "sourceIp": "127.0.0.1"},
                {"encrypt": encrypted},
            )
        )
        assert (await app.history(None, 10)) == (app_unit,)
        app_codec = wecom_provider.WeComApplicationCodec(_app_instance())
        assert app_codec.normalize(await app.hydrate(app_unit.envelope)).content == (
            TextBlock("text", "hello"),
        )
        app_target = AuthorizedChatTarget(
            "target-2",
            ChatRef("ref-2", _app_instance(), "fp-wca", "user-1", "dm", None, None),
            AudienceSnapshot("private", (), "complete", None, 1),
            "reply",
            "canonical-2",
        )
        app_resolution = await app.resolve(app_target)
        assert app_resolution.status == "resolved"
        assert app_codec.render(
            app_resolution, SimpleNamespace(content=(TextBlock("text", "reply"),))
        ).nodes
        app_operation = SimpleNamespace(
            sealed_request=freeze_json_v1(
                {
                    "instanceId": "wca-instance",
                    "generation": 8,
                    "organizationId": "org-a",
                    "toUserId": "user-1",
                    "text": "reply",
                }
            ),
            idempotency_key="idem-app",
        )
        app_outcome = await app.submit(app_operation)
        assert app_outcome.submissions[0].state == "accepted"
        assert await app.reconcile(app_operation, app_outcome) is app_outcome
        with pytest.raises(WeComProtocolError, match="pull-unavailable"):
            await app.next_ingress()
        await app.reconnect("test")
        assert (await app.health()).state == "online"
        media = wecom_provider.WeComMedia("bot")
        with pytest.raises(WeComProtocolError, match="media-fetch-unavailable"):
            await media.fetch(SealedProviderMediaRef("wc-instance", 3, "digest", "handle"), 10)
        with pytest.raises(WeComProtocolError, match="media-upload-unavailable"):
            await media.upload(SimpleNamespace(), resolution, SimpleNamespace())
        app_media = wecom_provider.WeComMedia("application")
        with pytest.raises(WeComProtocolError, match="media-fetch-unavailable"):
            await app_media.fetch(
                SealedProviderMediaRef("wca-instance", 8, "digest", "handle"), 10
            )
        with pytest.raises(WeComProtocolError, match="media-upload-unavailable"):
            await app_media.upload(app_operation, app_resolution, SimpleNamespace())
        assert (await app.close(2_000)).state == "closed"

    asyncio.run(scenario())
