from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets

from hyprial.daemon.impl.adapters.common.transport import (
    HttpEffectState,
    ResourceCloseState,
)
from hyprial.daemon.impl.adapters.slack import HttpxSlackTransport, SlackCredentials
from hyprial.daemon.impl.adapters.slack.errors import SlackProtocolError

from .support import APP_TOKEN, BOT_TOKEN, read_request, send_json


def test_real_loopback_http_uses_distinct_tokens_and_never_puts_them_in_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        seen: list[tuple[str, dict[str, str], bytes]] = []

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            request = await read_request(reader)
            seen.append(request)
            path, headers, _body = request
            if path.endswith("/auth.test"):
                assert headers["authorization"] == f"Bearer {BOT_TOKEN}"
                await send_json(writer, {"ok": True, "team_id": "T1", "bot_id": "BOT1"})
            elif path.endswith("/apps.connections.open"):
                assert headers["authorization"] == f"Bearer {APP_TOKEN}"
                await send_json(
                    writer,
                    {
                        "ok": True,
                        "url": "wss://wss-primary.slack.com/link/?ticket=opaque",
                    },
                )
            else:
                assert headers["authorization"] == f"Bearer {BOT_TOKEN}"
                await send_json(writer, {"ok": True, "ts": "172.2"})

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = HttpxSlackTransport(api_base_url=f"http://127.0.0.1:{port}/api")
        credentials = SlackCredentials(BOT_TOKEN, APP_TOKEN)
        try:
            assert (await transport.authenticate(credentials))["team_id"] == "T1"
            assert (await transport.open_socket(credentials)).startswith("wss://")
            result = await transport.call(
                "chat.postMessage", {"channel": "D1", "text": "hello"}, "idem-1"
            )
            assert result.observation.state is HttpEffectState.ACCEPTED
            assert result.observation.native_id == "172.2"
            assert all(
                BOT_TOKEN not in path and APP_TOKEN not in path for path, _, _ in seen
            )
        finally:
            await transport.aclose(asyncio.get_running_loop().time() + 1)
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_effecting_response_outcomes_preserve_unknown_and_retry_after_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        responses = [
            (503, {"ok": False, "error": "unavailable"}, {}),
            (200, {"ok": True}, {}),
            (429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "2.5"}),
        ]

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await read_request(reader)
            status, body, headers = responses.pop(0)
            await send_json(writer, body, status=status, headers=headers)

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        settled = []
        transport = HttpxSlackTransport(
            api_base_url=f"http://127.0.0.1:{port}/api", settle=settled.append
        )
        transport.bind_credentials(SlackCredentials(BOT_TOKEN, APP_TOKEN))
        try:
            unavailable = await transport.call(
                "chat.postMessage", {"channel": "D1"}, "i1"
            )
            malformed = await transport.call(
                "chat.postMessage", {"channel": "D1"}, "i2"
            )
            limited = await transport.call("chat.postMessage", {"channel": "D1"}, "i3")
            assert unavailable.observation.state is HttpEffectState.UNKNOWN
            assert malformed.observation.state is HttpEffectState.UNKNOWN
            assert limited.observation.state is HttpEffectState.REJECTED
            assert limited.retry_after_ms == 2500
            assert settled == [
                unavailable.observation,
                malformed.observation,
                limited.observation,
            ]
        finally:
            await transport.aclose(asyncio.get_running_loop().time() + 1)
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_post_body_read_timeout_is_unknown_and_deprecated_upload_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        admitted = asyncio.Event()

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await read_request(reader)
            admitted.set()
            await asyncio.sleep(0.2)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = httpx.AsyncClient(
            timeout=0.02, trust_env=False, follow_redirects=False
        )
        transport = HttpxSlackTransport(
            client=client, api_base_url=f"http://127.0.0.1:{port}/api"
        )
        transport.bind_credentials(SlackCredentials(BOT_TOKEN, APP_TOKEN))
        try:
            outcome = await transport.call(
                "chat.postMessage", {"channel": "D1"}, "idem"
            )
            assert admitted.is_set()
            assert outcome.observation.state is HttpEffectState.UNKNOWN
            assert outcome.observation.exception_type == "ReadTimeout"
            for method in ("files.upload", "files.uploadV2"):
                with pytest.raises(
                    SlackProtocolError, match="SLACK_MEDIA_ROUTE_REQUIRED"
                ):
                    await transport.call(method, {}, "forbidden")
        finally:
            await transport.aclose(asyncio.get_running_loop().time() + 1)
            await client.aclose()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_real_connect_failure_before_body_admission_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        server = await asyncio.start_server(
            lambda _reader, _writer: None, "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        transport = HttpxSlackTransport(api_base_url=f"http://127.0.0.1:{port}/api")
        transport.bind_credentials(SlackCredentials(BOT_TOKEN, APP_TOKEN))
        try:
            outcome = await transport.call(
                "chat.postMessage", {"channel": "D1", "text": "hello"}, "idem"
            )
            assert outcome.observation.state is HttpEffectState.RETRYABLE
            assert outcome.observation.definitely_not_sent is True
            assert outcome.observation.exception_type == "ConnectError"
        finally:
            await transport.aclose(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())


def test_real_websocket_generation_ack_remote_close_and_reconnect_keeps_client_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        received: list[object] = []
        acknowledged = asyncio.Event()

        async def peer(socket) -> None:
            received.append(json.loads(await socket.recv()))
            acknowledged.set()
            await socket.close(code=4001, reason="refresh_requested")

        async with websockets.serve(peer, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/socket"
            client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
            transport = HttpxSlackTransport(
                client=client,
                api_base_url="http://127.0.0.1:1/api",
                allow_insecure_loopback=True,
                socket_url_override=url,
            )
            assert (
                await transport.open_socket(SlackCredentials(BOT_TOKEN, APP_TOKEN))
                == url
            )
            await transport.connect(url, max_frame_bytes=1_048_576)
            await transport.acknowledge("env-1")
            await asyncio.wait_for(acknowledged.wait(), timeout=1)
            assert received == [{"envelope_id": "env-1"}]
            with pytest.raises(
                SlackProtocolError, match="SLACK_SOCKET_CLOSED"
            ) as captured:
                await transport.receive(max_frame_bytes=1_048_576)
            assert captured.value.close_observation.rcvd_code == 4001
            assert captured.value.close_observation.sent_code == 4001
            assert captured.value.close_observation.rcvd_then_sent is True
            closed = await transport.close_socket(
                "reconnect", asyncio.get_running_loop().time() + 1
            )
            assert closed.state is ResourceCloseState.CLOSED
            assert not client.is_closed
            with pytest.raises(SlackProtocolError, match="SLACK_SOCKET_URL_STALE"):
                await transport.connect(url, max_frame_bytes=1_048_576)
            await transport.aclose(asyncio.get_running_loop().time() + 1)
            assert not client.is_closed
            await client.aclose()

    asyncio.run(scenario())


def test_real_websocket_no_close_frame_is_normalized_as_transport_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        async def peer(socket) -> None:
            socket.transport.abort()

        async with websockets.serve(peer, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{port}/socket"
            transport = HttpxSlackTransport(
                allow_insecure_loopback=True, socket_url_override=url
            )
            credentials = SlackCredentials(BOT_TOKEN, APP_TOKEN)
            assert await transport.open_socket(credentials) == url
            await transport.connect(url, max_frame_bytes=1_048_576)
            with pytest.raises(
                SlackProtocolError, match="SLACK_SOCKET_CLOSED"
            ) as captured:
                await transport.receive(max_frame_bytes=1_048_576)
            observation = captured.value.close_observation
            assert observation.rcvd_code is None
            assert observation.sent_code is None
            await transport.aclose(asyncio.get_running_loop().time() + 1)

    asyncio.run(scenario())
