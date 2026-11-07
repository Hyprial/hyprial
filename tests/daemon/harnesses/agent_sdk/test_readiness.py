"""Readiness diagnostics observe waits; they do not set deadlines or retry."""
import asyncio
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.harnesses.agent_sdk import readiness
from hyprial.kernel import redact


@pytest.mark.anyio
async def test_wait_reports_are_bounded_and_do_not_complete_the_operation(monkeypatch):
    delays, events = [], []
    original_sleep = asyncio.sleep

    async def sleep(delay):
        delays.append(delay)
        await original_sleep(0)

    monkeypatch.setattr(readiness.asyncio, "sleep", sleep)
    logger = SimpleNamespace(info=lambda name, **data: events.append((name, data)))
    future = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(readiness.observe_readiness(future, logger=logger, stage="sdk-ready"))
    for _ in range(10):
        await original_sleep(0)
    assert delays == [10, 20, 30]
    assert len(events) == 4
    assert all(name == "worker.readiness.waiting" and data["stage"] == "sdk-ready" for name, data in events)
    assert not task.done()  # exhausted diagnostics do not end or retry the actual wait
    future.set_result("ready")
    assert await task == "ready"


@pytest.mark.anyio
async def test_original_failure_is_preserved_and_observer_stops():
    async def refuse():
        raise TimeoutError("existing readiness deadline")

    before = asyncio.all_tasks()
    with pytest.raises(TimeoutError, match="existing readiness deadline"):
        await readiness.observe_readiness(refuse(), logger=None, stage="sdk-ready")
    assert asyncio.all_tasks() == before


def test_source_log_allows_only_closed_non_secret_vocabulary():
    for source in ("env-grant", "agent-native", "missing"):
        assert redact({"credentialSource": source}) == {"credentialSource": source}
    for value in ("synthetic-secret", {"token": "synthetic"}, ["env-grant"]):
        assert redact({"credentialSource": value}) == {"credentialSource": "[REDACTED]"}
    assert redact({"credential": "env-grant"}) == {"credential": "[REDACTED]"}
