"""Real loopback Gateway v10 REDs: heartbeat timing, ACK fence, close table."""

from __future__ import annotations

import asyncio
import json

import pytest

from hyprial.daemon.impl.adapters.common.transport import (
    WebSocketCloseObservation,
    WebSocketCloseOrigin,
)
from hyprial.daemon.impl.adapters.discord import (
    DiscordConnectionClosed,
    DiscordEndpointPolicy,
    DiscordFatalCloseError,
    DiscordGatewayRuntime,
    DiscordGatewayTransport,
    GatewayAction,
    classify_gateway_close,
)
from hyprial.daemon.impl.adapters.discord.errors import DiscordProtocolError

from .support import (
    Credentials,
    Ledger,
    LoopbackGateway,
    ManualClock,
    ScriptedHttpServer,
    discord_api_handler,
    durable_value,
    instance_ref,
)

WS_TIMEOUT = 3.0


class _Sink:
    async def stage(self, unit: object) -> object:
        return None


class Harness:
    """Real loopback HTTP + WS harness around one production transport."""

    def __init__(self, *, ledger: Ledger | None = None) -> None:
        self.gateway = LoopbackGateway()
        self.http: ScriptedHttpServer | None = None
        self.runtime: DiscordGatewayRuntime | None = None
        self.transport: DiscordGatewayTransport | None = None
        self.ledger = ledger if ledger is not None else Ledger()
        self.ws_url: str | None = None

    async def start(self, **runtime_kwargs: object) -> DiscordGatewayRuntime:
        """Start real loopback HTTP + WS harness; durable=True resumes a session."""
        self.ws_url = await self.gateway.start()
        if runtime_kwargs.pop("durable", False):
            self.ledger = Ledger(durable_value(resume_url=self.ws_url))
        self.http = ScriptedHttpServer(discord_api_handler(self.ws_url))
        http_base = await self.http.start()
        policy = DiscordEndpointPolicy(
            rest_base_url=http_base,
            gateway_hosts=("127.0.0.1",),
            gateway_schemes=("ws",),
        )
        self.transport = DiscordGatewayTransport(
            policy=policy,
            request_timeout=2.0,
            ws_open_timeout=1.0,
            ws_close_timeout=1.0,
        )
        self.runtime = DiscordGatewayRuntime(
            self.transport,
            self.ledger,
            Credentials(),
            "receiver:discord",
            **runtime_kwargs,  # type: ignore[arg-type]
        )
        await self.runtime.start(instance_ref(), _Sink())
        return self.runtime

    async def stop(self) -> None:
        if self.runtime is not None:
            async with asyncio.timeout(3.0):
                await self.runtime.close(2000)
        async with asyncio.timeout(3.0):
            await self.gateway.stop()
        if self.http is not None:
            async with asyncio.timeout(3.0):
                await self.http.stop()


async def _handshake(connection: object, *, session_id: str = "sess-1") -> None:
    await connection.send({"op": 10, "d": {"heartbeat_interval": 60000}})
    await connection.send(
        {
            "op": 0,
            "s": 1,
            "t": "READY",
            "d": {"session_id": session_id},
        }
    )


async def _resumed(connection: object, sequence: int = 7) -> None:
    await connection.send({"op": 10, "d": {"heartbeat_interval": 60000}})
    await connection.send({"op": 0, "s": sequence, "t": "RESUMED", "d": {}})


def _message(sequence: int, native: str = "m-9") -> dict:
    return {
        "op": 0,
        "s": sequence,
        "t": "MESSAGE_CREATE",
        "d": {
            "id": native,
            "channel_id": "c-1",
            "guild_id": None,
            "author": {"id": "u-1", "bot": False},
            "content": "hello",
        },
    }


async def _wait_online(runtime: DiscordGatewayRuntime) -> None:
    async with asyncio.timeout(2.0):
        while (await runtime.health()).state != "online":
            await asyncio.sleep(0.005)


