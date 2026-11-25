from __future__ import annotations

import asyncio

import httpx
import pytest

from hyprial.daemon.impl.adapters.common.transport import HttpEffectState
from hyprial.daemon.impl.adapters.telegram import TelegramCredentials
from hyprial.daemon.impl.adapters.telegram.errors import (
    TelegramPollRetry,
    TelegramProtocolError,
)
from hyprial.daemon.impl.adapters.telegram.transport import (
    HttpxTelegramTransport,
)

from .support import LocalHttpServer, Request, json_response


TOKEN = "123456:" + "A" * 35
CREDENTIALS = TelegramCredentials(TOKEN)


def test_bot_api_requests_are_exact_and_long_poll_timeouts_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        monkeypatch.setenv("no_proxy", "127.0.0.1")

        async def handler(request: Request):  # type: ignore[no-untyped-def]
            if request.target.endswith("/getUpdates"):
                body = request.json()
                if isinstance(body, dict) and body.get("offset") == 99:
                    return json_response(200, {"ok": True, "result": []})
            return json_response(200, {"ok": True, "result": {"id": 123456}})

        async with LocalHttpServer(handler) as server:
            transport = HttpxTelegramTransport(api_base_url=server.base_url)
            await transport.get_me(CREDENTIALS)
            await transport.get_webhook_info(CREDENTIALS)
            result = await transport.get_updates(CREDENTIALS, offset=99, limit=7)
            assert result == {"ok": True, "result": []}
            poll = server.requests[-1]
            assert poll.method == "POST"
            assert poll.target == f"/bot{TOKEN}/getUpdates"
            assert poll.json() == {
                "offset": 99,
                "limit": 7,
                "timeout": 30,
                "allowed_updates": [
                    "message",
                    "edited_message",
                    "channel_post",
                    "edited_channel_post",
                    "callback_query",
                ],
            }
            assert TOKEN.encode() not in poll.body
            await transport.aclose(1000)

        async def slow_handler(request: Request):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.2)
            return json_response(200, {"ok": True, "result": []})

        async with LocalHttpServer(slow_handler) as server:
            transport = HttpxTelegramTransport(
                api_base_url=server.base_url, read_timeout_seconds=0.03
            )
            with pytest.raises(TelegramPollRetry, match="long-poll-timeout"):
                await transport.get_updates(CREDENTIALS, offset=1, limit=1)
            await transport.aclose(1000)

    asyncio.run(scenario())


def test_effecting_send_phase_and_telegram_settlement_are_not_replayed() -> None:
    async def scenario() -> None:
        modes = iter(("read-timeout", "503", "malformed", "429", "400", "ok"))

        async def handler(request: Request):  # type: ignore[no-untyped-def]
            mode = next(modes)
            assert request.target == f"/bot{TOKEN}/sendMessage"
            assert request.json() == {"chat_id": "-1001", "text": "hello"}
            if mode == "read-timeout":
                await asyncio.sleep(0.2)  # body was consumed before the read timeout
                return json_response(200, {"ok": True, "result": {"message_id": 1}})
            if mode == "503":
                return json_response(503, {"ok": False})
            if mode == "malformed":
                return json_response(200, {"ok": True, "result": {}})
            if mode == "429":
                return json_response(
                    429,
                    {"ok": False, "parameters": {"retry_after": 3}},
                )
            if mode == "400":
                return json_response(400, {"ok": False, "description": TOKEN})
            return json_response(200, {"ok": True, "result": {"message_id": 81}})

        async with LocalHttpServer(handler) as server:
            transport = HttpxTelegramTransport(
                api_base_url=server.base_url, read_timeout_seconds=0.03
            )
            request = {"chat_id": "-1001", "text": "hello"}
            outcomes = [
                await transport.send_message(CREDENTIALS, request) for _ in range(6)
            ]
            assert [item.effect.state for item in outcomes] == [
                HttpEffectState.UNKNOWN,
                HttpEffectState.UNKNOWN,
                HttpEffectState.UNKNOWN,
                HttpEffectState.RETRYABLE,
                HttpEffectState.REJECTED,
                HttpEffectState.ACCEPTED,
            ]
            assert outcomes[3].retry_after_ms == 3000
            assert outcomes[5].effect.native_id == "81"
            assert all(TOKEN not in repr(item) for item in outcomes)
            await transport.aclose(1000)

    asyncio.run(scenario())


def test_official_endpoint_is_fixed_and_overrides_are_loopback_only() -> None:
    async def scenario() -> None:
        captured: list[httpx.Request] = []

        async def responder(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"id": 1}})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(responder),
            trust_env=False,
            follow_redirects=False,
        )
        transport = HttpxTelegramTransport(client=client)
        await transport.get_me(CREDENTIALS)
        assert str(captured[0].url) == f"https://api.telegram.org/bot{TOKEN}/getMe"
        await transport.aclose(1000)
        assert not client.is_closed  # borrowed by the bundle/transport
        await client.aclose()

        with pytest.raises(TelegramProtocolError, match="endpoint") as caught:
            HttpxTelegramTransport(api_base_url="https://evil.example/api")
        assert TOKEN not in str(caught.value)

        async def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        failing_client = httpx.AsyncClient(
            transport=httpx.MockTransport(refuse),
            trust_env=False,
            follow_redirects=False,
        )
        failing = HttpxTelegramTransport(client=failing_client)
        outcome = await failing.send_message(
            CREDENTIALS, {"chat_id": "-1001", "text": "hello"}
        )
        assert outcome.effect.state is HttpEffectState.RETRYABLE
        assert outcome.effect.definitely_not_sent
        with pytest.raises(TelegramProtocolError) as redacted:
            await failing.get_me(CREDENTIALS)
        assert redacted.value.__cause__ is None
        assert TOKEN not in repr(redacted.value)
        await failing_client.aclose()

    asyncio.run(scenario())
