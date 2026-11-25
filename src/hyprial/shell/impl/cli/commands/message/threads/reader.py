"""Who reads conversation list and message history.

The reader identity follows ``message send``: a verified agent session reads
as itself with its session fence (``sessionRef``); outside a session the
local operator ``user:<owner>`` reads, and ``--on-behalf-of`` names the agent
it reads for.  ``--from`` overrides the default the same way it does for
``send``.  The daemon's caller rule judges the result; the CLI never decides
who may read what.
"""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import (
    JsonObject,
    _local_operator_identity,
    _message_claim_params,
)

PAGE_MAX = 200
FROM_HELP = "Reading actor; defaults to the verified agent session, else the local operator."
ON_BEHALF_HELP = "Agent the local operator reads for."


def reader_params(source: str | None, on_behalf_of: str | None) -> JsonObject:
    """The caller fields of one read: ``from`` plus ``sessionRef``/``onBehalfOf``."""

    services = get_services()
    binding = services.runtime.worker_session_binding()
    if source is None:
        source = binding.actor if binding is not None else _local_operator_identity()
    return _message_claim_params(source, on_behalf_of=on_behalf_of, binding=binding)


def text(value: object) -> str:
    """A cell of a human table: the string, or ``-`` for nothing."""

    return value if isinstance(value, str) and value else "-"


__all__ = [
    "FROM_HELP",
    "ON_BEHALF_HELP",
    "PAGE_MAX",
    "reader_params",
    "text",
]
