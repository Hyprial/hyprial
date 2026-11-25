from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AudienceSnapshot,
    AuthorizedChatTarget,
    ChatRef,
    CustodyReceipt,
    HydratedEvent,
    NativeTargetResolution,
    OnboardingAttempt,
    TextBlock,
    frozen_json_digest_v1,
    freeze_json_v1,
)
from hyprial.daemon.impl.adapters.telegram import (
    TelegramBotRuntime,
    TelegramCredentials,
    TelegramEventCodec,
    TelegramOnboarding,
)
from hyprial.daemon.impl.adapters.telegram.errors import TelegramPollRetry
from hyprial.daemon.impl.adapters.telegram.transport import TelegramSendOutcome
from hyprial.daemon.impl.adapters.common.transport import (
    HttpEffectObservation,
    HttpEffectState,
    HttpRequestPhase,
    ResourceCloseOutcome,
    ResourceCloseState,
)


class _Ledger:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    async def load(self, instance_id: str, generation: int) -> str | None:
        return self.value

    async def commit(self, instance_id: str, generation: int, token: str) -> None:
        self.value = token


class _Credentials:
    async def resolve(self, credential_ref: str) -> TelegramCredentials:
        assert credential_ref == "receiver:telegram"
        return TelegramCredentials("123456:" + "A" * 35)


class _Transport:
    def __init__(self, updates: list[object]) -> None:
        self.updates = updates
        self.offsets: list[int] = []
        self.calls: list[tuple[str, object]] = []
        self.outcome: object = {"ok": True, "result": {"message_id": 81}}
        self.close_outcome = ResourceCloseOutcome(
            ResourceCloseState.CLOSED, None, False, False, None
        )
        self.auth_calls = 0

    async def get_me(self, credentials: TelegramCredentials) -> object:
        self.auth_calls += 1
        return {"ok": True, "result": {"id": 123456, "username": "hyprial_bot"}}

    async def get_webhook_info(self, credentials: TelegramCredentials) -> object:
        return {"ok": True, "result": {"url": "", "pending_update_count": 0}}

    async def get_updates(self, credentials: TelegramCredentials, *, offset: int, limit: int) -> object:
        self.offsets.append(offset)
        return {"ok": True, "result": self.updates[:limit]}

    async def send_message(self, credentials: TelegramCredentials, payload: object) -> TelegramSendOutcome:
        self.calls.append(("sendMessage", payload))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        response = self.outcome
        if isinstance(response, dict) and response.get("ok") is True:
            message_id = str(response["result"]["message_id"])
            effect = HttpEffectObservation(
                HttpRequestPhase.RESPONSE_RECEIVED,
                HttpEffectState.ACCEPTED,
                False,
                200,
                message_id,
                None,
                "telegram-send-accepted",
            )
            return TelegramSendOutcome(effect, None)
        if isinstance(response, dict) and response.get("error_code") == 429:
            effect = HttpEffectObservation(
                HttpRequestPhase.RESPONSE_RECEIVED,
                HttpEffectState.RETRYABLE,
                True,
                429,
                None,
                None,
                "telegram-rate-limited",
            )
            return TelegramSendOutcome(effect, response["parameters"]["retry_after"] * 1000)
        raise AssertionError(response)

    async def aclose(self, deadline_ms: int) -> ResourceCloseOutcome:
        return self.close_outcome


def _instance() -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(b"telegram\0telegram-cloud\0" + b"123456").hexdigest()
    return AdapterInstanceRef("telegram-1", "telegram", "bot", "main", "org-1", fingerprint, 4)


def _operation(**overrides: object) -> SimpleNamespace:
    request = {
        "provider": "telegram",
        "organization_id": "org-1",
        "instance_id": "telegram-1",
        "generation": 4,
        "receiver_ref": "receiver:telegram",
        "method": "sendMessage",
        "chat_id": "-1001",
        "text": "hello",
        "part_index": 0,
        **overrides,
    }
    return SimpleNamespace(sealed_request=freeze_json_v1(request), kind="text-send", idempotency_key="tg-1")


