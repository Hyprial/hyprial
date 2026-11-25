from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

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
from hyprial.daemon.impl.adapters import weixin as weixin_provider
from hyprial.daemon.impl.adapters.weixin import (
    WeixinCredentials,
    WeixinProtocolError,
    WeixinRuntime,
    classify_weixin_submission,
    decode_weixin_event,
    normalize_weixin_api_base_url,
)


@dataclass
class _Sink:
    staged: list[object]

    async def stage(self, unit: object) -> CustodyReceipt:
        self.staged.append(unit)
        envelope = unit.envelope  # type: ignore[attr-defined]
        return CustodyReceipt("journal-1", envelope.observation.observation_digest, 100)


class _Transport:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.cursors: list[str] = []
        self.pages: list[dict[str, object]] = []

    async def notify_start(self, credentials: WeixinCredentials) -> None:
        self.started += 1

    async def notify_stop(self, credentials: WeixinCredentials) -> None:
        self.stopped += 1

    async def get_updates(
        self, credentials: WeixinCredentials, cursor: str, *, limit: int
    ) -> dict[str, object]:
        self.cursors.append(cursor)
        return self.pages.pop(0)

    async def send_message(
        self, credentials: WeixinCredentials, request: object
    ) -> dict[str, object]:
        return {"ret": 0}


class _HttpResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {"ret": 0}


class _HttpClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []

    async def post(self, url: str, **kwargs: object) -> _HttpResponse:
        body = kwargs.get("json")
        assert isinstance(body, dict)
        self.posts.append((url, body))
        return _HttpResponse()


def _instance(generation: int = 7) -> AdapterInstanceRef:
    return AdapterInstanceRef(
        "wx-instance", "weixin", "personal", "wx", "org-a", "fp-a", generation
    )


def _credentials(generation: int = 7) -> WeixinCredentials:
    return WeixinCredentials(
        instance_id="wx-instance",
        organization_id="org-a",
        generation=generation,
        account_id="wx-owner",
        token="receiver-owned-token",
        api_base_url="https://ilinkai.weixin.qq.com/",
    )


def test_base_url_is_https_and_confined_to_weixin_hosts() -> None:
    assert normalize_weixin_api_base_url("https://ilinkai.weixin.qq.com/api") == (
        "https://ilinkai.weixin.qq.com/api/"
    )
    for value in (
        "http://ilinkai.weixin.qq.com/",
        "https://ilinkai.weixin.qq.com.evil.invalid/",
        "https://user:pass@ilinkai.weixin.qq.com/",
        "https://ilinkai.weixin.qq.com:444/",
    ):
        with pytest.raises(WeixinProtocolError):
            normalize_weixin_api_base_url(value)


def test_poll_cursor_advances_only_after_matching_custody_receipt() -> None:
    async def scenario() -> None:
        transport = _Transport()
        transport.pages.append(
            {
                "ret": 0,
                "get_updates_buf": "cursor-2",
                "msgs": [
                    {
                        "seq": 2,
                        "message_id": "m-2",
                        "from_user_id": "u-2",
                        "to_user_id": "wx-owner",
                        "item_list": [{"type": "text", "text_item": {"text": "second"}}],
                    },
                    {
                        "seq": 1,
                        "message_id": "m-1",
                        "from_user_id": "u-1",
                        "to_user_id": "wx-owner",
                        "item_list": [{"type": "text", "text_item": {"text": "first"}}],
                    },
                ],
            }
        )
        runtime = WeixinRuntime(_credentials(), transport, initial_cursor="cursor-1")
        await runtime.start(_instance(), _Sink([]))

        first = await runtime.next_ingress()
        assert first.envelope.observation.aliases[0].normalized_id == "m-1"
        assert runtime.committed_cursor == "cursor-1"
        with pytest.raises(WeixinProtocolError, match="custody"):
            await runtime.commit_checkpoint(
                first.checkpoint, CustodyReceipt("wrong", "wrong-digest", 101)
            )
        assert runtime.committed_cursor == "cursor-1"

        await runtime.commit_checkpoint(
            first.checkpoint,
            CustodyReceipt(
                "journal-1", first.envelope.observation.observation_digest, 102
            ),
        )
        assert runtime.committed_cursor == "cursor-1"
        second = await runtime.next_ingress()
        await runtime.commit_checkpoint(
            second.checkpoint,
            CustodyReceipt(
                "journal-2", second.envelope.observation.observation_digest, 103
            ),
        )
        assert runtime.committed_cursor == "cursor-2"
        assert transport.cursors == ["cursor-1"]

    asyncio.run(scenario())


