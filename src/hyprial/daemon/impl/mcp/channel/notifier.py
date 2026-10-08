"""Channel notification edge: notifier protocol, stdio notifier, driver."""
from __future__ import annotations

import json
import re

import logging
from typing import Any, Protocol, runtime_checkable

import anyio
from mcp import types
from mcp.shared.message import SessionMessage


from hyprial.daemon.impl.mcp.wake  import WakeAttempt, WakeCommand, WakeStatus

_logger = logging.getLogger(__name__)

CHANNEL_CAPABILITY = "claude/channel"

CHANNEL_NOTIFICATION = "notifications/claude/channel"

@runtime_checkable
class ChannelNotifier(Protocol):
    async def notify(self, method: str, params: dict[str, Any]) -> None: ...

class StdioChannelNotifier:
    """Serialize unsolicited channel notifications on the SDK stdio stream."""

    def __init__(self, write_stream: Any) -> None:
        self._write_stream = write_stream

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write_stream.send(
            SessionMessage(
                types.JSONRPCNotification(
                    jsonrpc="2.0",
                    method=method,
                    params=params,
                )
            )
        )

def _scrub(text: str) -> str:
    """Replace lone UTF-16 surrogates, which the stdio writer cannot encode."""

    return "".join(
        "\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in text
    )


def _meta_json(value: object) -> str:
    """One channel-meta attribute value: compact, stable JSON text.

    Encodability is :func:`repair_channel_params`' job, which counts it.
    """

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


#: Claude Code channels reference, "Notification format"
#: (code.claude.com/docs/en/channels-reference): ``content`` is a string,
#: ``meta`` an optional ``Record<string, string>`` whose keys "must be
#: identifiers: letters, digits, and underscores only" -- other keys "are
#: silently dropped".  A non-string value is rejected outright, and that
#: rejection drops the stdio connection (production 0.5.0, 2026-10-05).
_META_KEY = re.compile(r"[A-Za-z0-9_]+")


def repair_channel_params(params: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Make a channel notification safe to write, or say it cannot be.

    Returns the params to send (None when ``content`` is not a string -- the
    one defect no repair can fix) and what was repaired.  Repairing instead
    of refusing matters: a refused wake stays at the head of its actor's
    queue and is retried forever, blocking every wake behind it.
    """

    content = params.get("content")
    if not isinstance(content, str):
        return None, ("content is not a string",)
    problems: list[str] = []
    repaired_content = _scrub(content)
    if repaired_content != content:
        problems.append("content had unencodable characters")
    meta = params.get("meta")
    repaired_meta: dict[str, str] = {}
    if meta is not None and not isinstance(meta, dict):
        problems.append("meta was not an object")
    elif isinstance(meta, dict):
        for key, value in meta.items():
            if not isinstance(key, str) or not _META_KEY.fullmatch(key):
                problems.append(f"meta key {key!r} is not an identifier")
                continue
            if not isinstance(value, str):
                problems.append(f"meta.{key} was {type(value).__name__}")
                value = _meta_json(value)
            scrubbed = _scrub(value)
            if scrubbed != value:
                problems.append(f"meta.{key} had unencodable characters")
            repaired_meta[key] = scrubbed
    repaired: dict[str, Any] = {"content": repaired_content}
    if repaired_meta or meta is not None:
        repaired["meta"] = repaired_meta
    return repaired, tuple(problems)


class ClaudeChannelDriver:
    """Write a minimal formal edge to Claude's local stdio MCP connection."""

    def __init__(self, notifier: ChannelNotifier) -> None:
        self._notifier = notifier
        #: Wakes whose payload had to be repaired before Claude Code would
        #: accept it, and wakes that could not be sent at all.
        self.repaired_payloads = 0
        self.invalid_payloads = 0

    async def wake(self, command: WakeCommand) -> WakeAttempt:
        params, problems = repair_channel_params(self._params(command))
        if params is None:
            self.invalid_payloads += 1
            _logger.warning(
                "channel wake for delivery %s not sent: %s",
                command.delivery_id,
                "; ".join(problems),
            )
            return WakeAttempt(
                WakeStatus.FAILED, "invalid channel payload: " + "; ".join(problems)
            )
        if problems:
            self.repaired_payloads += 1
            _logger.warning(
                "channel wake for delivery %s repaired: %s",
                command.delivery_id,
                "; ".join(problems),
            )
        try:
            await self._notifier.notify(CHANNEL_NOTIFICATION, params)
        # anyio's stream errors do not inherit from OSError, so the tuple below
        # used to let BrokenResourceError straight through -- out of the wake
        # call, out of dispatch_due, out of poll_once, and out of the poll task,
        # which killed the whole channel session. That escape is what red-lined
        # test_held_open_client_session_survives_daemon_restart on CI run 776
        # (task 2681). The invariant it breaks is already stated at the top of
        # this module: an unguarded raise here orphans the channel, and Claude
        # Code cannot respawn a dead stdio MCP child.
        #
        # What this except now swallows that it did not before, one at a time:
        #   BrokenResourceError -- every receiver of the stdio stream is gone,
        #       or the pipe broke. That is precisely what ConnectionError
        #       already means on this line; anyio simply does not spell it as
        #       an OSError.
        #   ClosedResourceError -- *our* end was closed, which happens only
        #       while the session is being torn down. It is the symmetric half
        #       of the same window: which of the two a racing wake observes
        #       depends on the order the two ends close in, and neither order
        #       is under this code's control. Catching one and not the other
        #       would leave the same crash reachable from the other direction.
        #
        # Deliberately still fatal, because these are defects in us rather than
        # verdicts about the transport:
        #   BusyResourceError -- two tasks sending concurrently, which breaks
        #       the serialization this notifier exists to provide. Swallowing
        #       it would turn interleaved frames into a silent retry.
        #   EndOfStream -- receive-side only; send never raises it, and if that
        #       ever changes we want to hear about it.
        except (
            ConnectionError,
            OSError,
            RuntimeError,
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ) as error:
            # BrokenResourceError carries no message, so str() is "". A detail
            # of "" says "failed, no reason given" -- the same output a genuine
            # empty reason would produce. Fall back to the class name so the
            # two stay distinguishable.
            return WakeAttempt(WakeStatus.FAILED, str(error) or type(error).__name__)
        # This is transport acceptance only. WakeCoordinator deliberately
        # retains the durable key and exposes SIGNALLED instead of completion.
        return WakeAttempt(WakeStatus.ACCEPTED)

    @staticmethod
    def _params(command: WakeCommand) -> dict[str, Any]:
        return {
            "content": command.prompt,
            # Claude Code renders meta as attributes of the <channel> tag and
            # rejects any non-string value: a nested origin object failed
            # validation ("meta.origin: Invalid input"), dropped the
            # notification and the stdio connection, so every wake carrying
            # an origin was lost (production 0.5.0, 2026-10-05: 106
            # rejections in one session since 07:43Z).
            "meta": {
                "delivery_id": command.delivery_id,
                **(
                    {"origin": _meta_json(command.origin)}
                    if command.origin is not None
                    else {}
                ),
            },
        }
