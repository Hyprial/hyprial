"""Real loopback ownership REDs: borrowed client, deadlines, media authority."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import websockets

from hyprial.daemon.impl.adapters.common.transport import ResourceCloseState
from hyprial.daemon.impl.adapters.discord import (
    DiscordEndpointPolicy,
    DiscordGatewayTransport,
)

from .support import (
    Credentials,
    Ledger,
    LoopbackGateway,
    ScriptedHttpServer,
    discord_api_handler,
    instance_ref,
)

WS_TIMEOUT = 3.0


class _Sink:
    async def stage(self, unit: object) -> object:
        return None


def _loopback_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def test_borrowed_http_client_stays_open_and_owned_socket_closes_1000(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        gateway = LoopbackGateway()
        ws_url = await gateway.start()
        try:
            policy = DiscordEndpointPolicy(
                rest_base_url="http://127.0.0.1:9",
                gateway_hosts=("127.0.0.1",),
                gateway_schemes=("ws",),
            )
            shared = httpx.AsyncClient(trust_env=False, follow_redirects=False)
            transport = DiscordGatewayTransport(
                policy=policy,
                client=shared,
                owns_client=False,
                ws_open_timeout=1.0,
                ws_close_timeout=1.0,
            )
            await transport.connect(ws_url, max_frame_bytes=1 << 20)
            assert transport.socket_generation == 1
            loop = asyncio.get_running_loop()
            outcome = await transport.aclose(loop.time() + 2.0)
            assert outcome.state is ResourceCloseState.CLOSED
            assert outcome.client_closed is False
            assert shared.is_closed is False  # borrowed resources stay open
            connection = gateway.connections[0]
            close = await connection.wait_close(2.0)
            assert close is not None and close[0] == 1000  # final close is 1000
            await shared.aclose()
        finally:
            await gateway.stop()

    asyncio.run(scenario())


def test_noncooperating_close_deadline_is_unknown_idempotent_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        paused = asyncio.Event()

        async def stall(connection: object) -> None:
            connection.transport.pause_reading()  # type: ignore[attr-defined]
            paused.set()
            await asyncio.sleep(5)

        server = await websockets.serve(stall, "127.0.0.1", 0, close_timeout=10)
        port = server.sockets[0].getsockname()[1]
        try:
            policy = DiscordEndpointPolicy(
                rest_base_url="http://127.0.0.1:9",
                gateway_hosts=("127.0.0.1",),
                gateway_schemes=("ws",),
            )
            transport = DiscordGatewayTransport(
                policy=policy, ws_open_timeout=1.0, ws_close_timeout=5.0
            )
            await transport.connect(f"ws://127.0.0.1:{port}/", max_frame_bytes=1 << 20)
            await paused.wait()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 0.2
            outcome = await transport.aclose(deadline)
            assert outcome.state is ResourceCloseState.UNKNOWN
            assert outcome.reason_code is not None
            again = await transport.aclose(loop.time() + 0.2)
            assert again.state is ResourceCloseState.UNKNOWN  # idempotent finalizer
            with pytest.raises(RuntimeError):
                await transport.connect(
                    f"ws://127.0.0.1:{port}/", max_frame_bytes=1 << 20
                )
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_reconnect_close_uses_4000_and_reopens_new_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        gateway = LoopbackGateway()
        ws_url = await gateway.start()
        try:
            policy = DiscordEndpointPolicy(
                rest_base_url="http://127.0.0.1:9",
                gateway_hosts=("127.0.0.1",),
                gateway_schemes=("ws",),
            )
            transport = DiscordGatewayTransport(
                policy=policy, ws_open_timeout=1.0, ws_close_timeout=1.0
            )
            await transport.connect(ws_url, max_frame_bytes=1 << 20)
            await transport.close(4000)
            close = await gateway.connections[0].wait_close(2.0)
            assert close is not None and close[0] == 4000
            await transport.connect(ws_url, max_frame_bytes=1 << 20)
            assert transport.socket_generation == 2
            await transport.send({"op": 1, "d": None})
            frame = await gateway.connections[1].wait_for(
                lambda item: item.get("op") == 1, 1.0
            )
            assert frame["d"] is None
            loop = asyncio.get_running_loop()
            outcome = await transport.aclose(loop.time() + 2.0)
            assert outcome.state is ResourceCloseState.CLOSED
            final = await gateway.connections[1].wait_close(2.0)
            assert final is not None and final[0] == 1000
        finally:
            await gateway.stop()

    asyncio.run(scenario())


def test_transport_bundle_never_closes_media_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        from hyprial.daemon.impl.adapters.common.transport import (
            NativeMediaScope,
            SealedNativeMediaAuthority,
        )
        from hyprial.daemon.impl.adapters.discord import (
            DiscordGatewayRuntime,
            DiscordMedia,
        )

        gateway = LoopbackGateway()
        ws_url = await gateway.start()
        http = ScriptedHttpServer(discord_api_handler(ws_url))
        http_base = await http.start()
        authority = SealedNativeMediaAuthority(
            tmp_path / "media.db", max_blob_bytes=1 << 20
        )
        policy = DiscordEndpointPolicy(
            rest_base_url=http_base,
            gateway_hosts=("127.0.0.1",),
            gateway_schemes=("ws",),
            cdn_hosts=("127.0.0.1",),
            cdn_schemes=("http",),
            cdn_allow_private_addresses=True,
        )
        transport = DiscordGatewayTransport(policy=policy, request_timeout=2.0)
        runtime = DiscordGatewayRuntime(
            transport, Ledger(), Credentials(), "receiver:discord"
        )
        DiscordMedia(
            transport=transport,
            authority=authority,
            scope=SimpleNamespace(
                owner_principal="principal-1",
                organization_id="org-1",
                account_scope="app-1",
                instance_id="discord-1",
                generation=3,
            ),
        )
        try:
            started = asyncio.create_task(runtime.start(instance_ref(), _Sink()))
            connection = await gateway.wait_connection(0, WS_TIMEOUT)
            await connection.send({"op": 10, "d": {"heartbeat_interval": 60000}})
            await connection.send(
                {
                    "op": 0,
                    "s": 1,
                    "t": "READY",
                    "d": {"session_id": "sess-1"},
                }
            )
            await started
            digest = hashlib.sha256(b"survives").hexdigest()
            scope = NativeMediaScope(
                owner_principal="principal-1",
                organization_id="org-1",
                provider="discord",
                instance_id="discord-1",
                generation=3,
                target_ref_id="ref-1",
                content_digest=digest,
            )
            reference = authority.seal(
                scope=scope,
                body=b"survives",
                name="s.txt",
                media_type="text/plain",
                native_metadata={"attachment_id": "a-1"},
            )
            await runtime.close(1000)
            sealed = authority.open(reference, scope=scope, max_bytes=1024)
            chunk = await sealed.read(64)
            assert chunk == b"survives"  # authority outlives the transport bundle
        finally:
            await gateway.stop()
            await http.stop()

    asyncio.run(scenario())
