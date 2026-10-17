"""Read-only preflight for an agent-home (P2) cross-host transfer.

AT07 increment I1.  This module is the home of the P2 orchestration: it *describes*
the "one authorized context" that a target must be able to rebind -- identity,
config, session, and the native root the target derives -- from the payload that
``transfer.plan`` already returns.

It performs no writes: no identity row, no file, no worker, no lifecycle call.
Nothing dispatches through this module yet; the P0 orchestrator keeps refusing
agent-home workers with its original code until a later increment proves the
chain end to end and the review accepts lifting that refusal for the exact
harness/mode the round exercises.

Input contract (all read from the existing plan payload):
  ``{"ok", "actor", "nodeId", "owner", "spec", "agent", ...}``
  - ``agent`` is the registry row without ``entityToken``;
  - ``agent["config"]`` is ``AgentConfig.to_json()``
    (``{"sources": [{"path", "required"}], "discovery"}``), and its presence is
    what marks a worker as agent-home today.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

IDENTITY = "identity"
CONFIG = "config"
SESSION = "session"
NATIVE_ROOT = "nativeRoot"

PRESENT = "present"
MISSING = "missing"
INVALID = "invalid"
TARGET_DERIVES = "target-derives"

_REQUIRED = (IDENTITY, CONFIG, SESSION)


@dataclass(frozen=True, slots=True)
class P2Item:
    """One fact a target needs, with where it came from and whether it is usable."""

    name: str
    status: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class P2Preflight:
    """The verdict a caller may show an operator; ``reason`` is empty when ready."""

    actor: str
    owner: str
    node_id: str
    items: tuple[P2Item, ...]
    reason: str

    @property
    def ready(self) -> bool:
        return not self.reason

    def item(self, name: str) -> P2Item:
        for item in self.items:
            if item.name == name:
                return item
        raise KeyError(name)

    def to_json(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "actor": self.actor,
            "owner": self.owner,
            "nodeId": self.node_id,
            "reason": self.reason or None,
            "items": [item.to_json() for item in self.items],
        }


def _nonempty(value: Any) -> str:
    return value if isinstance(value, str) and value else ""


def _source_entry(config: Any) -> Mapping[str, Any] | None:
    if not isinstance(config, Mapping):
        return None
    sources = config.get("sources")
    if isinstance(sources, (list, tuple)) and sources and isinstance(sources[0], Mapping):
        return sources[0]
    return None


def preflight_plan(plan: Mapping[str, Any]) -> P2Preflight:
    """Describe the P2 context of one plan payload.  Pure: the input is not touched."""

    if not isinstance(plan, Mapping):
        raise TypeError("plan must be a mapping")
    actor = _nonempty(plan.get("actor"))
    owner = _nonempty(plan.get("owner"))
    node_id = _nonempty(plan.get("nodeId"))
    spec = plan.get("spec") if isinstance(plan.get("spec"), Mapping) else {}
    agent = plan.get("agent") if isinstance(plan.get("agent"), Mapping) else None

    items: list[P2Item] = []

    if agent is None:
        items.append(P2Item(IDENTITY, MISSING, "plan carries no agent row"))
    elif _nonempty(agent.get("actor")) != actor or _nonempty(agent.get("owner")) != owner:
        items.append(
            P2Item(
                IDENTITY,
                INVALID,
                "agent row disagrees with plan actor/owner",
            )
        )
    elif not actor or not owner:
        items.append(P2Item(IDENTITY, INVALID, "plan actor/owner is empty"))
    else:
        items.append(P2Item(IDENTITY, PRESENT, f"agent row for {actor}"))

    config = None if agent is None else agent.get("config")
    if config is None:
        items.append(
            P2Item(
                CONFIG,
                MISSING,
                "agent has no config: this is a harness transfer, not agent-home",
            )
        )
    else:
        entry = _source_entry(config)
        path = _nonempty(entry.get("path")) if entry is not None else ""
        if not path.startswith("/"):
            items.append(
                P2Item(
                    CONFIG,
                    INVALID,
                    "config.sources[0].path must be an absolute path",
                )
            )
        else:
            discovery = _nonempty(config.get("discovery")) if isinstance(config, Mapping) else ""
            items.append(P2Item(CONFIG, PRESENT, f"{path} ({discovery or 'discovery-unspecified'})"))

    session_ref = _nonempty(spec.get("sessionRef")) if isinstance(spec, Mapping) else ""
    last_session = _nonempty(agent.get("lastSessionId")) if agent is not None else ""
    if session_ref:
        items.append(P2Item(SESSION, PRESENT, f"spec.sessionRef={session_ref}"))
    elif last_session:
        items.append(
            P2Item(SESSION, MISSING, f"no spec.sessionRef; agent row only has lastSessionId={last_session}")
        )
    else:
        items.append(P2Item(SESSION, MISSING, "no session ref on the spec or the agent row"))

    items.append(
        P2Item(
            NATIVE_ROOT,
            TARGET_DERIVES,
            "the target derives it from the config manifest (build_native_projection)",
        )
    )

    failing = next(
        (item for item in items if item.name in _REQUIRED and item.status != PRESENT),
        None,
    )
    reason = "" if failing is None else f"{failing.name}:{failing.status}"
    return P2Preflight(actor=actor, owner=owner, node_id=node_id, items=tuple(items), reason=reason)
