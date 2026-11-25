"""Parse teleport request keys back into their parts.

Building keys is ``KeySpace.agent_teleport``; this module is the inverse.  A
query selector (``?...``) is dropped because Zenoh hands handlers the selector.
Wildcard keys are rejected: a request always names one concrete operation.
"""

from __future__ import annotations

from dataclasses import dataclass

from hyprial.daemon.impl.teleport.errors import TELEPORT_OPS
from hyprial.daemon.impl.transport.keys import KeySpace

_PREFIX = KeySpace.prefix.split("/") + ["agent-teleport"]


@dataclass(frozen=True, slots=True)
class TeleportKey:
    target_node: str
    op: str
    operation_id: str


def strip_selector(key: str) -> str:
    return key.split("?", 1)[0]


def parse_teleport_key(key: str) -> TeleportKey:
    parts = strip_selector(key).split("/")
    if len(parts) != len(_PREFIX) + 3 or parts[: len(_PREFIX)] != _PREFIX:
        raise ValueError(f"not a teleport key: {key!r}")
    node, op, operation_id = parts[len(_PREFIX) :]
    if op not in TELEPORT_OPS:
        raise ValueError(f"unknown teleport op in key: {key!r}")
    for segment in (node, operation_id):
        if not segment or any(ch in segment for ch in "*$?#"):
            raise ValueError(f"invalid teleport key segment: {segment!r}")
    try:
        decoded = (
            KeySpace.decode_identity(node),
            KeySpace.decode_identity(operation_id),
        )
    except ValueError as exc:
        raise ValueError(f"undecodable teleport key segment in {key!r}") from exc
    return TeleportKey(target_node=decoded[0], op=op, operation_id=decoded[1])
