"""Bounded observation only: no new deadline and no retry decisions."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import time


def report_wait(logger, stage: str, elapsed: float) -> None:
    if logger is not None:
        with suppress(OSError):
            logger.info("worker.readiness.waiting", stage=stage, elapsedSeconds=round(elapsed, 3))


async def observe_readiness(awaitable, *, logger, stage: str):
    started = time.monotonic()
    report_wait(logger, stage, 0)

    async def diagnostic():
        for delay in (10, 20, 30):
            await asyncio.sleep(delay)
            report_wait(logger, stage, time.monotonic() - started)

    observer = asyncio.create_task(diagnostic())
    try:
        return await awaitable
    finally:
        observer.cancel()
        with suppress(asyncio.CancelledError):
            await observer
