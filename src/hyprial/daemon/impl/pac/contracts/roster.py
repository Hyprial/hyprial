"""Canonical workflow roster projection and digest."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from hyprial.kernel import canonical_agent_uri, parse_agent_uri


def canonical_json(value: Any) -> str:
    """Return the byte-stable JSON representation used by briefs and storage."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _value(node: Any, name: str) -> Any:
    return node.get(name) if isinstance(node, Mapping) else getattr(node, name)


def build_roster(
    graph_id: str,
    nodes: Iterable[Any],
    *,
    owned_bindings: Mapping[str, tuple[str, str]],
    local_owner: str | None,
    local_machine: str | None,
) -> tuple[dict[str, Any], str, str]:
    """Build one deterministic node-to-actor projection.

    ``owned_bindings`` is authoritative.  Names and URI prefixes are never
    used to infer ownership, which keeps misleading borrowed actor names out
    of the receipt set.
    """

    projected: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(_value(node, "id"))
        role = str(_value(node, "role"))
        binding = owned_bindings.get(node_id)
        if binding is not None:
            actor_node, actor_uri = binding
            projected.append(
                {
                    "nodeId": node_id,
                    "role": role,
                    "owner": actor_uri,
                    "actorNode": actor_node,
                    "actorUri": actor_uri,
                    "ownership": "workflow",
                    "location": "local",
                }
            )
            continue

        if _value(node, "kind") == "expansion":
            if local_owner is None or local_machine is None:
                raise ValueError("expansion roster requires the local PAC identity")
            projected.append(
                {
                    "nodeId": node_id,
                    "role": role,
                    "owner": canonical_agent_uri(
                        local_owner, local_machine, "pac-expansion"
                    ),
                    "ownership": "system",
                }
            )
            continue

        owner = str(_value(node, "owner"))
        item: dict[str, Any] = {
            "nodeId": node_id,
            "role": role,
            "owner": owner,
            "ownership": "borrowed",
        }
        parsed = parse_agent_uri(owner)
        if parsed is not None and local_owner is not None and local_machine is not None:
            item["location"] = (
                "local"
                if parsed[:2] == (local_owner, local_machine)
                else "remote"
            )
        projected.append(item)

    unsigned = {"graphId": graph_id, "nodes": projected}
    digest = sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    roster = {"graphId": graph_id, "digest": digest, "nodes": projected}
    return roster, digest, canonical_json(roster)


def validate_roster_document(
    graph_id: str, roster_json: str, roster_digest: str
) -> dict[str, Any]:
    """Validate the stored projection without accepting alternate encodings."""

    try:
        roster = json.loads(roster_json)
    except (TypeError, ValueError) as error:
        raise ValueError("workflow roster is not valid JSON") from error
    if not isinstance(roster, dict) or canonical_json(roster) != roster_json:
        raise ValueError("workflow roster is not canonical JSON")
    if roster.get("graphId") != graph_id or roster.get("digest") != roster_digest:
        raise ValueError("workflow roster identity or digest field mismatch")
    unsigned = {"graphId": graph_id, "nodes": roster.get("nodes")}
    actual = sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    if actual != roster_digest:
        raise ValueError("workflow roster digest mismatch")
    return roster
