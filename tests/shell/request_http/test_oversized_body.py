"""Bounded discard before a complete 413 response; no JSON parsing or dispatch."""
from __future__ import annotations

import time
from types import SimpleNamespace

import anyio
import pytest
from starlette.requests import Request

from hyprial.shell.impl.request_http.app import _discard_oversized_body


def _server():
    return SimpleNamespace(config=SimpleNamespace(budgets=SimpleNamespace(
        max_request_bytes=10, request_timeout=0.2, header_timeout=0.02,
    )))


def _request(receive, *, started=None):
    return Request({
        "type": "http", "state": {"started": time.monotonic() if started is None else started},
    }, receive)


def test_discards_chunks_without_materializing_a_body():
    messages = [
        {"type": "http.request", "body": b"x" * 8, "more_body": True},
        {"type": "http.request", "body": b"x" * 8, "more_body": False},
    ]

    async def receive():
        return messages.pop(0)

    request = _request(receive)
    anyio.run(_discard_oversized_body, _server(), request, 16)
    assert messages == []
    assert not hasattr(request, "_body")


@pytest.mark.parametrize("length,expired", [(21, False), (16, True)])
def test_excessive_length_or_expired_deadline_never_reads(length, expired):
    async def receive():
        pytest.fail("must not read beyond the discard budget")

    request = _request(receive, started=time.monotonic() - 1 if expired else None)
    anyio.run(_discard_oversized_body, _server(), request, length)


def test_stalled_sender_is_bounded_by_the_existing_timeout():
    async def receive():
        await anyio.sleep(1)
        pytest.fail("discard did not enforce its timeout")

    started = time.monotonic()
    anyio.run(_discard_oversized_body, _server(), _request(receive), 16)
    assert time.monotonic() - started < 0.5


def test_disconnect_stops_discarding():
    async def receive():
        return {"type": "http.disconnect"}

    anyio.run(_discard_oversized_body, _server(), _request(receive), 16)
