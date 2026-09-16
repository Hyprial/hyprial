"""Daemon-owned actor status projection.

The actor table is enumerated once from every identity-bearing runtime source:
managed connectors, interactive/MCP sessions, network presence, and the local
agent registry. Consumers such as ``top.snapshot`` and ``targets`` project
this same list instead of rebuilding actor membership independently.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from hyprial.uri import canonical_agent_uri, parse_agent_uri, short_actor_name

JsonObject = dict[str, Any]

PROCESS_RUNNING = "running"
PROCESS_STOPPED = "stopped"
PROCESS_NOT_APPLICABLE = "not_applicable"

_SOURCE_ORDER = (
    "connector",
    "interactive_session",
    "presence",
    "registry",
)


def build_actor_status_snapshot(
    *,
    owner: str,
    node_id: str,
    connectors: Iterable[Mapping[str, object]],
    interactive_sessions: Iterable[Mapping[str, object]],
    registered_actors: Iterable[str],
    presence_actors: Iterable[str],
    status_for_actor: Callable[[str, frozenset[str]], str],
) -> list[JsonObject]:
    """Build the canonical, deduplicated actor table for one daemon instant.

    ``status_for_actor`` is the daemon's existing liveness authority. This
    layer owns enumeration and the L1 process projection; it deliberately does
    not reinterpret presence, heartbeat, or supervisor verdicts.
    """

    rows: dict[str, JsonObject] = {}
    sources: dict[str, set[str]] = {}
    registered_actors = tuple(registered_actors)
    # Registry identity wins over the daemon owner: a hosted connector must
    # not produce a second, invented native-owner target with the same name.
    registered_by_name = {
        parsed[2]: actor for actor in registered_actors
        if (parsed := parse_agent_uri(actor)) is not None
    }

    def ensure(actor: str, source: str) -> JsonObject | None:
        if parse_agent_uri(actor) is None:
            return None
        row = rows.setdefault(
            actor,
            {
                "actor": actor,
                "name": short_actor_name(actor),
                "runtime": None,
                "running": None,
                "pid": None,
                "processState": PROCESS_NOT_APPLICABLE,
            },
        )
        sources.setdefault(actor, set()).add(source)
        return row

    for connector in connectors:
        name = connector.get("name")
        if not isinstance(name, str) or not name:
            continue
        actor = registered_by_name.get(name) or canonical_agent_uri(owner, node_id, name)
        row = ensure(actor, "connector")
        assert row is not None
        running = connector.get("running") is True
        pid = connector.get("pid")
        raw_runtime = connector.get("runtime")
        row.update(
            {
                "runtime": raw_runtime if isinstance(raw_runtime, str) else None,
                "running": running,
                "pid": pid if isinstance(pid, int) else None,
                "processState": PROCESS_RUNNING if running else PROCESS_STOPPED,
            }
        )
        error = connector.get("error")
        if isinstance(error, str) and error:
            row["error"] = error

    for session in interactive_sessions:
        actor = session.get("actor")
        if not isinstance(actor, str) or not actor:
            continue
        if parse_agent_uri(actor) is None and ":" not in actor:
            actor = canonical_agent_uri(owner, node_id, actor)
        row = ensure(actor, "interactive_session")
        if row is None:
            continue
        if row["runtime"] is None:
            runtime = session.get("runtime") or session.get("source")
            row["runtime"] = runtime if isinstance(runtime, str) else None

    canonical_presence = frozenset(
        actor for actor in presence_actors if parse_agent_uri(actor) is not None
    )
    for actor in canonical_presence:
        ensure(actor, "presence")

    for actor in registered_actors:
        ensure(actor, "registry")

    result: list[JsonObject] = []
    for actor in sorted(rows):
        row = rows[actor]
        status = status_for_actor(actor, canonical_presence)
        row["status"] = status
        # An actor without a supervisor-owned process is online/offline, never
        # a stopped process inferred from missing evidence.
        row["state"] = (
            PROCESS_STOPPED
            if row["processState"] == PROCESS_STOPPED
            else status
        )
        row["sources"] = [
            source for source in _SOURCE_ORDER if source in sources[actor]
        ]
        result.append(row)
    return result


__all__ = [
    "PROCESS_NOT_APPLICABLE",
    "PROCESS_RUNNING",
    "PROCESS_STOPPED",
    "build_actor_status_snapshot",
]
