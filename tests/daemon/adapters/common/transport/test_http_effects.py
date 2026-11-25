from __future__ import annotations

import asyncio

import httpx
import pytest

from hyprial.daemon.impl.adapters.common.transport import (
    HttpAccepted,
    HttpEffectState,
    HttpRequestPhase,
    classify_httpx_exception,
    execute_http_effect,
)

from .support import close_writer, read_request, serve_raw_http


def _exception(exception_type: type[Exception]) -> Exception:
    request = httpx.Request("POST", "https://example.invalid/effect")
    response = httpx.Response(503, request=request)
    if issubclass(exception_type, httpx.HTTPStatusError):
        return exception_type("status", request=request, response=response)
    if issubclass(exception_type, httpx.RequestError):
        return exception_type("request", request=request)
    try:
        return exception_type("local")
    except TypeError:
        return exception_type()


def test_httpx_documented_exception_surface_is_exhaustively_classified() -> None:
    documented = {
        name
        for name in httpx.__all__
        if isinstance(value := getattr(httpx, name), type)
        and issubclass(value, Exception)
        and name not in {"HTTPError", "RequestError", "TransportError", "TimeoutException", "NetworkError", "ProtocolError", "StreamError"}
    }
    expected = {
        "CloseError",
        "ConnectError",
        "ConnectTimeout",
        "CookieConflict",
        "DecodingError",
        "HTTPStatusError",
        "InvalidURL",
        "LocalProtocolError",
        "PoolTimeout",
        "ProxyError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "RequestNotRead",
        "ResponseNotRead",
        "StreamClosed",
        "StreamConsumed",
        "TooManyRedirects",
        "UnsupportedProtocol",
        "WriteError",
        "WriteTimeout",
    }
    assert documented == expected

    for name in sorted(documented):
        error = _exception(getattr(httpx, name))
        pre_send = classify_httpx_exception(error, HttpRequestPhase.PRE_SEND)
        after_admission = classify_httpx_exception(error, HttpRequestPhase.MAY_HAVE_SENT)
        assert pre_send.exception_type == name
        assert after_admission.exception_type == name
        if name in {
            "CookieConflict",
            "InvalidURL",
            "RequestNotRead",
            "ResponseNotRead",
            "StreamClosed",
            "StreamConsumed",
            "UnsupportedProtocol",
        }:
            assert pre_send.state is HttpEffectState.REJECTED
            assert pre_send.definitely_not_sent is True
        assert after_admission.state is HttpEffectState.UNKNOWN
        assert after_admission.definitely_not_sent is False


def test_response_cookie_conflict_is_settled_unknown_exactly_once() -> None:
    async def scenario() -> None:
        response = httpx.Response(
            200,
            headers=[
                ("set-cookie", "sid=first; Path=/"),
                ("set-cookie", "sid=second; Path=/effect"),
            ],
            request=httpx.Request("POST", "https://example.invalid/effect"),
        )
        settled = []

        async def operation(attempt):
            attempt.mark_may_have_sent()
            attempt.mark_response_received()
            return response

        def parse(response: httpx.Response) -> HttpAccepted:
            response.cookies.get("sid")
            raise AssertionError("duplicate response cookies must conflict")

        async def settle(observation) -> None:
            settled.append(observation)

        observation = await execute_http_effect(
            operation,
            effecting=True,
            parse_success=parse,
            settle=settle,
        )

        assert settled == [observation]
        assert observation.phase is HttpRequestPhase.RESPONSE_RECEIVED
        assert observation.state is HttpEffectState.UNKNOWN
        assert observation.definitely_not_sent is False
        assert observation.status_code == 200
        assert observation.native_id is None
        assert observation.exception_type == "CookieConflict"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("lifecycle_failure", "exception_type"),
    [
        ("not-read", "ResponseNotRead"),
        ("consumed", "StreamConsumed"),
        ("closed", "StreamClosed"),
    ],
)
def test_response_stream_lifecycle_failure_is_settled_unknown_exactly_once(
    lifecycle_failure: str,
    exception_type: str,
) -> None:
    async def scenario() -> None:
        response = httpx.Response(
            200,
            stream=httpx.ByteStream(b"response body"),
            request=httpx.Request("POST", "https://example.invalid/effect"),
        )
        settled = []

        async def operation(attempt):
            attempt.mark_may_have_sent()
            attempt.mark_response_received()
            return response

        def parse(response: httpx.Response) -> HttpAccepted:
            if lifecycle_failure == "not-read":
                response.content
            elif lifecycle_failure == "consumed":
                list(response.iter_raw())
                list(response.iter_raw())
            else:
                response.close()
                list(response.iter_raw())
            raise AssertionError("HTTPX must raise a response stream error")

        async def settle(observation) -> None:
            settled.append(observation)

        observation = await execute_http_effect(
            operation,
            effecting=True,
            parse_success=parse,
            settle=settle,
        )

        assert settled == [observation]
        assert observation.phase is HttpRequestPhase.RESPONSE_RECEIVED
        assert observation.state is HttpEffectState.UNKNOWN
        assert observation.definitely_not_sent is False
        assert observation.status_code == 200
        assert observation.native_id is None
        assert observation.exception_type == exception_type

    asyncio.run(scenario())