def _reply_target() -> NativeTargetResolution:
    chat = ChatRef("chat-1", _instance(), "123456", "-1001", "thread", "77", "77")
    target = AuthorizedChatTarget(
        "target-1", chat, AudienceSnapshot("group", (), "unknown", None, 1), "reply", "8"
    )
    return NativeTargetResolution(target, "resolved", "-1001", "77", None)


def test_poll_offset_advances_only_after_custody_and_codec_preserves_topic_quote_files() -> None:
    async def scenario() -> None:
        update = {
            "update_id": 40,
            "message": {
                "message_id": 9,
                "date": 1710000000,
                "message_thread_id": 77,
                "chat": {"id": -1001, "type": "supergroup", "title": "ops"},
                "from": {"id": 55, "first_name": "Ada"},
                "text": "hello",
                "reply_to_message": {"message_id": 8, "text": "prior", "from": {"id": 56}},
                "document": {"file_id": "doc-1", "file_unique_id": "uniq-1", "file_name": "a.txt", "file_size": 3},
            },
        }
        ledger = _Ledger("40")
        transport = _Transport([update])
        runtime = TelegramBotRuntime(transport, ledger, _Credentials(), "receiver:telegram")
        await runtime.start(_instance(), SimpleNamespace())
        unit = await runtime.next_ingress()
        assert transport.offsets == [40]
        assert unit.checkpoint.token == "41"
        assert ledger.value == "40"
        message_alias = next(
            alias
            for alias in unit.envelope.observation.aliases
            if alias.alias_kind == "message"
        )
        assert (
            message_alias.platform,
            message_alias.account_scope,
            message_alias.chat_scope,
            message_alias.normalized_id,
        ) == ("telegram", "123456", "-1001", "9")
        await runtime.commit_checkpoint(
            unit.checkpoint,
            CustodyReceipt("j-1", frozen_json_digest_v1(unit.envelope.payload), 1),
        )
        assert ledger.value == "41"
        normalized, platform = TelegramEventCodec(_instance(), "123456").decode(
            HydratedEvent(unit.envelope, unit.envelope.payload)
        )
        assert platform.update_id == 40
        assert platform.file_ids == ("doc-1",)
        assert normalized.context.source_chat.native_thread_id == "77"
        assert normalized.context.channel_id.startswith("adapter:telegram:")
        assert normalized.reply_to is not None
        assert normalized.attachments[0].handle_id.endswith(":doc-1")
        await runtime.reconnect("long-poll-refresh")
        assert transport.auth_calls == 2

    asyncio.run(scenario())


def test_restart_reuses_persisted_next_offset_and_render_keeps_reply_parameters() -> None:
    async def scenario() -> None:
        update = {
            "update_id": 41,
            "message": {
                "message_id": 10,
                "date": 1710000001,
                "chat": {"id": -1001, "type": "supergroup"},
                "from": {"id": 55},
                "text": "next",
            },
        }
        transport = _Transport([update])
        runtime = TelegramBotRuntime(transport, _Ledger("41"), _Credentials(), "receiver:telegram")
        await runtime.start(_instance(), SimpleNamespace())
        unit = await runtime.next_ingress()
        assert transport.offsets == [41]
        assert unit.checkpoint.token == "42"

        draft = TelegramEventCodec(_instance(), "123456").render(
            _reply_target(), SimpleNamespace(content=(TextBlock("text", "reply"),), attachments=())
        )
        template = dict(draft.nodes[0].template)
        assert template["message_thread_id"] == "77"
        assert dict(template["reply_parameters"]) == {"message_id": "8"}
        await runtime.close(1000)

    asyncio.run(scenario())