def test_generation_fence_precedes_provider_start() -> None:
    async def scenario() -> None:
        transport = _Transport()
        runtime = WeixinRuntime(_credentials(generation=6), transport)
        with pytest.raises(WeixinProtocolError, match="generation"):
            await runtime.start(_instance(generation=7), _Sink([]))
        assert transport.started == 0

    asyncio.run(scenario())


def test_decode_captures_aliases_provenance_reply_and_context_without_secret() -> None:
    decoded = decode_weixin_event(
        _instance(),
        {
            "seq": 9,
            "message_id": "m-9",
            "event_id": "e-9",
            "from_user_id": "u-9",
            "to_user_id": "wx-owner",
            "create_time_ms": 1_790_000_000_000,
            "context_token": "secret-context-token",
            "item_list": [
                {"type": "text", "text_item": {"text": "hello"}},
                {"type": "reply", "ref_msg": {"message_id": "m-8"}},
                {"type": "image", "image_item": {"media": {"encrypt_query_param": "q"}}},
            ],
        },
        raw_source_ref="sealed:wx:m-9",
    )
    assert {alias.alias_kind for alias in decoded.aliases} == {"event", "message", "cursor-item"}
    assert decoded.provenance.refs.message_id == "m-9"
    assert decoded.provenance.reply_message_id == "m-8"
    assert decoded.provenance.context_token_ref.startswith("sealed-context:")
    assert "secret-context-token" not in repr(decoded)
    assert decoded.text == "hello"


def test_submission_classification_keeps_timeout_unknown_and_rejection_definite() -> None:
    accepted = classify_weixin_submission(
        {"ret": 0, "message_id": "native-1"}, operation_started=True
    )
    assert (accepted.state, accepted.provider_message_id) == ("accepted", "native-1")

    unknown = classify_weixin_submission(TimeoutError(), operation_started=True)
    assert (unknown.state, unknown.definitely_not_sent) == ("unknown", False)

    rejected = classify_weixin_submission(
        {"ret": 40003, "errmsg": "invalid target"}, operation_started=False
    )
    assert (rejected.state, rejected.definitely_not_sent) == ("failed-final", True)
    assert rejected.reason_code == "weixin-provider-40003"


def test_runtime_binds_operation_key_without_fabricating_provider_acceptance() -> None:
    async def scenario() -> None:
        client = _HttpClient()
        transport = weixin_provider.HttpxWeixinTransport(client)
        runtime = WeixinRuntime(_credentials(), transport)
        await runtime.start(_instance(), _Sink([]))
        operation = SimpleNamespace(
            sealed_request=freeze_json_v1(
                {
                    "instanceId": "wx-instance",
                    "generation": 7,
                    "organizationId": "org-a",
                    "toUserId": "u-1",
                    "text": "reply",
                }
            ),
            idempotency_key="operation-idem-1",
        )

        outcome = await runtime.submit(operation)

        send_calls = [call for call in client.posts if call[0].endswith("/sendmessage")]
        assert send_calls == [
            (
                "https://ilinkai.weixin.qq.com/ilink/bot/sendmessage",
                {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": "u-1",
                        "client_id": "operation-idem-1",
                        "message_type": 2,
                        "message_state": 2,
                        "item_list": [{"type": 1, "text_item": {"text": "reply"}}],
                    },
                    "base_info": {"channel_version": "2.4.6"},
                },
            )
        ]
        submission = outcome.submissions[0]
        assert (submission.state, submission.provider_message_id) == ("unknown", None)
        assert submission.reason_code == "weixin-missing-message-id"

    asyncio.run(scenario())


def test_local_weixin_send_preflight_is_definitely_not_sent() -> None:
    async def scenario() -> None:
        client = _HttpClient()
        runtime = WeixinRuntime(
            _credentials(), weixin_provider.HttpxWeixinTransport(client)
        )
        await runtime.start(_instance(), _Sink([]))
        operation = SimpleNamespace(
            sealed_request=freeze_json_v1(
                {
                    "instanceId": "wx-instance",
                    "generation": 7,
                    "organizationId": "org-a",
                    "toUserId": "u-1",
                }
            ),
            idempotency_key="operation-idem-2",
        )

        outcome = await runtime.submit(operation)

        submission = outcome.submissions[0]
        assert (submission.state, submission.definitely_not_sent) == (
            "failed-retryable",
            True,
        )
        assert submission.reason_code == "weixin-submit-not-started"
        assert not any(url.endswith("/sendmessage") for url, _body in client.posts)

    asyncio.run(scenario())


def test_weixin_onboarding_accepts_public_frozen_json_map() -> None:
    async def scenario() -> None:
        onboarding = weixin_provider.WeixinOnboarding(SimpleNamespace())
        config = FrozenJsonMap(
            (("credentialRef", "cred"), ("variant", "personal"))
        )
        assert (await onboarding.inspect(config)).state == "ready"

    asyncio.run(scenario())


