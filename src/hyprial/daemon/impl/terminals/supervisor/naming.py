"""Deterministic private-core member terminal identity."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any


_LABEL_AGENT = "hyprial.agent"
_LABEL_MEMBER = "hyprial.member"
_LABEL_ROLE = "hyprial.role"
_LABEL_DIGEST = "hyprial.spec_digest"


def member_terminal_name(agent_id: str, member: str, role: str) -> str:
    """Return a stable hc-safe name for one member role."""
    payload = "\x00".join((str(agent_id), str(member), str(role))).encode("utf-8")
    return f"hm-{hashlib.sha256(payload).hexdigest()[:16]}"


def member_terminal_labels(
    agent_id: str, member: str, role: str, spec_digest: str
) -> dict[str, str]:
    """Return labels that retain launch identity without credentials."""
    return {
        _LABEL_AGENT: str(agent_id),
        _LABEL_MEMBER: str(member),
        _LABEL_ROLE: str(role),
        _LABEL_DIGEST: str(spec_digest),
    }


def parse_member_terminal_labels(labels: Mapping[str, str]) -> dict[str, str] | None:
    """Decode a complete member identity label set."""
    if not isinstance(labels, Mapping):
        return None
    values = {
        "agent_id": labels.get(_LABEL_AGENT),
        "member": labels.get(_LABEL_MEMBER),
        "role": labels.get(_LABEL_ROLE),
        "spec_digest": labels.get(_LABEL_DIGEST),
    }
    if any(not isinstance(value, str) for value in values.values()):
        return None
    return values


def reconcile_orphan_terminals(
    terminals: Iterable[Any], active_launch_rows: Iterable[Any]
) -> tuple[str, ...]:
    """Return live labeled terminal addresses absent from active launch rows."""
    active = {_launch_identity(row) for row in active_launch_rows}
    orphaned: list[str] = []
    for terminal in terminals:
        labels = _value(terminal, "labels")
        identity = parse_member_terminal_labels(labels) if isinstance(labels, Mapping) else None
        address = _value(terminal, "address")
        state = _value(terminal, "state")
        if identity is not None and (identity["agent_id"], identity["member"]) in active:
            continue
        if identity is not None and isinstance(address, str) and state == "live":
            orphaned.append(address)
    return tuple(orphaned)


def _launch_identity(value: Any) -> tuple[str, str] | None:
    agent_id = _value(value, "agent_id")
    member = _value(value, "member")
    if isinstance(agent_id, str) and isinstance(member, str):
        return agent_id, member
    if isinstance(value, tuple) and len(value) == 2 and all(isinstance(item, str) for item in value):
        return value
    return None


def _value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


__all__ = [
    "member_terminal_labels",
    "member_terminal_name",
    "parse_member_terminal_labels",
    "reconcile_orphan_terminals",
]
