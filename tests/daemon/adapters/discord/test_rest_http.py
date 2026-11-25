"""Real loopback REST REDs: nonce, phase settlement, rate limits, reconcile."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.common.transport import HttpEffectState
from hyprial.daemon.impl.adapters.discord import (
    DiscordEndpointPolicy,
    DiscordGatewayRuntime,
    DiscordGatewayTransport,
    discord_nonce,
)
from hyprial.daemon.impl.adapters.contracts import freeze_json_v1

from .support import (
    Credentials,
    Ledger,
    LoopbackGateway,
    ScriptedHttpServer,
    discord_api_handler,
    instance_ref,
    json_response,
)

REST_TIMEOUT = 4.0


class _Sink:
    async def stage(self, unit: object) -> object:
        return None


@dataclass
class _Route:
    queue: deque
    default: object = None

    def __call__(self, exchange: object) -> object:
        if self.queue:
            return self.queue.popleft()
        return self.default


class RestHarness:
    def __init__(self, responses: list[object]) -> None:
        self.gateway = LoopbackGateway()
        self.route = _Route(deque(responses))
        self.http: ScriptedHttpServer | None = None
        self.runtime: DiscordGatewayRuntime | None = None
        self.transport: DiscordGatewayTransport | None = None

    async def start(self, **runtime_kwargs: object) -> DiscordGatewayRuntime:
        ws_url = await self.gateway.start()
        api = discord_api_handler(ws_url)

        def handler(exchange: object) -> object:
            path = getattr(exchange, "path", "")
            if path.startswith("/channels/"):
                return self.route(exchange)
            return api(exchange)

        self.http = ScriptedHttpServer(handler)
        http_base = await self.http.start()
        policy = DiscordEndpointPolicy(
            rest_base_url=http_base,
            gateway_hosts=("127.0.0.1",),
            gateway_schemes=("ws",),
        )
        self.transport = DiscordGatewayTransport(
            policy=policy,
            request_timeout=0.4,
            ws_open_timeout=1.0,
            ws_close_timeout=1.0,
        )
        self.runtime = DiscordGatewayRuntime(
            self.transport,
            Ledger("0"),
            Credentials(),
            "receiver:discord",
            **runtime_kwargs,
        )
        await self.runtime.start(instance_ref(), _Sink())
        return self.runtime

    async def stop(self) -> None:
        if self.runtime is not None:
            await self.runtime.close(2000)
        await self.gateway.stop()
        if self.http is not None:
            await self.http.stop()

    def channel_exchanges(self) -> list:
        assert self.http is not None
        return [e for e in self.http.exchanges if e.path.startswith("/channels/")]


def _operation(**overrides: object) -> SimpleNamespace:
    request = {
        "provider": "discord",
        "organization_id": "org-1",
        "instance_id": "discord-1",
        "generation": 3,
        "receiver_ref": "receiver:discord",
        "action": "create_message",
        "channel_id": "c-1",
        "content": "hello",
        "part_index": 0,
        **overrides,
    }
    return SimpleNamespace(
        sealed_request=freeze_json_v1(request), kind="text-send", idempotency_key="dc-1"
    )


async def _open_connection(harness: RestHarness) -> object:
    connection = await harness.gateway.wait_connection(0, REST_TIMEOUT)
    await connection.send({"op": 10, "d": {"heartbeat_interval": 60000}})
    await connection.send({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "sess-1"}})
    return connection


def _loopback_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def test_nonce_is_stable_digest_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    _loopback_env(monkeypatch)

    first = discord_nonce("operation-key-1")
    second = discord_nonce("operation-key-1")
    other = discord_nonce("operation-key-2")
    assert first == second
    assert first != other
    assert len(first) <= 25
    assert all(
        character in "0123456789abcdefghijklmnopqrstuvwxyz" for character in first
    )


def test_create_message_accepted_requires_native_id_enforce_nonce_and_bot_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = RestHarness(
            [json_response({"id": "m-out"}), json_response({"id": "m-out"})]
        )
        try:
            started = asyncio.create_task(harness.start())
            await _open_connection(harness)
            await started
            outcome = await harness.runtime.submit(_operation())
            assert outcome.submissions[0].state == "accepted"
            assert outcome.submissions[0].provider_message_id == "m-out"
            exchanges = harness.channel_exchanges()
            assert len(exchanges) == 1
            exchange = exchanges[0]
            assert exchange.method == "POST"
            assert exchange.path == "/channels/c-1/messages?enforce_nonce=true"
            assert exchange.headers.get("authorization") == "Bot discord-bot-token"
            body = json.loads(exchange.body)
            assert body["nonce"] == discord_nonce("dc-1")
            assert len(body["nonce"]) <= 25
            assert body["content"] == "hello"
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_malformed_success_and_effecting_5xx_stay_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = RestHarness(
            [json_response({}), (503, {}, b'{"message": "overloaded"}')]
        )
        try:
            started = asyncio.create_task(harness.start())
            await _open_connection(harness)
            await started
            malformed = await harness.runtime.submit(_operation(idempotency_key="k-1"))
            assert malformed.submissions[0].state == "unknown"
            assert malformed.submissions[0].provider_message_id is None
            overloaded = await harness.runtime.submit(_operation(idempotency_key="k-2"))
            assert overloaded.submissions[0].state == "unknown"
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_4xx_rejection_is_final(monkeypatch: pytest.MonkeyPatch) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = RestHarness([(403, {}, b'{"message": "Missing Permissions"}')])
        try:
            started = asyncio.create_task(harness.start())
            await _open_connection(harness)
            await started
            outcome = await harness.runtime.submit(_operation())
            assert outcome.submissions[0].state == "failed-final"
            assert outcome.submissions[0].definitely_not_sent is True
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_429_tracks_route_bucket_and_global_retry_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = RestHarness(
            [
                (
                    429,
                    {"x-ratelimit-bucket": "bucket-7"},
                    json.dumps({"retry_after": 2.5, "global": False}).encode(),
                ),
                json_response({"id": "m-2"}),
                (
                    429,
                    {},
                    json.dumps({"retry_after": 1.0, "global": True}).encode(),
                ),
                json_response({"id": "m-3"}),
            ]
        )
        try:
            started = asyncio.create_task(harness.start())
            await _open_connection(harness)
            await started

            limited = await harness.runtime.submit(_operation())
            assert limited.submissions[0].state == "failed-retryable"
            assert limited.retry_after_ms == 2500

            tracked = await harness.runtime.submit(_operation())
            assert tracked.submissions[0].state == "failed-retryable"
            assert tracked.retry_after_ms is not None and tracked.retry_after_ms > 2000
            assert len(harness.channel_exchanges()) == 1  # bucket blocks without HTTP

            other_channel = await harness.runtime.submit(_operation(channel_id="c-2"))
            assert other_channel.submissions[0].state == "accepted"
            assert len(harness.channel_exchanges()) == 2

            global_limited = await harness.runtime.submit(_operation(channel_id="c-3"))
            assert global_limited.submissions[0].state == "failed-retryable"
            assert global_limited.retry_after_ms == 1000
            global_blocked = await harness.runtime.submit(_operation(channel_id="c-4"))
            assert global_blocked.submissions[0].state == "failed-retryable"
            assert (
                global_blocked.retry_after_ms is not None
                and global_blocked.retry_after_ms > 500
            )
            assert len(harness.channel_exchanges()) == 3  # global blocks every route
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_post_body_timeout_is_unknown_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        harness = RestHarness([None])
        try:
            started = asyncio.create_task(harness.start())
            await _open_connection(harness)
            await started
            outcome = await harness.runtime.submit(_operation())
            assert outcome.submissions[0].state == "unknown"
            exchanges = harness.channel_exchanges()
            assert len(exchanges) == 1
            body = json.loads(exchanges[0].body)
            assert body["content"] == "hello"  # full body was delivered before the loss
            assert body["nonce"] == discord_nonce("dc-1")
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_reconcile_only_inside_retained_nonce_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    class MutableClock:
        def __init__(self) -> None:
            self.now = 1_000_000

        def __call__(self) -> int:
            return self.now

    async def scenario() -> None:
        clock = MutableClock()
        nonce = discord_nonce("dc-1")
        harness = RestHarness(
            [
                None,
                json_response(
                    [
                        {"id": "m-other", "nonce": discord_nonce("other")},
                        {"id": "m-late", "nonce": nonce},
                    ]
                ),
            ]
        )
        try:
            started = asyncio.create_task(harness.start(now_ms=clock))
            await _open_connection(harness)
            await started
            unknown = await harness.runtime.submit(_operation())
            assert unknown.submissions[0].state == "unknown"

            reconciled = await harness.runtime.reconcile(_operation(), unknown)
            assert reconciled.submissions[0].state == "accepted"
            assert reconciled.submissions[0].provider_message_id == "m-late"
            lookups = [
                exchange
                for exchange in harness.channel_exchanges()
                if exchange.method == "GET"
            ]
            assert len(lookups) == 1
            assert lookups[0].path == "/channels/c-1/messages?limit=100"
            assert "/lookup/" not in lookups[0].path  # only official Discord endpoints

            lost_again = await harness.runtime.submit(
                _operation(idempotency_key="k-late")
            )
            assert lost_again.submissions[0].state == "unknown"
            clock.now += 900_001  # outside the documented retention window
            stale = await harness.runtime.reconcile(
                _operation(idempotency_key="k-late"), lost_again
            )
            assert stale.submissions[0].state == "unknown"
            assert (
                len(harness.channel_exchanges()) == 3
            )  # no lookup HTTP outside the window
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_reconcile_after_restart_without_timestamp_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)

    async def scenario() -> None:
        first = RestHarness([None])
        try:
            started = asyncio.create_task(first.start())
            await _open_connection(first)
            await started
            unknown = await first.runtime.submit(_operation())
            assert unknown.submissions[0].state == "unknown"
            await first.runtime.close(1000)
        finally:
            await first.stop()

        second = RestHarness([])
        try:
            started = asyncio.create_task(second.start())
            await _open_connection(second)
            await started
            replayed = await second.runtime.reconcile(_operation(), unknown)
            assert replayed.submissions[0].state == "unknown"
            assert second.channel_exchanges() == []  # restart loses the nonce window
        finally:
            await second.stop()

    asyncio.run(scenario())


def test_effect_states_map_to_part_submission_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _loopback_env(monkeypatch)
    assert HttpEffectState.ACCEPTED.value == "accepted"
    assert HttpEffectState.UNKNOWN.value == "unknown"
    assert HttpEffectState.REJECTED.value == "rejected"
    assert HttpEffectState.RETRYABLE.value == "retryable"