def test_telegram_webhook_conflict_and_submit_outcomes_are_explicit() -> None:
    async def scenario() -> None:
        transport = _Transport([])
        transport.get_webhook_info = lambda credentials: asyncio.sleep(  # type: ignore[method-assign]
            0, result={"ok": True, "result": {"url": "https://example.invalid/hook"}}
        )
        onboarding = TelegramOnboarding(transport, _Credentials(), "org-1", "telegram-1", "main")
        preflight = await onboarding.inspect({"credential_ref": "receiver:telegram", "mode": "poll"})
        assert preflight.state == "blocked"
        assert "telegram-webhook-active" in preflight.reason_codes
        transport.get_webhook_info = lambda credentials: asyncio.sleep(  # type: ignore[method-assign]
            0, result={"ok": True, "result": {"url": "", "pending_update_count": 0}}
        )
        identity = await onboarding.authenticate(
            OnboardingAttempt("a-1", {}, "receiver:telegram")
        )
        capabilities = await onboarding.probe_capabilities(identity)
        assert {fact.name for fact in capabilities.facts} >= {"files", "topics", "reactions"}

        runtime = TelegramBotRuntime(transport, _Ledger(), _Credentials(), "receiver:telegram")
        await runtime.start(_instance(), SimpleNamespace())
        transport.outcome = {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}}
        limited = await runtime.submit(_operation())
        assert limited.submissions[0].state == "failed-retryable"
        assert limited.retry_after_ms == 3000
        transport.outcome = TimeoutError("after write")
        unknown = await runtime.submit(_operation())
        assert unknown.submissions[0].state == "unknown"
        reconciled = await runtime.reconcile(_operation(), unknown)
        assert reconciled is unknown
        with pytest.raises(ValueError, match="receiver"):
            await runtime.submit(_operation(receiver_ref="receiver:other"))
        calls = len(transport.calls)
        assert await runtime.reconcile(_operation(), unknown) is unknown
        assert len(transport.calls) == calls
        with pytest.raises(ValueError, match="text-only"):
            await runtime.submit(_operation(method="sendDocument", handle_id="raw:handle"))

    asyncio.run(scenario())


def test_empty_poll_is_a_normal_safe_retry_and_webhook_never_polls() -> None:
    async def scenario() -> None:
        transport = _Transport([])
        runtime = TelegramBotRuntime(transport, _Ledger(), _Credentials(), "receiver:telegram")
        await runtime.start(_instance(), SimpleNamespace())
        with pytest.raises(TelegramPollRetry, match="empty"):
            await runtime.next_ingress()
        assert transport.offsets == [0]

        blocked = _Transport([])
        blocked.get_webhook_info = lambda credentials: asyncio.sleep(  # type: ignore[method-assign]
            0, result={"ok": True, "result": {"url": "https://example.invalid/hook"}}
        )
        other = TelegramBotRuntime(blocked, _Ledger(), _Credentials(), "receiver:telegram")
        with pytest.raises(ValueError, match="webhook"):
            await other.start(_instance(), SimpleNamespace())
        assert blocked.offsets == []

    asyncio.run(scenario())


def test_close_deadline_unknown_retains_ingress_counts_and_is_idempotent() -> None:
    async def scenario() -> None:
        update = {
            "update_id": 8,
            "message": {
                "message_id": 9,
                "date": 1710000000,
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 2},
                "text": "pending",
            },
        }
        transport = _Transport([update])
        runtime = TelegramBotRuntime(
            transport, _Ledger(), _Credentials(), "receiver:telegram"
        )
        await runtime.start(_instance(), SimpleNamespace())
        await runtime.next_ingress()
        transport.close_outcome = ResourceCloseOutcome(
            ResourceCloseState.UNKNOWN,
            None,
            False,
            False,
            "telegram-close-deadline",
        )
        first = await runtime.close(1)
        second = await runtime.close(1000)
        assert first == second
        assert first.state == "unknown"
        assert first.retained_ingress == 1
        assert first.reason_code == "telegram-close-deadline"

    asyncio.run(scenario())