def test_body_bound_is_checked_before_payload_is_frozen() -> None:
    body = {"message_id": "m", "from_user_id": "u", "padding": "x" * (256 * 1024)}
    with pytest.raises(WeixinProtocolError, match="payload"):
        decode_weixin_event(_instance(), body, raw_source_ref="sealed:oversize")
    assert hashlib.sha256(body["padding"].encode()).hexdigest()


def test_all_weixin_public_protocol_methods_are_consumable_and_honest() -> None:
    async def scenario() -> None:
        codec = weixin_provider.WeixinCodec(_instance())
        media = weixin_provider.WeixinMedia()
        authority = SimpleNamespace(resolve=lambda _ref: None)

        async def resolve(_ref: str) -> WeixinCredentials:
            return _credentials()

        authority.resolve = resolve
        probe = SimpleNamespace()

        async def probe_capabilities(_credentials: WeixinCredentials) -> dict[str, object]:
            return {
                "authenticated": True,
                "facts": {
                    "dm-reply": {"support": "native", "permission": "granted", "limits": {}},
                    "proactive-send": {
                        "support": "unknown",
                        "permission": "unknown",
                        "limits": {},
                        "reasonCode": "provider-evidence-required",
                    },
                },
            }

        probe.probe_capabilities = probe_capabilities
        onboarding = weixin_provider.WeixinOnboarding(
            authority, login_transport=probe, now_ms=lambda: 1_000
        )
        assert (await onboarding.inspect({"variant": "personal", "credentialRef": "cred"})).state == "ready"
        identity = await onboarding.authenticate(
            OnboardingAttempt("attempt-1", {"variant": "personal"}, "cred")
        )
        fingerprint = await onboarding.fingerprint(identity)
        assert (fingerprint.instance_id, fingerprint.generation) == ("wx-instance", 7)
        capability = await onboarding.probe_capabilities(identity)
        assert capability.source == "weixin-ilink-authenticated-probe"

        transport = _Transport()
        transport.pages.append(
            {
                "ret": 0,
                "get_updates_buf": "cursor-2",
                "msgs": [{
                    "seq": 1,
                    "message_id": "m-1",
                    "from_user_id": "u-1",
                    "to_user_id": "wx-owner",
                    "item_list": [{"type": "text", "text_item": {"text": "hello"}}],
                }],
            }
        )
        runtime = WeixinRuntime(_credentials(), transport, initial_cursor="cursor-1")
        await runtime.start(_instance(), _Sink([]))
        unit = await runtime.next_ingress()
        hydrated = await runtime.hydrate(unit.envelope)
        normalized = codec.normalize(hydrated)
        assert normalized.content == (TextBlock("text", "hello"),)
        assert await runtime.history(None, 10) == (unit,)
        target = AuthorizedChatTarget(
            "target-1",
            ChatRef("ref-1", _instance(), "fp-a", "u-1", "dm", None, None),
            AudienceSnapshot("private", (), "complete", None, 1),
            "reply",
            "canonical-1",
        )
        resolution = await runtime.resolve(target)
        draft = codec.render(resolution, SimpleNamespace(content=(TextBlock("text", "reply"),)))
        assert draft.nodes[0].template
        operation = SimpleNamespace(
            sealed_request=freeze_json_v1(
                {
                    "instanceId": "wx-instance",
                    "generation": 7,
                    "organizationId": "org-a",
                    "toUserId": "u-1",
                    "text": "reply",
                }
            ),
            idempotency_key="idem-1",
        )
        outcome = await runtime.submit(operation)
        assert outcome.submissions[0].state == "unknown"
        assert await runtime.reconcile(operation, outcome) is outcome
        await runtime.commit_checkpoint(
            unit.checkpoint,
            CustodyReceipt("journal-1", unit.envelope.observation.observation_digest, 1_001),
        )
        await runtime.reconnect("test")
        assert (await runtime.health()).state == "online"
        with pytest.raises(WeixinProtocolError, match="callback-unavailable"):
            await runtime.accept_callback(
                VerifiedCallbackRequest("wx-instance", 7, "proof", {}, {})
            )
        with pytest.raises(WeixinProtocolError, match="media-fetch-unavailable"):
            await media.fetch(SealedProviderMediaRef("wx-instance", 7, "digest", "handle"), 10)
        with pytest.raises(WeixinProtocolError, match="media-upload-unavailable"):
            await media.upload(operation, resolution, SimpleNamespace())
        assert (await runtime.close(2_000)).state == "closed"
        await onboarding.revoke(identity)
        with pytest.raises(WeixinProtocolError, match="unrecognized"):
            await onboarding.fingerprint(identity)

    asyncio.run(scenario())
