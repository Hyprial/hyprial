"""A wake that races the client going away must report, not crash.

``ClaudeChannelDriver.wake`` catches transport failures and turns them into
``WakeAttempt(FAILED)`` so the poll loop survives -- Claude Code cannot respawn
a dead stdio MCP child, so an escape from that loop orphans the channel for the
rest of the session.  The catch listed ``ConnectionError, OSError,
RuntimeError``, which is every way a *socket* reports a dead peer and no way an
``anyio`` memory stream does: ``BrokenResourceError`` inherits straight from
``Exception``.

So the one exception the stdio write stream actually raises was the one that
got through.  It did, on CI run 776 (task 2681), where it took down
``test_held_open_client_session_survives_daemon_restart`` -- a test whose whole
subject is surviving exactly this window.

These tests pin the boundary from both sides.  Widening a bare ``except`` is
the kind of fix that quietly swallows real defects, so the last test asserts
what must still be allowed to crash: ``BusyResourceError`` means two tasks are
writing frames at once, which breaks the serialization ``StdioChannelNotifier``
exists to provide, and a silent retry there would interleave the wire.
"""

from __future__ import annotations

import anyio
import pytest

from hyprial.daemon.impl.mcp.channel.notifier import ClaudeChannelDriver, StdioChannelNotifier
from hyprial.daemon.impl.mcp.wake import WakeCommand, WakeStatus


def _command() -> WakeCommand:
    return WakeCommand(
        kind="formal",
        actor="alice",
        delivery_id="delivery-1",
        cwd="/work/alice",
        command=("claude",),
        prompt="call harness_read",
    )


@pytest.mark.anyio
async def test_the_premise_holds_anyio_raises_these_and_they_are_not_oserror() -> None:
    """Pin what the SDK actually raises, so this file cannot guard nothing.

    Every other test here asserts a *catch*.  If anyio one day stopped raising
    ``BrokenResourceError`` on a dead receiver, those tests would keep passing
    while guarding an exception that never occurs again -- green for a reason
    that no longer exists.  This one fails instead, and names why.
    """

    send, receive = anyio.create_memory_object_stream(1)
    await receive.aclose()
    with pytest.raises(anyio.BrokenResourceError):
        await send.send("anything")

    send2, _receive2 = anyio.create_memory_object_stream(1)
    await send2.aclose()
    with pytest.raises(anyio.ClosedResourceError):
        await send2.send("anything")

    assert not issubclass(anyio.BrokenResourceError, OSError), (
        "anyio.BrokenResourceError now inherits from OSError, so the original "
        "except tuple in ClaudeChannelDriver.wake would already have caught "
        "it; this file's reason for existing has changed and it should be "
        "re-read rather than kept passing"
    )
    assert not issubclass(anyio.ClosedResourceError, OSError)


@pytest.mark.anyio
async def test_a_wake_after_the_client_end_is_gone_is_reported_not_raised() -> None:
    """The CI failure, reduced to its window and made deterministic.

    Run 776 needed a daemon restart to line up with a 20ms poll tick.  Closing
    the receive end is the same state with the timing removed.
    """

    send, receive = anyio.create_memory_object_stream(1)
    await receive.aclose()
    driver = ClaudeChannelDriver(StdioChannelNotifier(send))

    attempt = await driver.wake(_command())

    assert attempt.status == WakeStatus.FAILED
    assert attempt.detail == "BrokenResourceError", (
        "a failed wake with an empty detail reports the same thing as a wake "
        "that failed for an unstated reason; BrokenResourceError carries no "
        f"message, so the class name has to stand in for it: {attempt.detail!r}"
    )


@pytest.mark.anyio
async def test_a_wake_after_our_own_end_is_closed_is_reported_not_raised() -> None:
    """The other half of the same window.

    Teardown closes both ends.  Which error a racing wake sees depends only on
    which end closed first, and nothing in this process chooses that order --
    so catching one and not the other leaves the identical crash reachable from
    the identical situation.
    """

    send, _receive = anyio.create_memory_object_stream(1)
    await send.aclose()
    driver = ClaudeChannelDriver(StdioChannelNotifier(send))

    attempt = await driver.wake(_command())

    assert attempt.status == WakeStatus.FAILED
    assert attempt.detail == "ClosedResourceError"


@pytest.mark.anyio
async def test_a_live_stream_still_accepts_and_the_notification_arrives() -> None:
    """The positive control.

    Without it, the two tests above are equally explained by "wake always
    returns FAILED" -- and a driver that never delivers anything would pass
    them both.
    """

    send, receive = anyio.create_memory_object_stream(1)
    driver = ClaudeChannelDriver(StdioChannelNotifier(send))

    attempt = await driver.wake(_command())

    assert attempt.status == WakeStatus.ACCEPTED
    delivered = receive.receive_nowait()
    assert delivered.message.method == "notifications/claude/channel"
    assert delivered.message.params["meta"] == {"delivery_id": "delivery-1"}


@pytest.mark.anyio
async def test_a_concurrent_writer_still_crashes_loudly() -> None:
    """The boundary of the widening, asserted rather than described.

    ``BusyResourceError`` is not a verdict about the peer -- it says two tasks
    are sending frames into one stream, which is the exact invariant
    ``StdioChannelNotifier`` is named for.  Turning that into a FAILED attempt
    would convert a corrupted wire into a retry nobody sees.
    """

    class BusyNotifier:
        async def notify(self, method: str, params: dict[str, object]) -> None:
            raise anyio.BusyResourceError("sending")

    driver = ClaudeChannelDriver(BusyNotifier())

    with pytest.raises(anyio.BusyResourceError):
        await driver.wake(_command())
