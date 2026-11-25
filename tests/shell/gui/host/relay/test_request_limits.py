"""Request bodies: exact size limits, strict JSON and the check order around the relay."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.shell.impl.gui.host.envelope import MAX_REQUEST_BYTES, MAX_SESSION_BYTES
from tests.shell.gui.host.support import FakeUpstream, run_gateway

DEEP_REQUEST = ('{"a":' * 20_000) + "1" + ("}" * 20_000)


def padded(limit: int) -> bytes:
    raw = b"{}" + b" " * (limit - 2)
    assert len(raw) == limit
    return raw


def test_a_method_body_of_exactly_the_limit_is_accepted_and_one_more_byte_is_refused() -> None:
    upstream = FakeUpstream()

    async def scenario(gateway) -> tuple[Any, Any]:
        opened = await gateway.open()
        exact = await gateway.call(
            "/daemon/request/agent.list", method="POST", raw_body=padded(MAX_REQUEST_BYTES), headers=opened.headers()
        )
        over = await gateway.call(
            "/daemon/request/agent.list",
            method="POST",
            raw_body=padded(MAX_REQUEST_BYTES + 1),
            headers=opened.headers(),
        )
        return exact, over

    exact, over = run_gateway(scenario, upstream=upstream)
    assert exact.status == 200
    assert (over.status, over.json["code"], over.json["dispatch"]) == (413, "HTTP_REQUEST_TOO_LARGE", "not-dispatched")
    assert [len(r.content) for r in upstream.posts()] == [MAX_REQUEST_BYTES]


def test_a_session_body_of_exactly_the_limit_is_read_and_one_more_byte_is_refused() -> None:
    async def scenario(gateway) -> tuple[Any, Any]:
        opened = await gateway.open()
        headers = opened.headers()
        exact = await gateway.call(
            "/daemon/session/status", method="POST", raw_body=padded(MAX_SESSION_BYTES), headers=headers
        )
        over = await gateway.call(
            "/daemon/session/status", method="POST", raw_body=padded(MAX_SESSION_BYTES + 1), headers=headers
        )
        return exact, over

    exact, over = run_gateway(scenario)
    assert (exact.status, exact.json.get("current")) == (200, True)
    assert (over.status, over.json["code"]) == (413, "HTTP_REQUEST_TOO_LARGE")


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_in_a_request_body_are_refused_before_dispatch(literal: str) -> None:
    upstream = FakeUpstream()

    async def scenario(gateway) -> Any:
        opened = await gateway.open()
        return await gateway.call(
            "/daemon/request/agent.create",
            method="POST",
            raw_body=f'{{"a":{literal}}}'.encode(),
            headers=opened.headers(),
        )

    reply = run_gateway(scenario, upstream=upstream)
    assert (reply.status, reply.json["code"], reply.json["dispatch"]) == (400, "INVALID_REQUEST", "not-dispatched")
    assert reply.json["method"] == "agent.create"
    assert upstream.posts() == []


def test_a_deeply_nested_request_body_is_a_400_not_a_500() -> None:
    upstream = FakeUpstream()

    async def scenario(gateway) -> Any:
        opened = await gateway.open()
        return await gateway.call(
            "/daemon/request/agent.create", method="POST", raw_body=DEEP_REQUEST.encode(), headers=opened.headers()
        )

    reply = run_gateway(scenario, upstream=upstream)
    assert (reply.status, reply.json["code"], reply.json["dispatch"]) == (400, "INVALID_REQUEST", "not-dispatched")
    assert upstream.posts() == []


def test_ordinary_json_numbers_and_nesting_still_relay_unchanged() -> None:
    upstream = FakeUpstream()
    body = {"a": [1, 2.5, -3, None, True], "b": {"c": "d"}}

    async def scenario(gateway) -> Any:
        opened = await gateway.open()
        return await gateway.call("/daemon/request/agent.create", method="POST", body=body, headers=opened.headers())

    assert run_gateway(scenario, upstream=upstream).status == 200
    assert json.loads(upstream.posts()[0].content) == body
