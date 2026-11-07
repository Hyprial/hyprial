"""Independent sample consumer of the public workflow events JSON contract.

Standard library only. This module never imports hyprial or opens a database.
Consumers persist a snapshot plus cursor, ignore duplicates, and bootstrap again
on structure invalidation, journal replacement, or unsupported event versions.
"""

from __future__ import annotations

import copy
import json
import subprocess
from typing import Any


class ResyncRequired(Exception):
    pass


class Client:
    def __init__(self, executable: str, env: dict[str, str]):
        self.executable, self.env = executable, env

    def invoke(self, *args: str) -> list[dict[str, Any]]:
        result = subprocess.run([self.executable, "workflow", *args, "--json"],
                                env=self.env, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ResyncRequired(result.stderr or result.stdout)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def snapshot(self, graph: str) -> dict[str, Any]:
        return self.invoke("events", graph, "--snapshot")[0]

    def events(self, graph: str, after: int, journal_id: str) -> list[dict[str, Any]]:
        return self.invoke("events", graph, "--after", str(after), "--journal-id", journal_id)


class Consumer:
    def __init__(self, snapshot: dict[str, Any]):
        if snapshot["schemaVersion"] != 1 or snapshot["type"] != "snapshot":
            raise ResyncRequired("unsupported snapshot")
        self.state = copy.deepcopy(snapshot)
        self._refresh_blocked_on()

    def _refresh_blocked_on(self) -> None:
        state = self.state
        actors = {item["actorName"] for item in state["actors"]}
        flags = state["flags"]
        edges = state["structure"]["edges"]
        blocked: dict[str, str] = {}
        for node in state["structure"]["nodes"]:
            node_id = node["nodeId"]
            predecessors = [edge["from"] for edge in edges
                            if edge["kind"] == "forward" and edge["to"] == node_id]
            ready = all(flags[item]["flag"] for item in predecessors)
            # Owner kind comes from the URI itself (identityFormat
            # principal-uri-v1): a user: owner is human, an agent: owner an
            # agent.  Pre-URI short names (mixed-legacy snapshots) keep the
            # actors-set heuristic for exactly those rows.
            if node["owner"].startswith("user:"):
                kind = "human"
            elif node["owner"].startswith("agent:"):
                kind = "agent"
            else:
                kind = "agent" if node["owner"] in actors else "human"
            blocked[node_id] = (
                "none"
                if state["closed"] is not None or flags[node_id]["flag"] or not ready
                else kind
            )
        state["blockedOn"] = blocked
        for assignment in state["assignments"]:
            assignment["blockedOn"] = blocked[assignment["nodeId"]]

    def _actor(self, node_id: str) -> dict[str, Any]:
        actor = next((item for item in self.state["actors"] if item["nodeId"] == node_id), None)
        if actor is None:
            raise ResyncRequired(f"lifecycle outcome for unknown actor node: {node_id}")
        return actor

    def apply(self, event: dict[str, Any]) -> None:
        state = self.state
        if event["journalId"] != state["journalId"] or event["graphId"] != state["graphId"]:
            raise ResyncRequired("wrong journal/graph")
        if event["seq"] <= state["cursor"]:
            return  # duplicate delivery is harmless; gaps are permitted
        if event["schemaVersion"] != 1:
            raise ResyncRequired("unsupported envelope")
        if state.get("assignmentFormat") == "workflow-request-v2":
            raise ResyncRequired("managed workflow request projection changed; bootstrap a new snapshot")
        kind, data = event["type"], event["data"]
        if kind in {"structure_changed", "migration_baseline"}:
            raise ResyncRequired("structure invalidated; bootstrap a new snapshot")
        if kind == "flag_set":
            flag = state["flags"][data["nodeId"]]
            flag.update(flag=True, setBy=data["actor"], setAt=event["at"], reasonRef=data["reasonRef"])
            flag["completedCount"] += 1
            state["assignments"] = [a for a in state["assignments"]
                                    if a["nodeId"] != data["nodeId"] or a["round"] > flag["completedCount"]]
        elif kind == "flag_reset":
            state["flags"][data["nodeId"]].update(flag=False, setBy=None, setAt=None, reasonRef=None)
        elif kind == "graph_activated":
            state["active"] = copy.deepcopy(data)
        elif kind == "graph_closed":
            state["closed"] = copy.deepcopy(data)
            state["assignments"] = []
        elif kind == "notification_planned":
            record = copy.deepcopy(data)
            # Missing round in early pre-contract writers is normalized by the
            # public contract. Supported current writers always include it.
            record.setdefault("round", None)
            record.pop("activationId", None)
            state["notifications"].append(record)
            if record["kind"] == "turn" and state["closed"] is None:
                identity = record["plan"]["activationId"]
                assignment = next((a for a in state["assignments"] if a["activationId"] == identity), None)
                if assignment is None:
                    assignment = {"activationId": identity, "nodeId": record["node"],
                                  "owner": record["recipient"], "round": record["round"],
                                  "requests": [], "blockedOn": "none"}
                    state["assignments"].append(assignment)
                assignment["requests"] = [r for r in assignment["requests"] if r["edge"] != record["edge"]]
                assignment["requests"].append(record)
            elif record["kind"] == "withdraw":
                for assignment in state["assignments"]:
                    assignment["requests"] = [r for r in assignment["requests"] if r["edge"] != record["edge"]]
                state["assignments"] = [a for a in state["assignments"] if a["requests"]]
        elif kind in {"actor_up", "actor_down", "actor_restored", "actor_lost", "actor_unowned", "launch_failed"}:
            actor = self._actor(data["nodeId"])
            current = actor.get("activation") or {}
            if kind == "actor_up":
                actor["activation"] = {
                    "incarnation": data["incarnation"], "desired": "up", "op": "done",
                    "effectId": data["effectId"], "operationId": data["operationId"],
                    "identityMarker": data["identityMarker"], "daemonEpoch": data.get("daemonEpoch"),
                }
            elif kind == "actor_down":
                actor["activation"] = {
                    "incarnation": data["incarnation"], "desired": "down", "op": "done",
                    "effectId": data.get("effectId", current.get("effectId", "stale")),
                    "operationId": data["operationId"],
                    "identityMarker": data["identityMarker"], "daemonEpoch": data.get("daemonEpoch"),
                }
            elif kind == "launch_failed":
                actor["activation"] = {
                    "incarnation": data["incarnation"], "desired": "up", "op": "done",
                    "effectId": data["effectId"], "operationId": data["operationId"],
                    "identityMarker": None, "daemonEpoch": data.get("daemonEpoch"),
                }
            else:
                current["daemonEpoch"] = data.get("daemonEpoch")
                if kind == "actor_lost":
                    current.update(desired="down", op="done")
                actor["activation"] = current
        elif kind == "delivery_changed":
            key = (data["eventId"], data["edge"])
            for record in state["notifications"]:
                if (record["eventId"], record["edge"]) == key:
                    record.update(messageId=data["messageId"], deliveredAt=data["deliveredAt"])
                    break
            else:
                raise ResyncRequired("delivery for unknown notification")
            for assignment in state["assignments"]:
                for request in assignment["requests"]:
                    if (request["eventId"], request["edge"]) == key:
                        request.update(messageId=data["messageId"], deliveredAt=data["deliveredAt"])
        else:
            # Lifecycle outcomes must never be interpreted as flag resets.
            raise ResyncRequired(f"unsupported event type: {kind}")
        state["cursor"] = event["seq"]
        self._refresh_blocked_on()
        # event.version attributes the recorded decision, not the current
        # structure. A late legacy receipt can be older than this snapshot.
        # Structure changes already require rebootstrap above; never roll the
        # snapshot version back (or invent a new structure from an outcome).
        state["notifications"].sort(key=lambda r: (r["eventId"], r["edge"]))
        state["assignments"].sort(key=lambda a: (a["nodeId"], a["round"]))
        for assignment in state["assignments"]:
            assignment["requests"].sort(key=lambda r: (r["eventId"], r["edge"]))
