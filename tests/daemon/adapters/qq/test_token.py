"""Token manager and onboarding wire: shape, timing, singleflight, custody."""

from __future__ import annotations

import asyncio

from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.contracts import OnboardingAttempt, VerifiedCallbackRequest

from .support import (
    APP_ID,
    APP_SECRET,
    BOT_ID,
    LocalHTTP,
    ManualClock,
    ManualSleep,
    build_components,
    json_response,
)


def _token_handler(
    server: LocalHTTP,
    script: list[tuple[str | None, int]],
) -> None:
    """Serve one scripted outcome per fetch: (token, expires_in) or (None, status)."""
    state = {"index": 0}

    async def respond(request: object) -> tuple[int, dict[str, str], bytes]:
        assert request.method == "POST"  # type: ignore[attr-defined]
        assert request.path == "/app/getAppAccessToken"  # type: ignore[attr-defined]
        assert request.header("authorization") is None  # type: ignore[attr-defined]
        value, expires_or_status = script[min(state["index"], len(script) - 1)]
        state["index"] += 1
        if value is None:
            return json_response(expires_or_status, {"error": f"secret echo {APP_SECRET}"})
        return json_response(200, {"access_token": value, "expires_in": expires_or_status})

    server.responder = respond


async def _requests_reach(http: LocalHTTP, count: int, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while len(http.requests) < count:
            await asyncio.sleep(0.002)


async def _delays_reach(sleeper: ManualSleep, count: int, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while len(sleeper.delays) < count:
            await asyncio.sleep(0.002)


def test_token_wire_shape_camel_case_and_cache(tmp_path: Path) -> None:
    async def scenario(tmp_path: Path) -> None:
        http = LocalHTTP()
        await http.start()
        try:
            _token_handler(http, [("tok-1", 7200)])
            http.requests.clear()
            components = build_components(
                http=http,
                gateway_url=None,
                store_path=tmp_path / "store.sqlite",  # type: ignore[operator]
                authority=None,  # type: ignore[arg-type]
            )
            token = components.token
            assert await token.access_token() == "tok-1"
            assert len(http.requests) == 1
            body = http.requests[0].json()
            assert body == {"appId": APP_ID, "clientSecret": APP_SECRET}
            assert http.requests[0].header("content-type") == "application/json"
            assert await token.access_token() == "tok-1"
            assert len(http.requests) == 1
            await token.stop()
            components.store.close()
            await components.owner.aclose(asyncio.get_running_loop().time() + 2)
        finally:
            await http.stop()

    asyncio.run(scenario(tmp_path))


def test_token_singleflight_coalesces_concurrent_fetches(tmp_path: Path) -> None:
    async def scenario(tmp_path: Path) -> None:
        http = LocalHTTP()
        await http.start()
        try:
            gate = asyncio.Event()

            async def respond(request: object) -> tuple[int, dict[str, str], bytes]:
                await gate.wait()
                return json_response(200, {"access_token": "tok-1", "expires_in": 7200})

            http.responder = respond
            components = build_components(
                http=http,
                gateway_url=None,
                store_path=tmp_path / "store.sqlite",  # type: ignore[operator]
                authority=None,  # type: ignore[arg-type]
            )
            token = components.token
            first = asyncio.create_task(token.access_token())
            second = asyncio.create_task(token.access_token())
            await asyncio.sleep(0)
            gate.set()
            assert await asyncio.gather(first, second) == ["tok-1", "tok-1"]
            assert len(http.requests) == 1
            await token.stop()
            components.store.close()
            await components.owner.aclose(asyncio.get_running_loop().time() + 2)
        finally:
            await http.stop()

    asyncio.run(scenario(tmp_path))


def test_token_forced_background_refresh_timing(tmp_path: Path) -> None:
    async def scenario(tmp_path: Path) -> None:
        http = LocalHTTP()
        await http.start()
        try:
            cases = [((7200, 0.0), 6900.0), ((120, 30.0), 60.0), ((1000, 10.0), 690.0)]
            for (expires_in, jitter), expected_delay in cases:
                _token_handler(http, [("tok-1", expires_in)])
                http.requests.clear()
                clock = ManualClock()
                sleeper = ManualSleep()
                components = build_components(
                    http=http,
                    gateway_url=None,
                    store_path=tmp_path / f"store-{expires_in}.sqlite",  # type: ignore[operator]
                    authority=None,  # type: ignore[arg-type]
                    clock=clock,
                    sleeper=sleeper,
                    jitter=lambda jitter=jitter: jitter,
                )
                token = components.token
                token.start_background()
                await _requests_reach(http, 1)
                await _delays_reach(sleeper, 1)
                assert sleeper.delays == [expected_delay], sleeper.delays
                clock.advance(expected_delay)
                sleeper.advance()
                await _requests_reach(http, 2)
                await token.stop()
                components.store.close()
                await components.owner.aclose(asyncio.get_running_loop().time() + 2)
        finally:
            await http.stop()

    asyncio.run(scenario(tmp_path))


def test_token_retains_valid_token_on_early_failure_and_retries_five_seconds(tmp_path: Path) -> None:
    async def scenario(tmp_path: Path) -> None:
        http = LocalHTTP()
        await http.start()
        components = None
        try:
            _token_handler(
                http,
                [("tok-1", 7200), (None, 500), (None, 500), ("tok-2", 7200)],
            )
            clock = ManualClock()
            sleeper = ManualSleep()
            components = build_components(
                http=http,
                gateway_url=None,
                store_path=tmp_path / "store.sqlite",  # type: ignore[operator]
                authority=None,  # type: ignore[arg-type]
                clock=clock,
                sleeper=sleeper,
                jitter=lambda: 0.0,
            )
            token = components.token
            assert await token.access_token() == "tok-1"
            token.start_background()
            await _requests_reach(http, 1)
            clock.advance(6900)
            sleeper.advance()
            await _requests_reach(http, 2)
            assert await token.access_token() == "tok-1", "valid token retained on failure"
            await _delays_reach(sleeper, 2)
            assert sleeper.delays[-1] == 5.0, "bounded 5s retry after failed refresh"
            sleeper.advance()
            await _requests_reach(http, 3)
            await _delays_reach(sleeper, 3)
            assert sleeper.delays[-1] == 5.0
            sleeper.advance()
            await _requests_reach(http, 4)
            await _delays_reach(sleeper, 4)
            assert sleeper.delays[-1] == 6900.0
            assert await token.access_token() == "tok-2"
        finally:
            try:
                if components is not None:
                    await components.token.stop()
                    components.store.close()
                    await components.owner.aclose(asyncio.get_running_loop().time() + 2)
            finally:
                await http.stop()

    asyncio.run(scenario(tmp_path))


def test_token_blocks_after_expiry_until_fetch_recovers(tmp_path: Path) -> None:
    async def scenario(tmp_path: Path) -> None:
        http = LocalHTTP()
        await http.start()
        try:
            from hyprial.daemon.impl.adapters.qq.errors import QQTokenUnavailable

            _token_handler(http, [("tok-1", 100), (None, 500), ("tok-2", 7200)])
            clock = ManualClock()
            components = build_components(
                http=http,
                gateway_url=None,
                store_path=tmp_path / "store.sqlite",  # type: ignore[operator]
                authority=None,  # type: ignore[arg-type]
                clock=clock,
                sleeper=ManualSleep(),
                jitter=lambda: 0.0,
            )
            token = components.token
            assert await token.access_token() == "tok-1"
            clock.advance(101)
            with pytest.raises(QQTokenUnavailable):
                await token.access_token()
            await _requests_reach(http, 2)
            assert await token.access_token() == "tok-2"
            await token.stop()
            components.store.close()
            await components.owner.aclose(asyncio.get_running_loop().time() + 2)
        finally:
            await http.stop()

    asyncio.run(scenario(tmp_path))


def test_token_error_never_carries_secret_or_response_body(tmp_path: Path) -> None:
    async def scenario(tmp_path: Path) -> None:
        http = LocalHTTP()
        await http.start()
        try:
            from hyprial.daemon.impl.adapters.qq.errors import QQProtocolError

            async def respond(request: object) -> tuple[int, dict[str, str], bytes]:
                return json_response(400, {"detail": f"leak {APP_SECRET}"})

            http.responder = respond
            components = build_components(
                http=http,
                gateway_url=None,
                store_path=tmp_path / "store.sqlite",  # type: ignore[operator]
                authority=None,  # type: ignore[arg-type]
            )
            token = components.token
            with pytest.raises(QQProtocolError) as caught:
                await token.access_token()
            assert APP_SECRET not in str(caught.value)
            assert APP_SECRET not in repr(caught.value)
            assert "leak" not in str(caught.value)
            await token.stop()
            components.store.close()
            await components.owner.aclose(asyncio.get_running_loop().time() + 2)
        finally:
            await http.stop()

    asyncio.run(scenario(tmp_path))


def test_token_stop_cancels_and_joins_background_task(tmp_path: Path) -> None:
    async def scenario(tmp_path: Path) -> None:
        http = LocalHTTP()
        await http.start()
        components = None
        try:
            _token_handler(http, [("tok-1", 7200)])
            sleeper = ManualSleep()
            components = build_components(
                http=http,
                gateway_url=None,
                store_path=tmp_path / "store.sqlite",  # type: ignore[operator]
                authority=None,  # type: ignore[arg-type]
                sleeper=sleeper,
                jitter=lambda: 0.0,
            )
            token = components.token
            token.start_background()
            background = token._background
            assert background is not None
            await asyncio.sleep(0.01)
            assert token.background_running
            await token.stop()
            await token.stop()
            assert not token.background_running
            assert background.done()
        finally:
            if components is not None:
                components.store.close()
            await http.stop()

    asyncio.run(scenario(tmp_path))


def test_onboarding_uses_official_endpoints_and_hides_proactive_support(tmp_path: Path) -> None:
    async def scenario(tmp_path: Path) -> None:
        from hyprial.daemon.impl.adapters.qq.provider import QQOfficialBotOnboarding
        from .support import StaticCredentials

        http = LocalHTTP()
        await http.start()
        try:

            async def respond(request: object) -> tuple[int, dict[str, str], bytes]:
                if request.path == "/app/getAppAccessToken":  # type: ignore[attr-defined]
                    return json_response(200, {"access_token": "tok-1", "expires_in": 7200})
                assert request.method == "GET"  # type: ignore[attr-defined]
                assert request.path == "/users/@me"  # type: ignore[attr-defined]
                assert request.header("authorization") == "QQBot tok-1"  # type: ignore[attr-defined]
                return json_response(200, {"id": BOT_ID, "username": "hyprial", "bot": True})

            http.responder = respond
            components = build_components(
                http=http,
                gateway_url=None,
                store_path=tmp_path / "store.sqlite",  # type: ignore[operator]
                authority=None,  # type: ignore[arg-type]
            )
            onboarding = QQOfficialBotOnboarding(
                components, StaticCredentials(), "org-1", "qq-1", "main"
            )
            qr = await onboarding.inspect({"variant": "qr", "credential_ref": "receiver:qq"})
            assert qr.state == "unsupported"
            assert qr.reason_codes == ("qq-qr-license-unavailable",)
            ready = await onboarding.inspect({"variant": "official-bot", "credential_ref": "receiver:qq"})
            assert ready.state == "ready"
            identity = await onboarding.authenticate(OnboardingAttempt("a-1", {}, "receiver:qq"))
            assert identity.account_scope == APP_ID
            assert identity.stable_account_id == BOT_ID
            capabilities = await onboarding.probe_capabilities(identity)
            facts = {fact.name: fact for fact in capabilities.facts}
            assert facts["proactive-send"].support == "unsupported"
            assert facts["qr-onboarding"].support == "unsupported"
            assert facts["threads"].support == "unsupported"
            assert facts["reactions"].support == "unknown"
            assert facts["files"].support == "native"
            assert facts["files"].permission != "granted" or facts["files"].reason_code
            await components.token.stop()
            components.store.close()
        finally:
            await http.stop()

    asyncio.run(scenario(tmp_path))


def test_runtime_rejects_callbacks_from_official_gateway(tmp_path: Path) -> None:
    from hyprial.daemon.impl.adapters.qq.provider import QQOfficialBotRuntime

    async def scenario(tmp_path: Path) -> None:
        http = LocalHTTP()
        await http.start()
        try:
            from .support import MemoryLedger, StaticCredentials, build_components

            components = build_components(
                http=http,
                gateway_url="ws://127.0.0.1:1/unused",
                store_path=tmp_path / "store.sqlite",  # type: ignore[operator]
                authority=None,  # type: ignore[arg-type]
            )
            runtime = QQOfficialBotRuntime(
                components, MemoryLedger(), StaticCredentials(), "receiver:qq"
            )
            with pytest.raises(ValueError):
                await runtime.accept_callback(
                    VerifiedCallbackRequest("qq-1", 2, "proof", {}, {})
                )
            components.store.close()
        finally:
            await http.stop()

    asyncio.run(scenario(tmp_path))
