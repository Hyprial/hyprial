"""Provider-neutral IPC contract for interactive session consumption."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5

# Background carrier polling observes daemon truth without claiming that a
# model turn received it. Every interactive implementation uses this exact
# flag only at its explicit user/model-facing fetch boundary.
SESSION_FETCH_PARAM = "fetched"
SESSION_CARRIER_SOURCES = frozenset(
    {"claude-channel", "pi-extension", "codex-app-server", "dsh-cordis-demo"}
)


def session_fetch_params() -> dict[str, bool]:
    """Return the wire marker for one real interactive inbox fetch."""

    return {SESSION_FETCH_PARAM: True}


def is_session_fetch(params: Mapping[str, object]) -> bool:
    """Distinguish explicit carrier consumption from observation-only polling."""

    return params.get(SESSION_FETCH_PARAM) is True


def reply_message_id(message_id: str) -> str:
    """The deterministic id message.reply mints for a reply to ``message_id``.

    Every reply to one inbound message shares this id no matter which path
    authored it (the model's in-turn harness_reply or a background carrier's
    settlement), which is what lets a carrier that finds the pending row gone
    ask message.status whether a reply was in fact delivered.
    """

    return str(uuid5(NAMESPACE_URL, f"hyprial:reply:{message_id}"))


__all__ = [
    "SESSION_CARRIER_SOURCES",
    "SESSION_FETCH_PARAM",
    "is_session_fetch",
    "reply_message_id",
    "session_fetch_params",
]
