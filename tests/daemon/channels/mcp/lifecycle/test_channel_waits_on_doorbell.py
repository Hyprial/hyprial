"""The Channel poll loop holds on ``message.pending.wait`` instead of sleeping.

Allen, 2026-10-05: 「请现在去做事件推送的设计和实施」.  An idle Channel child
listed its inbox every 0.5 s.  With the doorbell, a held wait *is* the pacing:
the next list runs when the daemon says something changed or the hold ends.
The wait decides only when the next poll runs, never whether -- the lesson of
the refresh gate that silenced wakes for hours on 0.5.0 -- so a refused or
failed wait falls back to the ordinary interval and never stops polling.
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
    Request,
    Unreachable,
    Wait,
)
from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.daemon.impl.mcp import ClaudeChannelDriver, WakeCoordinator
from hyprial.daemon.impl.mcp.api import DaemonRequestRejected
from hyprial.daemon.impl.mcp.channel.adapter import ClaudeChannelAdapter
from hyprial.daemon.impl.mcp.channel.loops import _run_channel_poll_loop
from hyprial.kernel import ipc_errors


class _DoorbellAdapter:
    """The adapter surface the loop drives: perform, apply, and the core."""

    actor = "agent:o:m:worker"
    session_ref = "ref"

    def __init__(
        self,
        *,
        poll_interval_ms: int,
        hold: float = 0.0,
        held: bool = True,
        wait_outcome: Outcome | None = None,
    ) -> None:
        self.watch = InboxWatch(poll_interval_ms=poll_interval_ms, heartbeat_interval_ms=1_000)
        self.hold = hold
        self.held = held
        self.wait_outcome = wait_outcome
        self.polls = 0
        self.waits = 0
        self.refreshes = 0

    async def perform(self, action: Request) -> Outcome:
        if isinstance(action, Register):
            return Ok({"ok": True, "daemonEpoch": "e1"})
        if isinstance(action, Refresh):
            self.refreshes += 1
            return Ok({"ok": True, "daemonEpoch": "e1"})
        if isinstance(action, ListInbox):
            self.polls += 1
            await anyio.sleep(0)
            return Ok({"ok": True, "messages": [], "daemonEpoch": "e1"})
        assert isinstance(action, Wait)
        self.waits += 1
        await anyio.sleep(self.hold)
        if self.wait_outcome is not None:
            return self.wait_outcome
        return Ok({"ok": True, "changed": False, "held": self.held, "daemonEpoch": "e1"})

    async def apply(self, change: object, response: dict[str, Any]) -> None:
        pass

    async def report_turns(self, report_ids: tuple[str, ...]) -> dict[str, Any]:
        return {"ok": True}


async def _run_for(adapter: Any, seconds: float) -> None:
    with anyio.move_on_after(seconds):
        await _run_channel_poll_loop(
            adapter, poll_interval=adapter.watch.poll_interval_ms / 1000
        )


@pytest.mark.anyio
async def test_an_idle_channel_polls_once_per_hold_not_once_per_interval() -> None:
    # Each wait holds 0.2 s and ends without a change.  Plain polling at
    # 20 ms would list ~50 times in a second; held waits bound it to ~5.
    adapter = _DoorbellAdapter(poll_interval_ms=20, hold=0.2)
    await _run_for(adapter, 1.0)

    assert adapter.waits >= 3, (adapter.polls, adapter.waits)
    assert adapter.polls <= adapter.waits + 1, (adapter.polls, adapter.waits)


@pytest.mark.anyio
async def test_a_failing_wait_falls_back_to_polling_without_refresh() -> None:
    adapter = _DoorbellAdapter(poll_interval_ms=20, wait_outcome=Unreachable())
    await _run_for(adapter, 0.5)

    # Polling continues at the ordinary interval and the session is not
    # treated as lost: a failed doorbell is not a failed poll.
    assert adapter.polls >= 8, adapter.polls
    assert adapter.refreshes == 0


@pytest.mark.anyio
async def test_a_wait_that_did_not_hold_keeps_the_ordinary_pace() -> None:
    # Capacity full / older daemon: the wait answers at once, so the loop must
    # still sleep its interval rather than spin on list+wait.
    adapter = _DoorbellAdapter(poll_interval_ms=100, held=False)
    await _run_for(adapter, 0.55)

    assert adapter.polls <= 7, adapter.polls


class _Store:
    def load(self) -> DesiredState:
        return DesiredState(
            interactive_sessions=(
                InteractiveSession(
                    actor="agent:o:m:worker",
                    cwd="/tmp",
                    command=("claude",),
                    source="claude-channel",
                ),
            )
        )


class _Notifications:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.sent.append((method, params))


def _batch_answer(rows: list[dict[str, Any]], *, cursor: str) -> dict[str, Any]:
    members = [row for row in rows if isinstance(row.get("messageId"), str)]
    return {
        "ok": True,
        "schemaVersion": 1,
        "format": "inbox-batch-v1",
        "cursor": cursor,
        "daemonEpoch": "epoch-1",
        "actor": "agent:o:m:worker",
        "itemCount": len(members),
        "totalBodyBytes": 0,
        "inlineLimitBytes": 16384,
        # Rows the daemon named plus any it could not: the connector must skip
        # the latter and count it, never lose the ones after it.
        "items": rows,
        "delivery": {
            "mode": "inline",
            "text": f"[Fake batch cursor={cursor}]",
            "spillPath": None,
            "spillBytes": None,
            "spillSha256": None,
        },
    }


class _Proxy:
    def __init__(self, *, wait: Any) -> None:
        self.wait = wait
        self.calls: list[tuple[str, dict[str, Any]]] = []

    epoch = "epoch-1"

    async def call(self, *, actor: str, session_ref: str, method: str, params: dict[str, Any], mutation: bool) -> dict[str, Any]:
        self.calls.append((method, params))
        await anyio.sleep(0.005)  # a real IPC round trip yields
        if method == "message.pending.list":
            return {
                "ok": True,
                "messages": [{"messageId": "m-1", "deliveryId": "m-1"}],
                "daemonEpoch": self.epoch,
            }
        if method == "message.pending.batch":
            return _batch_answer(
                [{"messageId": "m-1", "deliveryId": "m-1"}], cursor="m-1"
            )
        if method in ("session.register", "session.refresh"):
            return {"ok": True, "daemonEpoch": self.epoch}
        if isinstance(self.wait, BaseException):
            raise self.wait
        return self.wait


class _Wakes:
    def pending_deliveries(self, actor: str) -> tuple[str, ...]:
        return ()

    def complete(self, *args: object) -> bool:
        return True

    def enqueue(self, *args: object, **kwargs: object) -> bool:
        return True

    def rearm(self, *args: object) -> int:
        return 0

    def enqueue_batch(self, *args: object, **kwargs: object) -> bool:
        return True

    def batch_members(self, *args: object, **kwargs: object) -> tuple[str, ...]:
        return ()

    def observe_batch_consumed(self, *args: object, **kwargs: object) -> bool:
        return False

    def rearm_batch(self, *args: object, **kwargs: object) -> bool:
        return False

    async def dispatch_due(self) -> list[object]:
        return []


def _adapter(proxy: _Proxy, *, poll_interval_ms: int = 500) -> ClaudeChannelAdapter:
    return ClaudeChannelAdapter(
        proxy,  # type: ignore[arg-type]
        _Wakes(),  # type: ignore[arg-type]
        actor="agent:o:m:worker",
        session_ref="ref",
        cwd="/tmp",
        command=("claude",),
        poll_interval_ms=poll_interval_ms,
    )


@pytest.mark.anyio
async def test_the_wait_names_what_the_last_poll_saw() -> None:
    proxy = _Proxy(wait={"ok": True, "changed": False, "held": True})
    adapter = _adapter(proxy)

    await adapter.poll_once()
    action = adapter.watch.poll_next(0)
    assert isinstance(action, Wait)
    assert isinstance(await adapter.perform(action), Ok)

    method, params = proxy.calls[-1]
    assert method == "message.pending.wait"
    assert params == {"knownMessageIds": ["m-1"]}


def _waits(proxy: _Proxy) -> int:
    return sum(1 for method, _ in proxy.calls if method == "message.pending.wait")


@pytest.mark.anyio
async def test_a_daemon_without_the_doorbell_is_asked_once() -> None:
    proxy = _Proxy(
        wait=DaemonRequestRejected(ipc_errors.METHOD_NOT_FOUND, "unknown daemon method")
    )
    adapter = _adapter(proxy, poll_interval_ms=20)

    with anyio.move_on_after(0.3):
        await _run_channel_poll_loop(adapter, poll_interval=0.02)

    lists = sum(1 for method, _ in proxy.calls if method == "message.pending.list")
    assert _waits(proxy) == 1
    assert lists >= 5, lists


@pytest.mark.anyio
async def test_an_upgraded_daemon_is_asked_again() -> None:
    # Channel children can start against an old daemon that autoupdates
    # later; the refusal must not outlive the daemon that gave it.
    proxy = _Proxy(
        wait=DaemonRequestRejected(ipc_errors.METHOD_NOT_FOUND, "unknown daemon method")
    )
    adapter = _adapter(proxy, poll_interval_ms=20)
    with anyio.move_on_after(0.2):
        await _run_channel_poll_loop(adapter, poll_interval=0.02)
    assert _waits(proxy) == 1  # same daemon: not re-asked

    proxy.epoch = "epoch-2"
    proxy.wait = {"ok": True, "changed": False, "held": True}
    with anyio.move_on_after(0.2):
        await _run_channel_poll_loop(adapter, poll_interval=0.02)

    assert _waits(proxy) >= 2


class _RowsProxy(_Proxy):
    """A list answer whose row the coordinator cannot accept."""

    async def call(self, *, actor: str, session_ref: str, method: str, params: dict[str, Any], mutation: bool) -> dict[str, Any]:
        if method == "message.pending.list":
            self.calls.append((method, params))
            await anyio.sleep(0.005)
            return {
                "ok": True,
                "messages": [
                    {"messageId": "m-bad", "deliveryId": ""},
                    {"messageId": "m-1", "deliveryId": "d-1"},
                ],
                "daemonEpoch": self.epoch,
            }
        if method == "message.pending.batch":
            self.calls.append((method, params))
            await anyio.sleep(0.005)
            # The daemon is authoritative: a row it cannot name is simply not a
            # member, and the named one still wakes.
            return _batch_answer(
                [{"deliveryId": ""}, {"messageId": "m-1", "deliveryId": "d-1"}],
                cursor="d-1",
            )
        return await super().call(
            actor=actor, session_ref=session_ref, method=method, params=params, mutation=mutation
        )


@pytest.mark.anyio
async def test_a_malformed_row_is_skipped_and_the_good_row_still_wakes() -> None:
    # Reviews 841/844 on #1152: an empty deliveryId made the coordinator
    # raise; first that ended the poll task, then it silenced every row after
    # it.  The bad row is skipped and counted; the good one beside it wakes.
    proxy = _RowsProxy(
        wait=DaemonRequestRejected(ipc_errors.METHOD_NOT_FOUND, "unknown daemon method")
    )
    notifications = _Notifications()
    adapter = ClaudeChannelAdapter(
        proxy,  # type: ignore[arg-type]
        WakeCoordinator(
            _Store(),  # type: ignore[arg-type]
            ClaudeChannelDriver(notifications),
        ),
        actor="agent:o:m:worker",
        session_ref="ref",
        cwd="/tmp",
        command=("claude",),
    )
    errors: list[BaseException] = []
    with anyio.move_on_after(0.3):
        await _run_channel_poll_loop(
            adapter, poll_interval=0.02, on_error=lambda error, _n: errors.append(error)
        )

    lists = sum(1 for method, _ in proxy.calls if method == "message.pending.list")
    assert lists >= 3, lists
    assert [params["meta"]["delivery_id"] for _m, params in notifications.sent] == ["d-1"]
    assert adapter.watch.counters["deliver.invalid"] >= 1
    assert errors == []


@pytest.mark.anyio
async def test_a_tool_call_that_learns_superseded_silences_the_lanes() -> None:
    proxy = _Proxy(wait={"ok": True, "changed": False, "held": True})
    adapter = _adapter(proxy)
    await adapter.poll_once()

    async def superseded(**_kwargs: Any) -> dict[str, Any]:
        raise DaemonRequestRejected("SESSION_SUPERSEDED", "a newer session owns it")

    proxy.call = superseded  # type: ignore[method-assign]
    with pytest.raises(DaemonRequestRejected):
        await adapter.harness_reply("m-1", "done", "done")

    assert adapter.watch.quiet


@pytest.mark.anyio
async def test_a_cancelled_request_leaves_the_core_ready_for_the_next_one() -> None:
    adapter = _DoorbellAdapter(poll_interval_ms=20, hold=30.0)  # a wait that never returns
    with anyio.move_on_after(0.2):
        await _run_channel_poll_loop(adapter, poll_interval=0.02)
    assert adapter.waits == 1

    # The cancelled wait was settled, so the lane can ask for its next action.
    assert adapter.watch.poll_next(10**12).op != "wait"


class _UnusableListAdapter(_DoorbellAdapter):
    """Every list answers, and every answer fails to apply."""

    def __init__(self) -> None:
        super().__init__(poll_interval_ms=20, wait_outcome=Unreachable())
        self.applies = 0

    async def apply(self, change: object, response: dict[str, Any]) -> None:
        self.applies += 1
        raise TypeError("daemon pending response messages must be an array")


@pytest.mark.anyio
async def test_a_reowed_rewake_keeps_the_ordinary_pace() -> None:
    # Review 848 on #1152: re-owing the re-wake reset the list timer, so a
    # daemon that kept answering an unusable list was re-listed at once,
    # every time (764 lists/s at 1 ms IPC instead of ~42).
    adapter = _UnusableListAdapter()
    adapter.watch.request_rewake("recovery")
    await _run_for(adapter, 0.3)

    # 20 ms interval over 0.3 s: about 15 lists, never hundreds.
    assert adapter.polls <= 25, adapter.polls
    # ...and the re-wake is still owed, not dropped.
    assert adapter.watch.counters.get("rewake.recovery", 0) >= 2


class _QuietDuringListAdapter(_DoorbellAdapter):
    """A tool learns SUPERSEDED while the poll lane's list is in flight."""

    def __init__(self) -> None:
        super().__init__(poll_interval_ms=20)
        self.applies = 0

    async def perform(self, action: Request) -> Outcome:
        outcome = await super().perform(action)
        if isinstance(action, ListInbox) and self.polls == 2:
            self.watch.observe_rejection("SESSION_SUPERSEDED")
        return outcome

    async def apply(self, change: object, response: dict[str, Any]) -> None:
        self.applies += 1


@pytest.mark.anyio
async def test_a_list_that_settles_into_a_quiet_session_is_not_applied() -> None:
    adapter = _QuietDuringListAdapter()
    with anyio.fail_after(2):
        await _run_channel_poll_loop(adapter, poll_interval=0.02)

    assert adapter.polls == 2
    assert adapter.applies == 1  # the first list only
