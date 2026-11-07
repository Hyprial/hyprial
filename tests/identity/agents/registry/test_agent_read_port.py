"""Bounded read effects preserve a fresh grant query and home I/O isolation."""

from __future__ import annotations

import threading
import time

import pytest

from hyprial.identity.impl.agents.state.read_port import AgentReadBusy, AgentReadEffectPort


class Registry:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def home_receipt(self, actor):
        self.entered.set()
        assert self.release.wait(3.0)
        return actor

    def capability_grants(self, _actor):
        return ()

    def secret_inventory(self, _actor):
        return ()

    def workspace_path(self, actor):
        return f"/test/{actor}/workspace"

    def activity_hints(self, actor):
        return {
            "createdAtMs": 42,
            "hasLastSessionId": actor == "bound",
            "agentHomeMtimeMs": None,
        }


def test_read_effect_capacity_is_bounded_while_home_validation_blocks() -> None:
    registry = Registry()
    port = AgentReadEffectPort(registry, capacity=1, workers=1, call_timeout=3.0)
    results: list[str] = []
    first = threading.Thread(target=lambda: results.append(port.validate_home("first")))
    second = threading.Thread(target=lambda: results.append(port.validate_home("second")))
    first.start()
    try:
        assert registry.entered.wait(1.0)
        second.start()
        deadline = time.monotonic() + 1.0
        while port._queue.qsize() < 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with pytest.raises(AgentReadBusy):
            port.validate_home("third")
    finally:
        registry.release.set()
        first.join(3.0)
        second.join(3.0)
        assert port.close(3.0)
    assert sorted(results) == ["first", "second"]


def test_workspace_path_and_activity_hints_use_read_only_bounded_port() -> None:
    registry = Registry()
    port = AgentReadEffectPort(registry, capacity=1, workers=1, call_timeout=3.0)
    try:
        assert port.workspace_path("owner") == "/test/owner/workspace"
        assert port.activity_hints("bound").to_payload() == {
            "createdAtMs": 42,
            "hasLastSessionId": True,
            "agentHomeMtimeMs": None,
        }
    finally:
        assert port.close(3.0)


def test_read_port_cannot_mutate_workspace() -> None:
    port = AgentReadEffectPort(Registry(), capacity=1, workers=1)
    try:
        assert not hasattr(port, "ensure_workspace")
    finally:
        assert port.close(3.0)
