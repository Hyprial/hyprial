# Decision record (file header; no inline comments by repository rule):
# - One method serves the dry run and the move (``dryRun``), because a second
#   method would let the two diverge.  The result shows what was or would be
#   moved; ``to.uri`` is null and ``bytes`` 0 for a dry run, which has no
#   ``commit`` object.
# - ``retry="never"`` and ``confirmation="none"``: a teleport stops and
#   removes the agent, so it is never replayed.  ``confirmation="preview"``
#   needs a second execute method with an operationId-only call, which cannot
#   describe a run that has both modes; the ``--yes`` prompt is the CLI's.
# - ``http_exposed=False``: the GUI backend is still an HTTP 501 placeholder
#   (AGENTS.md), so HTTP exposure, examples and a generated GUI client are
#   added with the GUI wiring.  The audience is the local operator only.
"""The ``teleport.run`` method declaration."""

from __future__ import annotations

from hyprial.daemon.impl.request_catalog.teleport.errors import teleport_errors
from hyprial.kernel import IdempotencySpec, MethodSpec

TELEPORT_RUN = "teleport.run"

_STRING = {"type": "string", "minLength": 1}

_PARAMS = {
    "type": "object",
    "properties": {
        "agent": {**_STRING, "description": "The local agent to move."},
        "to": {**_STRING, "description": "The target node id (HYPRIAL_NODE_ID)."},
        "dryRun": {
            "type": "boolean",
            "description": "Only check and report the plan; change nothing.",
        },
    },
    "required": ["agent", "to"],
    "additionalProperties": False,
}

_END = {
    "type": "object",
    "properties": {"node": _STRING, "uri": {"type": ["string", "null"]}},
    "required": ["node", "uri"],
    "additionalProperties": False,
}

_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "operationId": _STRING,
        "dryRun": {"type": "boolean"},
        "agent": _STRING,
        "from": _END,
        "to": _END,
        "bytes": {"type": "integer", "minimum": 0},
        "routines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": _STRING, "enabled": {"type": "boolean"}},
                "required": ["name", "enabled"],
                "additionalProperties": False,
            },
        },
        "commit": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "pausedRoutines": {"type": "array", "items": _STRING},
            },
            "required": ["ok", "pausedRoutines"],
            "additionalProperties": False,
        },
    },
    "required": ["ok", "operationId", "dryRun", "agent", "from", "to", "bytes", "routines"],
    "additionalProperties": False,
}

TELEPORT_RUN_SPEC = MethodSpec(
    name=TELEPORT_RUN,
    owner="daemon.teleport",
    params_schema=_PARAMS,
    result_schema=_RESULT,
    errors=teleport_errors(),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="agent-teleport"),
)
