"""Declared errors of local agent address admission (UUID addressing §2.3).

``application/actors/address_admission.py`` raises these at the send
boundary and wherever a caller identity is resolved
(``_authenticated_message_caller``, ``_resolve_on_behalf_actor``,
``_resolve_send_sender``, ``_validate_input_alias``).  Every MethodSpec whose
handler reaches one of those declares the same entries, so the registry
never rewrites the fail-loud codes (or their migration hints) to
DAEMON_ERROR.  SESSION_SUPERSEDED from the session fence is declared by each
fenced spec without data and is not repeated here.  In the
AGENT_ADDRESS_RETIRED data, ``address``/``agentId`` name the id address of the
live agent the name belongs to.  ``retired`` stays declared (the published
contract) but is no longer produced since the frozen-name ledger was removed.
"""

from __future__ import annotations

from hyprial.kernel import MethodError, ipc_errors

RETIRED_ADDRESS_DATA = {
    "type": "object",
    "properties": {
        "target": {"type": "string"},
        "address": {"type": "string"},
        "agentId": {"type": "string"},
        "retired": {"type": "boolean"},
    },
    "required": ["target", "agentId"],
    "additionalProperties": False,
}

TARGET_UNRESOLVED_DATA = {
    "type": "object",
    "properties": {"target": {"type": "string"}},
    "required": ["target"],
    "additionalProperties": False,
}

AGENT_ADDRESS_RETIRED_ERROR = MethodError(
    ipc_errors.AGENT_ADDRESS_RETIRED,
    RETIRED_ADDRESS_DATA,
    description=(
        "With id-form minting on, a local name address of a registered or "
        "frozen agent is refused; data carries the id address to use."
    ),
)

TARGET_UNRESOLVED_ERROR = MethodError(
    ipc_errors.TARGET_UNRESOLVED,
    TARGET_UNRESOLVED_DATA,
    description=(
        "With id-form minting on, a local id address with no live agent, or "
        "a local name nothing on this node claims, is refused (fatal)."
    ),
)

__all__ = [
    "AGENT_ADDRESS_RETIRED_ERROR",
    "RETIRED_ADDRESS_DATA",
    "TARGET_UNRESOLVED_DATA",
    "TARGET_UNRESOLVED_ERROR",
]
