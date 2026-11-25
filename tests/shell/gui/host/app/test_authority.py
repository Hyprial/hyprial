"""In-process authority rotation: how the session layer reacts when the source hands it a new config."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx

from tests.shell.gui.host.support import (
    FakeUpstream,
    host_config,
    run_gateway,
)

SECOND_ENDPOINT = "http://127.0.0.1:8792"


class Source:
    """The in-process authority: each ``rotate`` publishes one new immutable config."""

    def __init__(self) -> None:
        self.config = host_config(endpoint="http://127.0.0.1:8791")
        self.broken = False

    def __call__(self):
        if self.broken:
            raise RuntimeError("the holder failed")
        return self.config

    def rotate(self, **overrides) -> None:
        self.config = replace(self.config, endpoint=SECOND_ENDPOINT, **overrides)


class Routing:
    """Two fake daemons told apart by endpoint, so rotation can be observed."""

    def __init__(self) -> None:
        self.first = FakeUpstream()
        self.second = FakeUpstream()
        self.gate: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None

    def transport(self) -> httpx.AsyncBaseTransport:
        class Router(httpx.AsyncBaseTransport):
            def __init__(self, outer: "Routing") -> None:
                self.outer = outer

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                outer = self.outer
                target = outer.second if request.url.port == 8792 else outer.first
                if target is outer.first and request.url.path == "/daemon/request/agent.create" and outer.gate:
                    outer.entered.set()
                    await outer.gate.wait()
                return target.handle(request)

        return Router(self)


def test_rotation_revokes_business_sessions_and_a_refresh_keeps_the_view() -> None:
    source = Source()
    routing = Routing()

    async def scenario(gateway) -> None:
        routing.gate, routing.entered = asyncio.Event(), asyncio.Event()
        initial = await gateway.open()
        assert initial.reply.status == 200
        headers = initial.headers()

        async def check():
            return await gateway.call("/daemon/session/status", method="POST", body={}, headers=headers)

        assert (await check()).json["current"] is True
        pending = asyncio.create_task(
            gateway.call("/daemon/request/agent.create", method="POST", body={}, headers=headers)
        )
        await routing.entered.wait()
        source.rotate()
        assert (await check()).json["current"] is False
        routing.gate.set()
        changed = await pending
        assert changed.json["code"] == "GUI_SESSION_CHANGED" and changed.json["dispatch"] == "unknown"
        assert changed.status == 409

        refused = await gateway.call("/daemon/request/agent.create", method="POST", body={}, headers=headers)
        assert (refused.status, refused.json["dispatch"]) == (401, "not-dispatched")

        refreshed = await gateway.call("/daemon/session", method="POST", body={}, headers=headers)
        assert refreshed.status == 200
        assert refreshed.json["view"] == initial.session["view"]
        assert refreshed.json["generation"] != initial.session["generation"]
        assert refreshed.json["csrf"] != initial.session["csrf"]

        spoof = await gateway.call(
            "/daemon/session/status", method="POST", body={}, headers={**headers, "x-hyprial-view": "wrong"}
        )
        assert spoof.status == 401
        source.broken = True
        assert (await check()).status == 503

    run_gateway(scenario, upstream=routing.transport(), config=source.config, authority=source)
    assert [r for r in routing.second.captured if r.method == "POST"] == []


def test_a_source_that_raises_fails_closed_with_a_503_on_every_route() -> None:
    source = Source()

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        headers = opened.headers()
        source.broken = True
        for call in (
            lambda: gateway.call("/daemon/nothing"),
            lambda: gateway.call("/daemon/session/status", method="POST", body={}, headers=headers),
            lambda: gateway.call("/daemon/readyz", headers=headers),
        ):
            refused = await call()
            assert (refused.status, refused.json["code"], refused.json["dispatch"]) == (
                503, "GUI_AUTHORITY_UNAVAILABLE", "not-dispatched",
            )

        method = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=headers)
        assert (method.status, method.json["code"], method.json["dispatch"]) == (
            503, "GUI_AUTHORITY_UNAVAILABLE", "not-dispatched",
        )
        wrong_host = await gateway.call("/daemon/nothing", host="localhost:9999")
        assert (wrong_host.status, wrong_host.json["code"]) == (403, "HTTP_HOST_DENIED")
        no_origin = await gateway.call("/daemon/nothing", method="POST", body={}, omit_origin=True)
        assert (no_origin.status, no_origin.json["code"]) == (403, "ORIGIN_REQUIRED")

    run_gateway(scenario, config=source.config, authority=source)


def test_without_an_authority_source_the_initial_config_is_the_authority() -> None:
    async def scenario(gateway) -> None:
        opened = await gateway.open()
        status = await gateway.call("/daemon/session/status", method="POST", body={}, headers=opened.headers())
        assert status.json["current"] is True

    run_gateway(scenario)


def test_the_rotated_authority_changes_the_upstream_and_bearer_the_next_probe_uses() -> None:
    source = Source()
    routing = Routing()

    async def scenario(gateway) -> None:
        first = await gateway.open()
        assert first.reply.status == 200
        source.rotate(upstream_token="second-token")
        refreshed = await gateway.call("/daemon/session", method="POST", body={}, headers=first.headers())
        assert refreshed.status == 200

    run_gateway(scenario, upstream=routing.transport(), config=source.config, authority=source)
    assert routing.second.captured and routing.second.captured[0].headers["authorization"] == "Bearer second-token"
