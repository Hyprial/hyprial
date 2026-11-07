"""Shared builders for the recap §7 suite on the unified-hooks path (H2).

The suite moved from ``tests/daemon/turns/hooks/recap_writer`` with its
assertions intact: recap is now a ``biz/hooks/recap`` consumer driven by the
H1 daemon bus, configured by ``hooks.json`` schema 2 instead of the retired
``turn-hooks.json``.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hyprial.biz import RecapHookConsumer
from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.hooks import DaemonHookService
from hyprial.kernel import HookBus

from hyprial.kernel.impl.contracts.daemon.daemon_teardown import (
    TURN_HOOK_CLOSE_TIMEOUT_SECONDS,
)

RECIPIENT = "agent:owner:node:worker"


class _Harness:
    """Track everything the recap §7 suite must settle before teardown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.buses: list[HookBus] = []
        self.consumers: list[RecapHookConsumer] = []

    def register(self, bus: HookBus, consumer: RecapHookConsumer) -> None:
        with self._lock:
            self.buses.append(bus)
            self.consumers.append(consumer)

    def settle(self) -> None:
        with self._lock:
            buses, consumers = list(self.buses), list(self.consumers)
            self.buses, self.consumers = [], []
        for bus in buses:
            assert bus.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)
        for consumer in consumers:
            assert consumer.close(TURN_HOOK_CLOSE_TIMEOUT_SECONDS)


RECAP_TURN_EVENTS = ("turn.completed", "turn.failed")


def write_hooks_config(
    home: Path, events: tuple[str, ...] = RECAP_TURN_EVENTS
) -> Path:
    """Write a hooks.json v2 recap entry (the migration target of ``{"recap": true}``)."""

    config = home / "config" / "hooks.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    previous = config.stat().st_mtime_ns if config.exists() else time.time_ns()
    entries = (
        [{"events": list(events), "consumer": "recap"}] if events else []
    )
    config.write_text(
        json.dumps({"schemaVersion": 2, "hooks": entries}), encoding="utf-8"
    )
    changed = max(time.time_ns(), previous + 1)
    import os

    os.utime(config, ns=(changed, changed))
    return config


def build_service(
    home: Path,
    *,
    queue_size: int = 256,
    logger: Callable[..., None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[HookBus, DaemonHookService, RecapHookConsumer]:
    """One daemon bus plus the real recap consumer, as the shell wires them."""

    bus = HookBus(logger=logger, monotonic=monotonic)
    consumer = RecapHookConsumer(
        home_for_actor=lambda _actor: home,
        logger=logger,
        monotonic=monotonic,
        queue_size=queue_size,
    )
    service = DaemonHookService(
        bus=bus,
        config_path_for_agent=lambda _actor: home / "config" / "hooks.json",
        consumers={"recap": consumer},
        handler_notifier=lambda *_args, **_kwargs: None,
        logger=logger,
        monotonic=monotonic,
    )
    prime_configuration(service)
    return bus, service, consumer


def prime_configuration(service: DaemonHookService) -> None:
    """Wait until the service has loaded RECIPIENT's config once."""

    service._has_route(RECIPIENT, "turn.completed")  # noqa: SLF001
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with service._config_lock:  # noqa: SLF001
            loaded = RECIPIENT in service._configs  # noqa: SLF001
            refreshing = RECIPIENT in service._config_refreshing  # noqa: SLF001
        if loaded and not refreshing:
            return
        time.sleep(0.005)
    raise AssertionError("hook configuration was not loaded before timeout")


def observe(
    service: DaemonHookService,
    delivery_id: str,
    status: HarnessResultStatus = HarnessResultStatus.COMPLETED,
    *,
    output: str = "",
    tool_names: tuple[str, ...] = (),
    failure_code: str | None = None,
) -> None:
    service.observe_turn(
        HarnessDelivery(
            delivery_id, "conversation", "agent:owner:node:sender", RECIPIENT, "body"
        ),
        HarnessResult(
            delivery_id,
            RECIPIENT,
            status,
            output=output,
            failure_code=failure_code,
        ),
        started_at_ms=1,
        ended_at_ms=2,
        tool_names=tool_names,
    )


def wait_for_recap_lines(path: Path, count: int) -> list[str]:
    deadline = time.monotonic() + 1
    while True:
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        if len(lines) >= count:
            return lines
        assert time.monotonic() < deadline
        time.sleep(0.005)


def read_records(path: Path, count: int) -> list[Mapping[str, Any]]:
    return [json.loads(line) for line in wait_for_recap_lines(path, count)]


__all__ = [
    "RECAP_TURN_EVENTS",
    "RECIPIENT",
    "TURN_HOOK_CLOSE_TIMEOUT_SECONDS",
    "build_service",
    "observe",
    "prime_configuration",
    "read_records",
    "wait_for_recap_lines",
    "write_hooks_config",
]
