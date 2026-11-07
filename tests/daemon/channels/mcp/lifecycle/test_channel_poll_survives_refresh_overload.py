"""A refused session.refresh must not stop the channel from polling.

Production 0.5.0, 2026-10-05: while session persistence was overloaded, the
channel's session.refresh kept being rejected with
SESSION_PERSISTENCE_OVERLOADED.  The poll loop retried the refresh before every
poll, so it never polled again and no message woke the session for hours
(Allen's 14:59 message left no trace in the channel log until the overload
cleared).  The poll itself is fenced by sessionRef, so a superseded session is
still told SESSION_SUPERSEDED by the poll; the refresh is not a safety gate.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from hyprial.daemon.impl.inbox_watch import (
    InboxWatch,
    ListInbox,
    Ok,
    Outcome,
    Refresh,
    Register,
    Rejected,
    Request,
)
from hyprial.daemon.impl.mcp.api import SESSION_SUPERSEDED_CODE
from hyprial.daemon.impl.mcp.channel.loops import _run_channel_poll_loop

OVERLOADED = "SESSION_PERSISTENCE_OVERLOADED"


class _OverloadedRefreshAdapter:
    """Every list after the first sees a restarted daemon; every refresh is
    refused as overloaded -- the 0.5.0 shape."""

    actor = "agent:o:m:worker"
    session_ref = "ref"

    def __init__(self, *, list_script: list[Outcome] | None = None) -> None:
        self.watch = InboxWatch(
            poll_interval_ms=1, heartbeat_interval_ms=1_000, use_wait=False
        )
        self._list_script = list(list_script or [])
        self.polls = 0
        self.refreshes = 0

    async def perform(self, action: Request) -> Outcome:
        await anyio.sleep(0)
        if isinstance(action, Register):
            return Ok({"ok": True, "daemonEpoch": "e1"})
        if isinstance(action, Refresh):
            self.refreshes += 1
            return Rejected(OVERLOADED)
        assert isinstance(action, ListInbox)
        self.polls += 1
        if self._list_script:
            return self._list_script.pop(0)
        return Ok({"ok": True, "messages": [], "daemonEpoch": "e2"})

    async def apply(self, change: object, response: dict[str, Any]) -> None:
        pass

    async def report_turns(self, report_ids: tuple[str, ...]) -> dict[str, Any]:
        return {"ok": True}


async def _run_until(adapter: _OverloadedRefreshAdapter, predicate, seconds: float) -> None:
    with anyio.move_on_after(seconds):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                lambda: _run_channel_poll_loop(adapter, poll_interval=0.001)  # type: ignore[arg-type]
            )
            while not predicate():
                await anyio.sleep(0.005)
            tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_polling_continues_while_refresh_is_overloaded() -> None:
    adapter = _OverloadedRefreshAdapter()
    await _run_until(adapter, lambda: adapter.polls >= 4 and adapter.refreshes >= 2, 8.0)
    # The refresh stays overloaded throughout and is retried...
    assert adapter.refreshes >= 2
    # ...while polling (and therefore wake dispatch) keeps going regardless.
    assert adapter.polls >= 4, (adapter.polls, adapter.refreshes)


@pytest.mark.anyio
async def test_a_superseded_poll_still_ends_the_channel_during_refresh_overload() -> None:
    adapter = _OverloadedRefreshAdapter(
        list_script=[
            Ok({"ok": True, "messages": [], "daemonEpoch": "e2"}),
            Rejected(SESSION_SUPERSEDED_CODE),
        ]
    )
    with anyio.fail_after(8.0):
        await _run_channel_poll_loop(adapter, poll_interval=0.001)  # type: ignore[arg-type]
    assert adapter.polls == 2
    assert adapter.refreshes >= 1
