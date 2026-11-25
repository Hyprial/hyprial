"""Shared actor-engine port fixtures for kernel actor tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hyprial.kernel.impl.actor_runtime.port import ActorHost, create_actor_host


@pytest.fixture
def actor_host() -> Iterator[ActorHost]:
    """A fresh P5 host over one engine, shut down after the test."""

    host = create_actor_host()
    try:
        yield host
    finally:
        host.shutdown(5.0)
