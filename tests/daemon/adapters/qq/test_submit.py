"""REST text wire: passive-only admission, official bodies, honest outcomes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from hyprial.daemon.impl.adapters.contracts import (
    AudienceSnapshot,
    ChatRef,
    NativeSubmissionOutcome,
    PartSubmission,
    PrincipalRef,
)

from .support import (
    APP_ID,
    BOT_ID,
    LocalGateway,
    LocalHTTP,
    MemoryLedger,
    StaticCredentials,
    WallClockMs,
    build_components,
    default_hello,
    json_response,
    qq_instance,
    text_operation,
)


class SubmitEnv:
    def __init__(self, tmp_path: Path, *, rest_timeout: float = 5.0) -> None:
        self.http = LocalHTTP()
        self.gateway = LocalGateway()
        self.clock = WallClockMs()
        self.rest_timeout = rest_timeout
        self.tmp_path = tmp_path
        self.messages_response: tuple[int, object] = (200, {"id": "qq-out-1"})
        self.runtime = None
        self.components = None

    async def __aenter__(self) -> "SubmitEnv":
        async def respond(request: object) -> tuple[int, dict[str, str], bytes]:
            path = request.path  # type: ignore[attr-defined]
            if path == "/app/getAppAccessToken":
                return json_response(200, {"access_token": "tok-1", "expires_in": 7200})
            if path == "/users/@me":
                return json_response(200, {"id": BOT_ID, "username": "hyprial", "bot": True})
            status, body = self.messages_response
            return json_response(status, body)

        self.http.responder = respond
        self.gateway.handler = default_hello()
        await self.http.start()
        self.gateway_url = await self.gateway.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.runtime is not None:
            try:
                await asyncio.wait_for(self.runtime.close(2000), timeout=5)
            except Exception:
                pass
        if self.components is not None:
            try:
                await self.components.token.stop()
            except Exception:
                pass
            self.components.store.close()
        await self.gateway.stop()
        if self.http._server is not None:
            await self.http.stop()

    async def build(self, *, inbound: tuple[str, int] = ("message-1", 1_000_000)):
        from hyprial.daemon.impl.adapters.qq.provider import QQOfficialBotRuntime

        self.components = build_components(
            http=self.http,
            gateway_url=self.gateway_url,
            store_path=self.tmp_path / "store.sqlite",
            authority=None,
            now_ms=self.clock,
            rest_timeout=self.rest_timeout,
        )
        self.runtime = QQOfficialBotRuntime(
            self.components, MemoryLedger(), StaticCredentials(), "receiver:qq"
        )
        await self.runtime.start(qq_instance(), SimpleNamespace())
        msg_id, at = inbound
        self.components.store.record_inbound(
            instance_id="qq-1", generation=2, msg_id=msg_id, received_at_ms=at
        )
        return self.runtime

    def message_requests(self) -> list:
        return [r for r in self.http.requests if "/messages" in r.path]

    def upload_requests(self) -> list:
        return [r for r in self.http.requests if "upload" in r.path or "/files" in r.path]


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.002)


def test_passive_group_text_send_matches_official_wire(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            outcome = await runtime.submit(text_operation())
            assert outcome.submissions[0].state == "accepted"
            assert outcome.submissions[0].provider_message_id == "qq-out-1"
            request = env.message_requests()[0]
            assert request.method == "POST"
            assert request.path == "/v2/groups/group-1/messages"
            assert request.header("authorization") == "QQBot tok-1"
            body = request.json()
            assert body["content"] == "hello"
            assert body["msg_type"] == 0
            assert body["msg_id"] == "message-1"
            assert isinstance(body["msg_seq"], int) and 0 <= body["msg_seq"] <= 65535

    asyncio.run(scenario())


def test_c2c_text_send_uses_users_route(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            env.components.store.record_inbound(
                instance_id="qq-1", generation=2, msg_id="message-c2c", received_at_ms=1_000_000
            )
            outcome = await runtime.submit(
                text_operation(scene="c2c", msg_id="message-c2c", content="dm hello")
            )
            assert outcome.submissions[0].state == "accepted"
            request = env.message_requests()[0]
            assert request.path == "/v2/users/user-1/messages"
            assert request.json()["msg_id"] == "message-c2c"

    asyncio.run(scenario())


def test_missing_forged_and_stale_msg_ids_rejected_before_http(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            missing = await runtime.submit(text_operation(msg_id=None))
            assert missing.submissions[0].state == "failed-final"
            assert missing.submissions[0].reason_code == "qq-msg-id-required"
            forged = await runtime.submit(text_operation(msg_id="never-seen"))
            assert forged.submissions[0].reason_code == "qq-msg-id-unknown"
            env.clock.advance_ms(3_600_001)
            stale = await runtime.submit(text_operation())
            assert stale.submissions[0].reason_code == "qq-msg-id-stale"
            assert env.message_requests() == [], "rejections happen before HTTP"

    asyncio.run(scenario())


def test_four_reply_limit_enforced_locally_before_http(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            for index in range(4):
                outcome = await runtime.submit(text_operation(operation_id=f"op-{index}"))
                assert outcome.submissions[0].state == "accepted"
            fifth = await runtime.submit(text_operation(operation_id="op-4"))
            assert fifth.submissions[0].state == "failed-final"
            assert fifth.submissions[0].reason_code == "qq-msg-limit"
            assert len(env.message_requests()) == 4

    asyncio.run(scenario())


def test_429_maps_provider_delay(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            env.messages_response = (429, {"code": 100001, "retry_after_ms": 1200})
            outcome = await runtime.submit(text_operation())
            part = outcome.submissions[0]
            assert part.state == "failed-retryable"
            assert part.reason_code == "qq-rate-limited"
            assert outcome.retry_after_ms == 1200
            assert part.definitely_not_sent is True

    asyncio.run(scenario())


def test_22009_is_retryable_without_delay(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            env.messages_response = (400, {"code": 22009, "message": "msg limit exceed"})
            outcome = await runtime.submit(text_operation())
            part = outcome.submissions[0]
            assert part.state == "failed-retryable"
            assert part.reason_code == "qq-biz-22009"
            assert outcome.retry_after_ms is None

    asyncio.run(scenario())


def test_post_body_timeout_is_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path, rest_timeout=0.3) as env:
            runtime = await env.build()

            async def hang(request: object) -> tuple[int, dict[str, str], bytes]:
                await asyncio.sleep(3600)
                raise AssertionError("unreachable")

            env.http.responder = hang
            outcome = await runtime.submit(text_operation())
            part = outcome.submissions[0]
            assert part.state == "unknown"
            assert part.definitely_not_sent is False

    asyncio.run(scenario())


def test_effecting_server_error_is_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            env.messages_response = (500, {"code": 500, "message": "boom"})
            outcome = await runtime.submit(text_operation())
            assert outcome.submissions[0].state == "unknown"

    asyncio.run(scenario())


def test_success_without_native_id_is_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            env.messages_response = (200, {})
            outcome = await runtime.submit(text_operation())
            assert outcome.submissions[0].state == "unknown"
            env.messages_response = (200, {"id": ""})
            again = await runtime.submit(text_operation(operation_id="op-2"))
            assert again.submissions[0].state == "unknown"

    asyncio.run(scenario())


def test_pre_send_connection_refusal_is_retryable_and_definitely_not_sent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            await env.http.stop()
            outcome = await runtime.submit(text_operation())
            part = outcome.submissions[0]
            assert part.state == "failed-retryable"
            assert part.definitely_not_sent is True
            assert part.reason_code == "qq-pre-send-failure"

    asyncio.run(scenario())


def test_post_body_connection_loss_is_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()

            async def drop_after_body(
                _request: object,
            ) -> tuple[int, dict[str, str], bytes]:
                raise ConnectionResetError("drop after request body admission")

            env.http.responder = drop_after_body
            outcome = await runtime.submit(text_operation())
            part = outcome.submissions[0]
            assert part.state == "unknown"
            assert part.definitely_not_sent is False

    asyncio.run(scenario())


def test_duplicate_submit_never_fabricates_prior_native_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            first = await runtime.submit(text_operation(operation_id="op-dup"))
            assert first.submissions[0].provider_message_id == "qq-out-1"
            env.messages_response = (200, {})
            retry = await runtime.submit(text_operation(operation_id="op-dup"))
            assert retry.submissions[0].state == "unknown"
            assert retry.submissions[0].provider_message_id is None

    asyncio.run(scenario())


def test_reconcile_keeps_ambiguous_outcome_unknown_without_http(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            prior = NativeSubmissionOutcome(
                (PartSubmission(0, "unknown", None, False, "qq-submit-unknown"),), None
            )
            resolved = await runtime.reconcile(text_operation(), prior)
            assert resolved == prior
            assert env.message_requests() == []

    asyncio.run(scenario())


def test_resolve_rejects_proactive_send_and_foreign_scope(tmp_path: Path) -> None:
    async def scenario() -> None:
        from hyprial.daemon.impl.adapters.contracts import AuthorizedChatTarget

        async with SubmitEnv(tmp_path) as env:
            runtime = await env.build()
            instance = qq_instance()
            chat = ChatRef(
                "chat-ref:v1:qq:qq-1:group:group-1",
                instance,
                APP_ID,
                "group-1",
                "group",
                None,
                None,
            )
            from hyprial.daemon.impl.adapters.contracts import AdapterInstanceRef

            foreign_instance = AdapterInstanceRef(
                "qq-9",
                "qq",
                "official-bot",
                "other",
                "org-1",
                qq_instance().external_fingerprint,
                2,
            )
            foreign = ChatRef(
                "chat-ref:v1:qq:qq-9:group:group-1",
                foreign_instance,
                APP_ID,
                "group-1",
                "group",
                None,
                None,
            )
            sender = PrincipalRef(
                "user-1", None, None, None, None, "unresolved", None, None, None,
                "qq-official-gateway",
            )
            audience = AudienceSnapshot("group", (sender,), "partial", None, 1)
            proactive = AuthorizedChatTarget("t-1", chat, audience, "send", None)
            assert (await runtime.resolve(proactive)).status == "unsupported"
            assert (await runtime.resolve(proactive)).reason_code == "qq-proactive-unsupported"
            outsider = AuthorizedChatTarget("t-2", foreign, audience, "reply", "message-1")
            assert (await runtime.resolve(outsider)).status == "unauthorized"
            reply = AuthorizedChatTarget("t-3", chat, audience, "reply", "message-1")
            assert (await runtime.resolve(reply)).status == "resolved"

    asyncio.run(scenario())