def test_real_http_loopback_translates_presend_drop_5xx_and_malformed_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        async def drop(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await read_request(reader)
            await close_writer(writer)

        drop_server, drop_url = await serve_raw_http(drop)
        unused_port = drop_server.sockets[0].getsockname()[1]
        drop_server.close()
        await drop_server.wait_closed()

        async with httpx.AsyncClient(timeout=0.1, trust_env=False) as client:
            with pytest.raises(httpx.ConnectError) as captured:
                await client.post(f"http://127.0.0.1:{unused_port}/effect")
            pre_send = classify_httpx_exception(
                captured.value, HttpRequestPhase.PRE_SEND
            )
            assert pre_send.state is HttpEffectState.RETRYABLE
            assert pre_send.definitely_not_sent is True

        drop_server, drop_url = await serve_raw_http(drop)
        try:
            async with httpx.AsyncClient(timeout=0.1, trust_env=False) as client:
                async def send_drop(attempt):
                    attempt.mark_may_have_sent()
                    return await client.post(f"{drop_url}/effect", content=b"effect")

                dropped = await execute_http_effect(
                    send_drop,
                    effecting=True,
                    parse_success=lambda _response: HttpAccepted("never"),
                )
            assert dropped.phase is HttpRequestPhase.MAY_HAVE_SENT
            assert dropped.state is HttpEffectState.UNKNOWN
            assert dropped.definitely_not_sent is False
        finally:
            drop_server.close()
            await drop_server.wait_closed()

        responses = iter(
            (
                b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n",
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}",
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 17\r\n\r\n{\"id\":\"native-1\"}",
            )
        )

        async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await read_request(reader)
            writer.write(next(responses))
            await writer.drain()
            await close_writer(writer)

        server, url = await serve_raw_http(respond)
        try:
            async with httpx.AsyncClient(timeout=0.2, trust_env=False) as client:
                async def send(attempt):
                    attempt.mark_may_have_sent()
                    response = await client.post(f"{url}/effect", content=b"effect")
                    attempt.mark_response_received()
                    return response

                def parse(response: httpx.Response) -> HttpAccepted:
                    value = response.json()
                    native_id = value.get("id")
                    if not isinstance(native_id, str) or not native_id:
                        raise ValueError("native id missing")
                    return HttpAccepted(native_id)

                unavailable = await execute_http_effect(send, effecting=True, parse_success=parse)
                malformed = await execute_http_effect(send, effecting=True, parse_success=parse)
                accepted = await execute_http_effect(send, effecting=True, parse_success=parse)
            assert unavailable.state is HttpEffectState.UNKNOWN
            assert unavailable.status_code == 503
            assert malformed.state is HttpEffectState.UNKNOWN
            assert malformed.reason_code == "http-success-malformed"
            assert accepted.state is HttpEffectState.ACCEPTED
            assert accepted.native_id == "native-1"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_cancellation_after_admission_settles_unknown_before_reraise() -> None:
    async def scenario() -> None:
        admitted = asyncio.Event()
        release = asyncio.Event()
        settled = []

        async def operation(attempt):
            attempt.mark_may_have_sent()
            admitted.set()
            await release.wait()
            raise AssertionError("unreachable")

        async def settle(observation) -> None:
            settled.append(observation)

        task = asyncio.create_task(
            execute_http_effect(
                operation,
                effecting=True,
                parse_success=lambda _response: HttpAccepted("unused"),
                settle=settle,
            )
        )
        await admitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(settled) == 1
        assert settled[0].phase is HttpRequestPhase.MAY_HAVE_SENT
        assert settled[0].state is HttpEffectState.UNKNOWN
        assert settled[0].reason_code == "http-effect-cancelled"

    asyncio.run(scenario())


@pytest.mark.parametrize("cancellation_count", [2, 4])
def test_repeated_cancellation_waits_for_durable_settlement_before_reraise(
    cancellation_count: int,
) -> None:
    async def scenario() -> None:
        settlement_started = asyncio.Event()
        release_settlement = asyncio.Event()
        settlement_complete = asyncio.Event()
        settled = []

        async def operation(attempt):
            attempt.mark_may_have_sent()
            attempt.mark_response_received()
            return httpx.Response(
                200,
                json={"id": "native-2"},
                request=httpx.Request("POST", "https://example.invalid/effect"),
            )

        async def settle(observation) -> None:
            settlement_started.set()
            await release_settlement.wait()
            settled.append(observation)
            settlement_complete.set()

        task = asyncio.create_task(
            execute_http_effect(
                operation,
                effecting=True,
                parse_success=lambda response: HttpAccepted(response.json()["id"]),
                settle=settle,
            )
        )
        await settlement_started.wait()
        completed_before_release = []
        for _ in range(cancellation_count):
            task.cancel()
            await asyncio.sleep(0)
            completed_before_release.append(task.done())

        release_settlement.set()
        await asyncio.wait_for(settlement_complete.wait(), timeout=1)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed_before_release == [False] * cancellation_count
        assert len(settled) == 1
        assert settled[0].state is HttpEffectState.ACCEPTED
        assert settled[0].phase is HttpRequestPhase.RESPONSE_RECEIVED

    asyncio.run(scenario())