def _loopback_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def test_heartbeat_generation_starts_between_hello_and_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = Harness()
        try:
            started = asyncio.create_task(harness.start())
            connection = await harness.gateway.wait_connection(0, WS_TIMEOUT)
            await connection.send({"op": 10, "d": {"heartbeat_interval": 60}})
            heartbeat = await connection.wait_for(
                lambda frame: frame.get("op") == 1, 0.5
            )
            await connection.send(
                {
                    "op": 0,
                    "s": 5,
                    "t": "READY",
                    "d": {
                        "session_id": "sess-1",
                    },
                }
            )
            await started
            await connection.wait_for(lambda frame: frame.get("op") == 2, 0.5)
            assert heartbeat["d"] is None
            assert harness.gateway.count == 1
        finally:
            await harness.stop()

    async def bounded() -> None:
        async with asyncio.timeout(6.0):
            await scenario()

    asyncio.run(bounded())


def test_start_joins_heartbeat_replacement_and_absorbs_old_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missed ACK during READY wait transfers, rather than fails, start ownership."""

    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = Harness()
        started: asyncio.Task[DiscordGatewayRuntime] | None = None
        try:
            started = asyncio.create_task(
                harness.start(durable=True, heartbeat_jitter=lambda low, high: 0.0)
            )
            first = await harness.gateway.wait_connection(0, WS_TIMEOUT)
            await first.send({"op": 10, "d": {"heartbeat_interval": 20}})
            resume = await first.wait_for(lambda frame: frame.get("op") == 6, 0.5)
            assert resume["d"]["session_id"] == "sess-durable"
            await first.wait_for(lambda frame: frame.get("op") == 1, 0.5)

            close = await first.wait_close(1.0)
            assert close == (4000, "discord-reconnect")
            second = await harness.gateway.wait_connection(1, WS_TIMEOUT)
            assert started.done() is False

            await second.send({"op": 10, "d": {"heartbeat_interval": 60000}})
            replacement = await second.wait_for(lambda frame: frame.get("op") == 6, 0.5)
            assert replacement["d"]["session_id"] == "sess-durable"
            assert replacement["d"]["seq"] == 6
            await second.send({"op": 11, "d": None})
            await second.send({"op": 0, "s": 7, "t": "RESUMED", "d": {}})

            runtime = await asyncio.wait_for(started, 1.0)
            assert runtime is harness.runtime
            assert harness.gateway.count == 2
            assert first.client_close == (4000, "discord-reconnect")
            assert second.closed.is_set() is False
            assert (await runtime.health()).state == "online"
        finally:
            if started is not None and not started.done():
                started.cancel()
                try:
                    await started
                except asyncio.CancelledError:
                    pass
            await harness.stop()

    async def bounded() -> None:
        async with asyncio.timeout(6.0):
            await scenario()

    asyncio.run(bounded())


def test_op1_answers_immediately_latest_sequence_and_missed_ack_closes_4000_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = Harness()
        clock = ManualClock()
        pump: asyncio.Task[object] | None = None
        try:
            started = asyncio.create_task(
                harness.start(
                    durable=True,
                    heartbeat_sleep=clock.sleep,
                    heartbeat_jitter=lambda low, high: 0.0,
                )
            )
            first = await harness.gateway.wait_connection(0, WS_TIMEOUT)
            await first.send({"op": 10, "d": {"heartbeat_interval": 45000}})
            await first.wait_for(lambda frame: frame.get("op") == 6, 0.5)
            await first.send({"op": 1, "d": None})
            await first.wait_for(
                lambda frame: frame.get("op") == 1 and frame.get("d") == 6, 0.5
            )
            await first.send({"op": 11, "d": None})  # fence the immediate heartbeat
            await first.send({"op": 0, "s": 7, "t": "RESUMED", "d": {}})
            await started
            pump = asyncio.create_task(harness.runtime.next_ingress())  # pull loop

            clock.advance()
            await first.wait_for(
                lambda frame: frame.get("op") == 1 and frame.get("d") == 7, 0.5
            )
            await first.send({"op": 11, "d": None})  # pump dispatches the ACK fence
            await _wait_online(harness.runtime)
            clock.advance()
            await first.wait_for(
                lambda frame: len([f for f in first.received if f.get("op") == 1]) >= 3,
                0.5,
            )
            clock.advance()
            close = await first.wait_close(1.5)
            assert close is not None and close[0] == 4000

            second = await harness.gateway.wait_connection(1, WS_TIMEOUT)
            await _resumed(second)
            resume = await second.wait_for(lambda frame: frame.get("op") == 6, 0.5)
            assert resume["d"]["session_id"] == "sess-durable"
            assert resume["d"]["seq"] == 6
            await _wait_online(harness.runtime)
        finally:
            if pump is not None:
                pump.cancel()
                try:
                    await pump
                except (asyncio.CancelledError, DiscordConnectionClosed):
                    pass
            await harness.stop()

    async def bounded() -> None:
        async with asyncio.timeout(6.0):
            await scenario()

    asyncio.run(bounded())


def test_close_table_matrix_is_exact() -> None:
    for code in (4000, 4001, 4002, 4005, 4008):
        assert (
            classify_gateway_close(
                origin=WebSocketCloseOrigin.REMOTE,
                rcvd_code=code,
                has_resume_tuple=True,
                conservative_attempted=False,
            ).action
            is GatewayAction.RESUME
        )
        assert (
            classify_gateway_close(
                origin=WebSocketCloseOrigin.REMOTE,
                rcvd_code=code,
                has_resume_tuple=False,
                conservative_attempted=False,
            ).action
            is GatewayAction.IDENTIFY
        )
    for origin in (WebSocketCloseOrigin.REMOTE, WebSocketCloseOrigin.TRANSPORT):
        assert (
            classify_gateway_close(
                origin=origin,
                rcvd_code=None,
                has_resume_tuple=True,
                conservative_attempted=False,
            ).action
            is GatewayAction.RESUME
        )
        assert (
            classify_gateway_close(
                origin=origin,
                rcvd_code=1006,
                has_resume_tuple=True,
                conservative_attempted=False,
            ).action
            is GatewayAction.RESUME
        )
        assert (
            classify_gateway_close(
                origin=origin,
                rcvd_code=None,
                has_resume_tuple=False,
                conservative_attempted=False,
            ).action
            is GatewayAction.IDENTIFY
        )
    for code in (4003, 4007, 4009):
        assert (
            classify_gateway_close(
                origin=WebSocketCloseOrigin.REMOTE,
                rcvd_code=code,
                has_resume_tuple=True,
                conservative_attempted=False,
            ).action
            is GatewayAction.IDENTIFY
        )
    for code in (4004, 4010, 4011, 4012, 4013, 4014):
        assert (
            classify_gateway_close(
                origin=WebSocketCloseOrigin.REMOTE,
                rcvd_code=code,
                has_resume_tuple=True,
                conservative_attempted=False,
            ).action
            is GatewayAction.FATAL
        )
    for code in (1000, 1001):
        assert (
            classify_gateway_close(
                origin=WebSocketCloseOrigin.LOCAL,
                rcvd_code=code,
                has_resume_tuple=True,
                conservative_attempted=False,
            ).action
            is GatewayAction.STOP
        )
        assert (
            classify_gateway_close(
                origin=WebSocketCloseOrigin.REMOTE,
                rcvd_code=code,
                has_resume_tuple=True,
                conservative_attempted=False,
            ).action
            is GatewayAction.RESUME_ONCE
        )
        assert (
            classify_gateway_close(
                origin=WebSocketCloseOrigin.REMOTE,
                rcvd_code=code,
                has_resume_tuple=True,
                conservative_attempted=True,
            ).action
            is GatewayAction.IDENTIFY
        )
        assert (
            classify_gateway_close(
                origin=WebSocketCloseOrigin.REMOTE,
                rcvd_code=code,
                has_resume_tuple=False,
                conservative_attempted=False,
            ).action
            is GatewayAction.IDENTIFY
        )
    assert (
        classify_gateway_close(
            origin=WebSocketCloseOrigin.REMOTE,
            rcvd_code=4999,
            has_resume_tuple=True,
            conservative_attempted=False,
        ).action
        is GatewayAction.IDENTIFY
    )


@pytest.mark.parametrize("code", [4000, 4001, 4002, 4005, 4008, 1006])
def test_remote_resume_close_codes_reconnect_with_durable_tuple(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = Harness()

        harness = Harness()
        try:
            started = asyncio.create_task(harness.start(durable=True))
            first = await harness.gateway.wait_connection(0, WS_TIMEOUT)
            await _resumed(first)
            await started
            if code == 1006:
                first.abort()
            else:
                await first.close(code, "rotate")
            ingress = asyncio.create_task(harness.runtime.next_ingress())
            second = await harness.gateway.wait_connection(1, WS_TIMEOUT)
            await second.send({"op": 10, "d": {"heartbeat_interval": 60000}})
            resume = await second.wait_for(lambda frame: frame.get("op") == 6, 0.5)
            assert resume["d"]["session_id"] == "sess-durable"
            assert resume["d"]["seq"] == 6
            await second.send({"op": 0, "s": 8, "t": "RESUMED", "d": {}})
            await second.send(_message(9))
            unit = await asyncio.wait_for(ingress, WS_TIMEOUT)
            assert unit.checkpoint.token == "9"
        finally:
            await harness.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [4003, 4007, 4009])
def test_remote_invalidate_close_codes_tombstone_and_identify(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = Harness()
        try:
            started = asyncio.create_task(harness.start(durable=True))
            first = await harness.gateway.wait_connection(0, WS_TIMEOUT)
            await _resumed(first)
            await started
            await first.close(code, "invalidate")
            ingress = asyncio.create_task(harness.runtime.next_ingress())
            second = await harness.gateway.wait_connection(1, WS_TIMEOUT)
            await second.send({"op": 10, "d": {"heartbeat_interval": 60000}})
            await second.wait_for(lambda frame: frame.get("op") == 2, 0.5)
            assert json.loads(harness.ledger.value or "{}").get("session_id") is None
            await second.send(
                {
                    "op": 0,
                    "s": 8,
                    "t": "READY",
                    "d": {
                        "session_id": "sess-new",
                    },
                }
            )
            await second.send(_message(9))
            unit = await asyncio.wait_for(ingress, WS_TIMEOUT)
            assert unit.checkpoint.token == "9"
        finally:
            await harness.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [4004, 4010, 4011, 4012, 4013, 4014])
def test_remote_fatal_close_codes_block_without_reconnect(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = Harness()
        try:
            started = asyncio.create_task(harness.start(durable=True))
            first = await harness.gateway.wait_connection(0, WS_TIMEOUT)
            await _resumed(first)
            await started
            await first.close(code, "fatal")
            with pytest.raises(DiscordFatalCloseError):
                await asyncio.wait_for(harness.runtime.next_ingress(), WS_TIMEOUT)
            assert (await harness.runtime.health()).state == "blocked"
            assert harness.gateway.count == 1
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_remote_clean_close_gets_one_conservative_resume_then_identify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = Harness()
        try:
            started = asyncio.create_task(harness.start(durable=True))
            first = await harness.gateway.wait_connection(0, WS_TIMEOUT)
            await _resumed(first)
            await started
            await first.close(1000, "bye")
            ingress = asyncio.create_task(harness.runtime.next_ingress())
            second = await harness.gateway.wait_connection(1, WS_TIMEOUT)
            await second.send({"op": 10, "d": {"heartbeat_interval": 60000}})
            resume = await second.wait_for(lambda frame: frame.get("op") == 6, 0.5)
            assert resume["d"]["session_id"] == "sess-durable"
            await second.send({"op": 9, "d": False})
            third = await harness.gateway.wait_connection(2, WS_TIMEOUT)
            await third.send({"op": 10, "d": {"heartbeat_interval": 60000}})
            await third.wait_for(lambda frame: frame.get("op") == 2, 0.5)
            assert json.loads(harness.ledger.value or "{}").get("session_id") is None
            await third.send(
                {
                    "op": 0,
                    "s": 8,
                    "t": "READY",
                    "d": {
                        "session_id": "sess-clean",
                    },
                }
            )
            await third.send(_message(9))
            unit = await asyncio.wait_for(ingress, WS_TIMEOUT)
            assert unit.checkpoint.token == "9"
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_final_close_tombstones_closes_1000_and_never_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = Harness()
        try:
            started = asyncio.create_task(harness.start(durable=True))
            first = await harness.gateway.wait_connection(0, WS_TIMEOUT)
            await _resumed(first)
            await started
            outcome = await harness.runtime.close(2000)
            assert outcome.state == "closed"
            close = await first.wait_close(2.0)
            assert close is not None and close[0] == 1000
            assert json.loads(harness.ledger.value or "{}")["session_id"] is None
            assert harness.gateway.count == 1
            again = await harness.runtime.close(2000)
            assert again.state == "closed"
            await asyncio.sleep(0.05)
            assert harness.gateway.count == 1
        finally:
            await harness.stop()

    asyncio.run(scenario())


class _StaleOnceTransport:
    """Delegate everything, but raise one stale close observation on receive."""

    def __init__(
        self, inner: DiscordGatewayTransport, observation: WebSocketCloseObservation
    ) -> None:
        self._inner = inner
        self._pending = observation

    async def receive(self, *, max_frame_bytes: int) -> object:
        if self._pending is not None:
            pending, self._pending = self._pending, None
            raise DiscordConnectionClosed(pending)
        return await self._inner.receive(max_frame_bytes=max_frame_bytes)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_stale_generation_close_observation_cannot_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = Harness()
        try:
            started = asyncio.create_task(harness.start())
            first = await harness.gateway.wait_connection(0, WS_TIMEOUT)
            await _handshake(first)
            await started
            reconnect = asyncio.create_task(harness.runtime.reconnect("rotate"))
            second = await harness.gateway.wait_connection(1, WS_TIMEOUT)
            await _resumed(second)
            await reconnect
            assert harness.gateway.count == 2

            stale = WebSocketCloseObservation(
                generation=1,
                origin=WebSocketCloseOrigin.REMOTE,
                clean=False,
                rcvd_code=4000,
                rcvd_reason=None,
                sent_code=None,
                sent_reason=None,
                rcvd_then_sent=None,
                local_code=None,
                local_reason=None,
                opening_error=None,
            )
            inner = harness.runtime._transport
            harness.runtime._transport = _StaleOnceTransport(inner, stale)  # type: ignore[assignment]
            deliver = asyncio.create_task(harness.runtime.next_ingress())
            await asyncio.sleep(0.05)
            await second.send(_message(9))
            unit = await asyncio.wait_for(deliver, WS_TIMEOUT)
            assert unit.checkpoint.token == "9"
            assert harness.gateway.count == 2
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_gateway_url_allowlist_rejects_untrusted_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        production = DiscordGatewayTransport()
        try:
            for url in (
                "ws://gateway.discord.gg/?v=10&encoding=json",
                "wss://evil.example/?v=10&encoding=json",
                "wss://gateway.discord.gg.evil.example/",
                "wss://user:pass@gateway.discord.gg/",
                "https://gateway.discord.gg/",
            ):
                with pytest.raises((DiscordProtocolError, ValueError)):
                    production.validate_gateway_url(url)
            assert (
                production.validate_gateway_url("wss://gateway.discord.gg")
                == "wss://gateway.discord.gg/?v=10&encoding=json"
            )
            assert (
                production.validate_gateway_url(
                    "wss://us-east.gateway.discord.gg/?v=10&encoding=json"
                )
                == "wss://us-east.gateway.discord.gg/?v=10&encoding=json"
            )
            assert (
                production.validate_gateway_url(
                    "wss://gateway.discord.gg/?v=9&encoding=etf&compress=zlib-stream"
                )
                == "wss://gateway.discord.gg/?v=10&encoding=json"
            )
        finally:
            await production.aclose(asyncio.get_running_loop().time() + 1.0)

    asyncio.run(scenario())
