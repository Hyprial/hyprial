"""The one strict parser for the PAC ownership marker in desired_state.

PAC's coordinator builds an actor activation ``identity_marker`` of the shape
``pac:<graph_id>:<node_id>:<incarnation>:<effect>`` and
``DaemonActorRuntime._launch_spec`` persists it verbatim as the launched
harness's ``nickname`` (``coordinator.core._prepare_up`` / ``types.start``).
That desired-state row is PAC's own ownership record for a temporary workflow
worker: ``DaemonActorRuntime.observe`` reports it back as ``identity_marker``,
and the link service reads the same fact to keep such a worker out of the
proactive empty-file roster (E2E-021).

Exactly one parser, strict by construction.  Only that five-field shape is
ownership evidence; a resident agent whose operator-set nickname merely begins
with ``pac:`` is NOT a worker and keeps publishing its B13 file.  The
activation marker always carries the incarnation field -- the four-field
``pac:<graph>:<node>:<effect>`` is not a PAC marker.
"""

from __future__ import annotations

__all__ = ["pac_worker_marker"]

_PAC_MARKER_HEAD = "pac"
_PAC_MARKER_FIELDS = 5


def pac_worker_marker(nickname: str | None) -> str | None:
    """Return ``nickname`` when it is a strict PAC activation marker, else None.

    Grammar: ``pac:<graph_id>:<node_id>:<incarnation>:<effect>`` with four
    non-empty fields and a positive decimal incarnation (``1`` on the first
    activation, incremented on every restart).  Anything else is not PAC
    ownership evidence.
    """

    if not isinstance(nickname, str):
        return None
    parts = nickname.split(":")
    if len(parts) != _PAC_MARKER_FIELDS or parts[0] != _PAC_MARKER_HEAD:
        return None
    graph_id, node_id, incarnation, effect = parts[1:]
    if not graph_id or not node_id or not effect:
        return None
    if not incarnation.isascii() or not incarnation.isdigit():
        return None
    if int(incarnation) < 1:
        return None
    return nickname
