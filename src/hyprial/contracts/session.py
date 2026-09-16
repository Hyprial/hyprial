"""Provider-neutral IPC contract for interactive session consumption."""

from __future__ import annotations

from collections.abc import Mapping

# Background carrier polling observes daemon truth without claiming that a
# model turn received it. Every interactive implementation uses this exact
# flag only at its explicit user/model-facing fetch boundary.
SESSION_FETCH_PARAM = "fetched"
SESSION_CARRIER_SOURCES = frozenset(
    {"claude-channel", "pi-extension", "codex-app-server"}
)


def session_fetch_params() -> dict[str, bool]:
    """Return the wire marker for one real interactive inbox fetch."""

    return {SESSION_FETCH_PARAM: True}


def is_session_fetch(params: Mapping[str, object]) -> bool:
    """Distinguish explicit carrier consumption from observation-only polling."""

    return params.get(SESSION_FETCH_PARAM) is True


__all__ = [
    "SESSION_CARRIER_SOURCES",
    "SESSION_FETCH_PARAM",
    "is_session_fetch",
    "session_fetch_params",
]
