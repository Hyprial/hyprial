"""Shared samples for the slice 4a protocol message tests.

Decisions pinned by these tests (docs/design/agent-teleport-protocol.md §2):

- The public surface is ``hyprial.daemon.impl.teleport.protocol``; it is
  imported lazily through ``protocol()`` so a missing package fails each test
  on its own instead of failing collection.
- Every message is a frozen dataclass with snake_case attributes and keyword
  construction; sequence attributes are tuples, so decoded values compare
  equal to the constructed ones.
- Per message there is an ``encode_<op>_request`` / ``decode_<op>_request``
  and ``encode_<op>_reply`` / ``decode_<op>_reply`` pair built on the slice 2
  frame codec.  Encoders always write every field (defaults included); the
  only exception is ``AbortRequest.reason``, omitted when ``None``.
- ``PrepareReply.recipient_public_key`` is the raw 32-byte X25519 key; on the
  wire it is standard padded base64.
- ``manifestDigest`` and ``streamSha256`` are both 64 lowercase hex digits.
- ``strictTimeoutSeconds`` is a positive JSON number (not a boolean) held as
  ``float``; when absent it defaults to 60.  ``args`` defaults to ``()`` and
  ``dryRun`` to ``False``.
- Identifier strings (nodes, owner, agent, URIs, harness, routine names) must
  be non-empty; ``sessionRef`` is a non-empty string or ``null``.
- ``decode_<op>_reply`` on an ``ok:false`` frame raises ``TeleportError``
  carrying the remote ``error.code`` and ``error.message``.
- Anything else malformed (missing field, wrong type, unknown or mismatched
  ``op``, non-empty body) raises ``TELEPORT_BAD_FRAME``.
"""

from __future__ import annotations

import base64
import importlib
from types import ModuleType

PROTOCOL = "hyprial.daemon.impl.teleport.protocol"
OPERATION_ID = "op-4a"
PUBLIC_KEY = bytes(range(32))
PUBLIC_KEY_B64 = base64.b64encode(PUBLIC_KEY).decode("ascii")
MANIFEST_DIGEST = "a1" * 32
STREAM_SHA256 = "0f" * 32
SOURCE_URI = "agent://owner-1/node-a/worker"
TARGET_URI = "agent://owner-1/node-b/worker"
OPS = ("prepare", "receive", "commit", "abort")


def protocol() -> ModuleType:
    """Import the slice 4a protocol package on demand."""

    return importlib.import_module(PROTOCOL)


def _common(op: str) -> dict:
    return {"v": 1, "op": op, "operationId": OPERATION_ID}


def _reply(op: str) -> dict:
    return {**_common(op), "ok": True}


def base_headers() -> dict[str, dict]:
    """Valid wire headers keyed by ``<op>_request`` / ``<op>_reply``."""

    return {
        "prepare_request": {
            **_common("prepare"),
            "sourceNode": "node-a",
            "targetNode": "node-b",
            "owner": "owner-1",
            "agent": "worker",
            "sourceAgentUri": SOURCE_URI,
            "harness": "claude",
            "routines": ["nightly", "weekly"],
            "dryRun": False,
        },
        "prepare_reply": {
            **_reply("prepare"),
            "targetNode": "node-b",
            "recipientPublicKey": PUBLIC_KEY_B64,
            "dryRun": False,
        },
        "receive_request": {
            **_common("receive"),
            "manifestDigest": MANIFEST_DIGEST,
            "streamSha256": STREAM_SHA256,
            "harness": "codex",
            "args": ["--model", "m-1"],
            "sessionRef": "sess-1",
            "strictTimeoutSeconds": 60,
            "routines": [{"name": "nightly", "yaml": "name: nightly\n", "enabled": True}],
        },
        "receive_reply": {
            **_reply("receive"),
            "agentUri": TARGET_URI,
            "sessionRef": "sess-1",
            "routines": [{"name": "nightly", "restoreEnabled": True}],
        },
        "commit_request": _common("commit"),
        "commit_reply": {
            **_reply("commit"),
            "routines": [{"name": "nightly", "enabled": True, "resumed": False}],
        },
        "abort_request": {**_common("abort"), "reason": "source not quiesced"},
        "abort_reply": _reply("abort"),
    }


def sample_messages() -> dict[str, object]:
    """One fully populated message per ``<op>_request`` / ``<op>_reply``."""

    p = protocol()
    return {
        "prepare_request": p.PrepareRequest(
            operation_id=OPERATION_ID,
            source_node="node-a",
            target_node="node-b",
            owner="owner-1",
            agent="worker",
            source_agent_uri=SOURCE_URI,
            harness="claude",
            routines=("nightly", "weekly"),
            dry_run=True,
        ),
        "prepare_reply": p.PrepareReply(
            operation_id=OPERATION_ID,
            target_node="node-b",
            recipient_public_key=PUBLIC_KEY,
            dry_run=True,
        ),
        "receive_request": p.ReceiveRequest(
            operation_id=OPERATION_ID,
            manifest_digest=MANIFEST_DIGEST,
            stream_sha256=STREAM_SHA256,
            harness="codex",
            args=("--model", "m-1"),
            session_ref="sess-1",
            strict_timeout_seconds=12.5,
            routines=(
                p.RoutinePayload(name="nightly", yaml="name: nightly\n", enabled=True),
                p.RoutinePayload(name="weekly", yaml="name: weekly\n", enabled=False),
            ),
        ),
        "receive_reply": p.ReceiveReply(
            operation_id=OPERATION_ID,
            agent_uri=TARGET_URI,
            session_ref="sess-1",
            routines=(
                p.RoutineRestore(name="nightly", restore_enabled=True),
                p.RoutineRestore(name="weekly", restore_enabled=False),
            ),
        ),
        "commit_request": p.CommitRequest(operation_id=OPERATION_ID),
        "commit_reply": p.CommitReply(
            operation_id=OPERATION_ID,
            routines=(
                p.RoutineOutcome(name="nightly", enabled=True, resumed=True),
                p.RoutineOutcome(name="weekly", enabled=False, resumed=False),
            ),
        ),
        "abort_request": p.AbortRequest(
            operation_id=OPERATION_ID, reason="chunk send failed"
        ),
        "abort_reply": p.AbortReply(operation_id=OPERATION_ID),
    }


def codec(kind: str) -> tuple:
    """Return ``(encode, decode)`` for ``kind`` such as ``prepare_request``."""

    p = protocol()
    return getattr(p, f"encode_{kind}"), getattr(p, f"decode_{kind}")


KINDS = tuple(f"{op}_{side}" for op in OPS for side in ("request", "reply"))
