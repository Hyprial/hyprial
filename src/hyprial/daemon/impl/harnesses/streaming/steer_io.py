"""Shared native-steer I/O helper for the streaming turn clients.

Only Codex's ``turn/steer`` needs the accept/reject/unknown classification
here: the wire grammar and the honest verdict live in one place, next to the
other steer vocabulary, so the timeout path never mistakes a transport
failure for an acceptance.  Pi owns the equivalent classification inside its
own multiplexer (it has its own single stdout reader).
"""

from __future__ import annotations

from hyprial.daemon.impl.harnesses.codex.app_server import CodexAppServerRpcError
from hyprial.daemon.impl.harnesses.streaming.turn_io import (
    _SteerRejected,
    _SteerUnknown,
)

#: Explicit safe-reject markers: anything else is an unprovable outcome.
_CODEX_STEER_REJECT_MARKERS = (
    "no active turn",
    "not active",
    "no in-progress turn",
    "mismatch",
    "stale",
    "expectedturnid",
)


async def steer_codex_turn(
    client: object,
    prompt: str,
    *,
    thread_id: str,
    turn_id: str,
    timeout: float | None = None,
) -> str:
    """One codex ``turn/steer`` with the live-turn precondition.

    ``thread_id``/``turn_id`` come from the client's own active turn, read at
    call time (never a caller snapshot); the returned turn id must equal
    ``turn_id`` verbatim.  Explicit refusals raise _SteerRejected; unprovable
    outcomes raise _SteerUnknown.  Never turn/start or interrupt.
    """

    try:
        result = await client.request(  # type: ignore[attr-defined]
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": prompt}],
            },
            timeout=timeout,
        )
    except CodexAppServerRpcError as error:
        message = str(error).lower()
        if any(marker in message for marker in _CODEX_STEER_REJECT_MARKERS):
            raise _SteerRejected(str(error)) from error
        raise _SteerUnknown(
            f"codex turn/steer refusal not classifiable: {error}"
        ) from error
    except (ConnectionError, TimeoutError, OSError) as error:
        raise _SteerUnknown(
            f"codex turn/steer outcome unprovable after transport failure: {error}"
        ) from error
    steered = result.get("turnId") if isinstance(result, dict) else None
    if not isinstance(steered, str) or not steered:
        raise _SteerUnknown("codex turn/steer returned no turn id")
    if steered != turn_id:
        # Protocol error: never adopt a different turn id.
        raise _SteerUnknown(
            "codex turn/steer returned a different turn id; acceptance cannot"
            " be correlated with the expected turn"
        )
    return "steered"


__all__ = ["steer_codex_turn"]
