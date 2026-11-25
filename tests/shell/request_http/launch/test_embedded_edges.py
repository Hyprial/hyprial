"""``RequestHttpServer(listen=False)``: the address, refusal and concurrency edges.

Decision: spike-2822-in-process-asgi-20261119, B1 of 2822.  Restored from the
spike: the Host header is the only address check (a hostile host or origin is
refused), every refusal is a plain response equal to the socket's, the app needs
no lifespan and never runs the daemon call on the loop, the concurrency budget
still answers 429, and the session host's ``UpstreamClient`` works over the
ASGI transport (a wrong bearer is ``UPSTREAM_AUTH_REJECTED``).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import threading
import time
from typing import Any

import httpx
import pytest

from hyprial.shell.impl.gui.host.config import HostConfig, Principal
from hyprial.shell.impl.gui.host.envelope import HostFailure
from hyprial.shell.impl.gui.host.upstream import UpstreamClient
from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.shell.request_http.launch.support import (
    ACTOR,
    BASE,
    acall,
    daemon_pair,
    embedded,
    over_socket,
    status_of,
)
from tests.shell.request_http.rebind.support import OWNER, gui_config


def test_host_header_is_the_only_address_check(tmp_path) -> None:
    server, _, _ = embedded(tmp_path)
    server.start()

    async def code(base: str, **headers: str) -> tuple[int, str | None]:
        status, body = await acall(server.asgi_app(), "GET", "/healthz", None, base=base, headers=headers)
        return status, body.get("code")

    async def run() -> dict[str, tuple[int, str | None]]:
        out: dict[str, tuple[int, str | None]] = {}
        for base in (BASE, "http://127.0.0.1:1", "http://127.0.0.1:59999", "http://localhost"):
            out[base] = await code(base)
        for base in ("http://testserver", "http://[::1]:1", "http://evil.example", "http://127.0.0.1.evil.example"):
            out[base] = await code(base)
        out["origin"] = await code(BASE, Origin="http://127.0.0.1:1")
        out["evil-origin"] = await code(BASE, Origin="http://evil.example")
        out["host-override"] = await code(BASE, Host="127.0.0.1:7")
        out["evil-host-override"] = await code(BASE, Host="evil.example")
        return out

    try:
        got = asyncio.run(run())
    finally:
        server.shutdown()
    for allowed in (BASE, "http://127.0.0.1:1", "http://127.0.0.1:59999", "http://localhost", "host-override"):
        assert got[allowed] == (200, None)
    for hostile in ("http://testserver", "http://[::1]:1", "http://evil.example", "http://127.0.0.1.evil.example", "evil-host-override"):
        assert got[hostile] == (403, "HTTP_HOST_DENIED")
    assert got["origin"] == got["evil-origin"] == (403, "HTTP_ORIGIN_DENIED")


def test_refusals_are_plain_responses_and_match_the_socket(tmp_path) -> None:
    server, _, _ = embedded(tmp_path / "e")
    server.start()
    _, gate = daemon_pair(tmp_path / "r")
    real = RequestHttpServer(gui_config(tmp_path / "r", owner=OWNER), request_once=gate)
    real.start()
    cases: list[tuple[str, str, str | None, Any]] = [
        ("GET", "/readyz", None, None),
        ("GET", "/readyz", "wrong", None),
        ("GET", "/daemon/request", "wrong", None),
        ("GET", "/openapi.json", "wrong", None),
        ("POST", "/daemon/request/link.list", None, {"from": ACTOR}),
        ("POST", "/daemon/request/link.list", "wrong", {"from": ACTOR}),
        ("POST", "/daemon/request/link.add", "wrong", {"peer": "agent:alice", "onBehalfOf": ACTOR}),
        ("GET", "/healthz?x=1", "ok", None),
        ("GET", "/daemon/request?x=1", "ok", None),
        ("POST", "/daemon/request/link.list?x=1", "ok", {"from": ACTOR}),
        ("GET", "//healthz", "ok", None),
        ("GET", "/healthz/", "ok", None),
        ("GET", "/nope", "ok", None),
        ("POST", "/daemon/request/link%2Elist", "ok", {"from": ACTOR}),
        ("GET", "/daemon/request/link.list", "ok", None),
        ("POST", "/healthz", "ok", None),
    ]
    try:
        in_process = [
            status_of(server.asgi_app(), m, p, server.token if t == "ok" else t, b) for m, p, t, b in cases
        ]
        through_socket = [over_socket(real, m, p, b, real.token if t == "ok" else t) for m, p, t, b in cases]
    finally:
        real.shutdown()
        server.shutdown()
    assert in_process == through_socket
    codes = [(status, body.get("code")) for status, body in in_process]
    assert codes[:7] == [(401, "HTTP_UNAUTHORIZED")] * 7
    assert codes[7:11] == [(400, "INVALID_REQUEST")] * 4
    assert codes[11][0] == codes[12][0] == 404
    assert status_of(server.asgi_app(), "GET", "/healthz", None)[0] in {200, 503}


def test_no_lifespan_and_the_daemon_call_never_runs_on_the_loop(tmp_path) -> None:
    server, daemon, _ = embedded(tmp_path)
    server.start()
    release, entered = threading.Event(), threading.Event()
    threads: list[int] = []

    def slow(params: dict[str, Any]) -> dict[str, Any]:
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return {
            "ok": True,
            "participant": params["from"],
            "conversations": [],
            "nextCursor": None,
            "recordAvailable": True,
        }

    daemon.handlers["conversations.list"] = slow

    async def run() -> tuple[int, int, int]:
        loop_thread = threading.get_ident()
        ticks = 0
        pending = asyncio.ensure_future(
            acall(server.asgi_app(), "POST", "/daemon/request/conversations.list", server.token, {})
        )
        deadline = time.monotonic() + 10.0
        while not entered.is_set():
            assert time.monotonic() < deadline, "the handler was never entered"
            await asyncio.sleep(0.005)
        for _ in range(20):
            await asyncio.sleep(0.005)
            ticks += 1
        status, _ = await acall(server.asgi_app(), "GET", "/healthz", None)
        assert status == 200 and not pending.done()
        release.set()
        result = await pending
        return result[0], ticks, loop_thread

    try:
        status, ticks, loop_thread = asyncio.run(run())
    finally:
        release.set()
        server.shutdown()
    assert status == 200 and ticks == 20
    assert threads and loop_thread not in threads
    assert not hasattr(server.asgi_app().state, "lifespan_started")


def test_concurrency_budget_is_still_enforced_in_process(tmp_path) -> None:
    server, daemon, _ = embedded(tmp_path)
    server.start()
    budget = server.config.budgets.max_concurrent
    release = threading.Event()
    started = threading.Semaphore(0)

    def hold(params: dict[str, Any]) -> dict[str, Any]:
        started.release()
        assert release.wait(10)
        return {
            "ok": True,
            "participant": params["from"],
            "conversations": [],
            "nextCursor": None,
            "recordAvailable": True,
        }

    daemon.handlers["conversations.list"] = hold

    async def run() -> tuple[int, str | None]:
        pending = [
            asyncio.ensure_future(
                acall(server.asgi_app(), "POST", "/daemon/request/conversations.list", server.token, {})
            )
            for _ in range(budget)
        ]
        deadline = time.monotonic() + 10.0
        for _ in range(budget):
            while not started.acquire(blocking=False):
                assert time.monotonic() < deadline, "the budget was never filled"
                await asyncio.sleep(0.005)
        status, body = await acall(server.asgi_app(), "GET", "/healthz", None)
        release.set()
        await asyncio.gather(*pending)
        return status, body.get("code")

    try:
        assert asyncio.run(run()) == (429, "HTTP_BUSY")
    finally:
        release.set()
        server.shutdown()


def _host_config(token: str) -> HostConfig:
    return HostConfig(
        driver="daemon",
        principal=Principal(ACTOR, OWNER, "daemon-node"),
        endpoint="http://127.0.0.1:1",
        upstream_token=token,
        launch_grant="g",
        handoff_path=None,
        publication_path=None,
        launch_ttl_ms=60000,
        session_ttl_ms=60000,
        max_sessions=4,
    )


def test_the_session_host_upstream_client_works_over_the_asgi_transport(tmp_path) -> None:
    server, _, _ = embedded(tmp_path)
    server.start()

    async def run() -> dict[str, Any]:
        upstream = UpstreamClient(transport=httpx.ASGITransport(app=server.asgi_app()))
        out: dict[str, Any] = {}
        try:
            config = _host_config(server.token)
            authority = await upstream.describe(config)
            out["authority"] = (authority.driver, authority.phase)
            read = await upstream.relay(
                config, path="/daemon/request/conversations.list", method="POST", incoming_request_id="req-1",
                body=b"{}", write=False, pinned_digest=authority.catalog_digest,
                expected_method="conversations.list", fallback_request_id=None,
            )
            out["read"] = (read.status, read.headers["x-hyprial-dispatch"])
            bad = replace(config, upstream_token="wrong")
            relayed = await upstream.relay(
                bad, path="/daemon/request/conversations.list", method="POST", incoming_request_id=None,
                body=b"{}", write=False, pinned_digest=authority.catalog_digest,
                expected_method=None, fallback_request_id="fallback-3",
            )
            out["wrong-bearer"] = (relayed.status, json.loads(relayed.body)["code"])
            with pytest.raises(HostFailure) as refused:
                await upstream.describe(bad)
            out["describe-bad"] = refused.value.code
        finally:
            await upstream.aclose()
        return out

    try:
        got = asyncio.run(run())
    finally:
        server.shutdown()
    assert got["authority"] == ("daemon", "ready")
    assert got["read"] == (200, "dispatched")
    assert got["wrong-bearer"] == (401, "HTTP_UNAUTHORIZED")
    assert got["describe-bad"] == "UPSTREAM_AUTH_REJECTED"
