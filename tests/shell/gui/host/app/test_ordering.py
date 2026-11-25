"""Order and atomicity the contract pins: one grant wins a race, the second authenticate, refresh before probe."""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from typing import Any

import httpx

from hyprial.shell.impl.gui.host.session import SESSION_COOKIE, SessionStore, random_token
from tests.shell.gui.host.support import (
    GRANT,
    FakeClock,
    FakeUpstream,
    host_config,
    run_gateway,
)

RACERS = 8


def decoded_length(token: str) -> int:
    return len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def test_concurrent_requests_with_one_grant_yield_exactly_one_session() -> None:
    upstream = FakeUpstream()
    arrived = 0
    barrier: asyncio.Event | None = None

    class Rendezvous(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal arrived
            if request.url.path == "/readyz":
                arrived += 1
                if arrived == RACERS:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), 10)
            return upstream.handle(request)

    async def scenario(gateway) -> list[Any]:
        nonlocal barrier
        barrier = asyncio.Event()
        return await asyncio.gather(*(gateway.open() for _ in range(RACERS)))

    opened = run_gateway(scenario, upstream=Rendezvous(), config=host_config(max_sessions=RACERS * 2))
    statuses = sorted(o.reply.status for o in opened)
    assert statuses == [200] + [409] * (RACERS - 1)
    assert {o.reply.json["code"] for o in opened if o.reply.status != 200} == {"LAUNCH_GRANT_USED"}


def test_grant_consumption_is_atomic_across_threads() -> None:
    class SlowClock(FakeClock):
        def now_ms(self) -> int:
            time.sleep(0.01)
            return super().now_ms()

    store = SessionStore(host_config(), clock=SlowClock())
    start = threading.Barrier(RACERS)
    results: list[str] = []

    def consume() -> None:
        start.wait()
        results.append(store.consume_grant(GRANT))

    threads = [threading.Thread(target=consume) for _ in range(RACERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["ok"] + ["used"] * (RACERS - 1)


def test_every_secret_the_host_mints_is_32_random_bytes() -> None:
    tokens = {random_token() for _ in range(200)}
    assert len(tokens) == 200
    assert {decoded_length(token) for token in tokens} == {32}

    async def scenario(gateway) -> Any:
        return await gateway.open()

    opened = run_gateway(scenario)
    session = opened.session
    cookie_ids = [pair.split("=", 1)[1] for pair in opened.cookies.split("; ")]
    for value in (session["view"], session["generation"], session["csrf"], *cookie_ids):
        assert decoded_length(value) == 32, value


def test_a_session_revoked_while_the_body_is_read_is_never_relayed() -> None:
    upstream = FakeUpstream()

    async def scenario(gateway) -> tuple[int, dict[str, Any]]:
        opened = await gateway.open()
        session_id = dict(p.split("=", 1) for p in opened.cookies.split("; "))[SESSION_COOKIE]
        sent = {"host": "127.0.0.1:4173", "origin": "http://127.0.0.1:4173", "content-type": "application/json"}
        sent.update(opened.headers())
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/daemon/request/agent.create",
            "raw_path": b"/daemon/request/agent.create",
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in sent.items()],
            "server": ("127.0.0.1", 4173),
        }
        messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            gateway.app.store.revoke(session_id)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await gateway.app(scope, receive, send)
        import json

        start = next(m for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return start["status"], json.loads(body)

    status, payload = run_gateway(scenario, upstream=upstream)
    assert (status, payload["code"], payload["dispatch"]) == (401, "SESSION_REQUIRED", "not-dispatched")
    assert upstream.posts() == []


def test_refresh_checks_the_control_cookie_before_it_probes_the_upstream() -> None:
    upstream = FakeUpstream()

    async def scenario(gateway) -> list[tuple[int, str]]:
        opened = await gateway.open()
        seen = len(upstream.captured)
        upstream.ready_status = 503
        outcomes = []
        for headers in (
            {},
            {"cookie": "hyprial_gui_control=" + "x" * 43, "x-hyprial-view": str(opened.session["view"])},
            {"cookie": opened.cookies, "x-hyprial-view": "wrong"},
        ):
            reply = await gateway.call("/daemon/session", method="POST", body={}, headers=headers)
            outcomes.append((reply.status, reply.json["code"]))
        assert len(upstream.captured) == seen
        return outcomes

    assert run_gateway(scenario, upstream=upstream) == [(401, "SESSION_REQUIRED")] * 3
