"""App chat authorization matrix (device x session x operation), consumption side.

Owned here by the consumption side (org-improver's answer assigns the matrix
to the consumer).  Device identity comes from the N-A2 verified envelope:
``deviceId`` resolves to a device row; the request's body ``author`` is
optional and must equal that row's ``owner`` when present (a missing author
resolves to the owner).  The matrix then answers the second question: which of
the owner's sessions may that author touch, and with which operation.

MVP matrix (deny by default -- every allow is an explicit row):

- ``capabilities.get`` and ``sessions.list``: allowed for any envelope-verified
  device; both describe the facade itself, not a conversation (the facade only
  ever lists the bindings the composition root configured, so this reveals
  nothing the device's owner could not already see).
- ``sessions.history`` / ``sessions.events`` / ``sessions.send``: allowed only
  when the request names a configured session binding AND the verified author
  is one of that binding's targets.  A session therefore belongs to the actors
  it binds, and a device row owned by anyone else cannot reach it even with a
  valid envelope.
- every other operation (and any sessionless request for a session-scoped
  operation): refused with ``auth-not-authorized``.

Relationship to the composition root: the binding table stays exactly what the
composition root configured (sender, targets, conversation id); this matrix
never invents a session or widens a binding -- it only refuses what the
bindings do not claim.  ``reply`` deliberately has no row: replies belong to
the agent's harness (``message.reply``), never to the facade's five
operations.  Multi-target bindings currently require the author to be *a*
target; tightening or loosening that is a product ruling -- see the questions
list in the slice record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hyprial.shell.impl.gui.adapter.envelope import VerifiedRequest

#: The two operations that describe the facade itself (no session domain).
SESSIONLESS_OPERATIONS = frozenset({"capabilities.get", "sessions.list"})

#: The three operations scoped to one session binding.
SESSION_OPERATIONS = frozenset(
    {"sessions.history", "sessions.events", "sessions.send"}
)


@dataclass(frozen=True, slots=True)
class AuthorizationMatrix:
    """The MVP matrix; policy lives in the docstring, not in configuration."""

    sessionless_operations: frozenset[str] = SESSIONLESS_OPERATIONS
    session_operations: frozenset[str] = SESSION_OPERATIONS


def matrix_denial(
    matrix: AuthorizationMatrix,
    verified: VerifiedRequest,
    binding: Any | None,
) -> dict[str, object] | None:
    """``None`` when allowed; a detail dict describing the denial otherwise.

    ``binding`` is the session binding from the composition root
    (``SessionBinding``), or ``None`` for an unknown/absent session.
    """

    operation = verified.operation
    if operation in matrix.sessionless_operations:
        if verified.session_id is not None:
            return {
                "operation": operation,
                "reason": "sessionless operation must not carry a session id",
            }
        return None
    if operation in matrix.session_operations:
        if verified.session_id is None:
            return {
                "operation": operation,
                "reason": "the operation needs a session the request never named",
            }
        if binding is None:
            return {
                "operation": operation,
                "sessionId": verified.session_id,
                "reason": "the session is not bound by this composition root",
            }
        targets = getattr(binding, "targets", ())
        if verified.author not in targets:
            return {
                "operation": operation,
                "sessionId": verified.session_id,
                "author": verified.author,
                "reason": "the author is not one of the session's bound targets",
            }
        return None
    return {"operation": operation, "reason": "the operation has no matrix row"}
