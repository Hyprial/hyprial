"""Strict declarative workflow input; compiling a plan performs no effects.

Format 2 describes PAC nodes and worker ownership. Format 1 is deliberately
rejected: translating its reply matcher/retry/report would invent semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
import json
from pathlib import Path
import re
from typing import Any

import yaml

from hyprial.dispatch.matrix import resolve
from hyprial.duration import DurationParseError, parse_duration
from hyprial.pac.principal import parse_principal
from hyprial.pac.errors import PacError

MAX_NODES = 100
MAX_TEXT = 65536
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
POLICIES = ("terminate", "continue", "hold")


class WorkflowSchemaError(ValueError):
    code = "WORKFLOW_SCHEMA_ERROR"


def mapping(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise WorkflowSchemaError(f"{label} must be a string-keyed mapping")
    unknown = set(value) - allowed
    if unknown:
        raise WorkflowSchemaError(
            f"{label}: unknown fields {', '.join(sorted(unknown))}"
        )
    return value


def text(value: Any, label: str, *, maximum: int = MAX_TEXT) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\0" in value
    ):
        raise WorkflowSchemaError(f"{label} must be nonempty text (maximum {maximum})")
    return value


def name(value: Any, label: str) -> str:
    value = text(value, label, maximum=64)
    if not NAME.fullmatch(value):
        raise WorkflowSchemaError(
            f"{label} must start with a letter and contain letters, digits, _, . or -"
        )
    return value


def principal(value: Any, label: str) -> str:
    value = text(value, label, maximum=512)
    try:
        parse_principal(value)
    except (ValueError, PacError) as error:
        raise WorkflowSchemaError(f"{label}: {error}") from error
    return value


def launch(value: Any, label: str) -> dict[str, Any]:
    value = mapping(
        value, {"tier", "harness", "provider", "model", "cwd", "args"}, label
    )
    result = dict(value)
    if "tier" in value:
        if value["tier"] not in ("fast", "strong", "super"):
            raise WorkflowSchemaError(f"{label}.tier must be fast, strong or super")
        if any(key in value for key in ("harness", "provider", "model")):
            raise WorkflowSchemaError(
                f"{label}: choose tier OR explicit harness/provider/model"
            )
        choice = resolve(value["tier"])
        result.update(
            harness=choice.harness, provider=choice.provider, model=choice.model
        )
    else:
        text(value.get("harness"), f"{label}.harness", maximum=64)
        for key in ("provider", "model"):
            if key in value:
                text(value[key], f"{label}.{key}", maximum=256)
    if "cwd" in value:
        cwd = text(value["cwd"], f"{label}.cwd", maximum=4096)
        if not Path(cwd).is_absolute():
            raise WorkflowSchemaError(f"{label}.cwd must be absolute")
    args = value.get("args", [])
    if not isinstance(args, list) or any(
        not isinstance(arg, str) or "\0" in arg for arg in args
    ):
        raise WorkflowSchemaError(f"{label}.args must be a list of strings")
    overrides = any(
        arg in {"--model", "--provider", "-m"}
        or arg.startswith(("--model=", "--provider=", "-m="))
        or (
            arg in {"-c", "--config"}
            and index + 1 < len(args)
            and args[index + 1].startswith(
                ("model=", "model_provider=", "model_providers.")
            )
        )
        or arg.startswith(("--config=model", "-c=model"))
        for index, arg in enumerate(args)
    )
    if overrides and any(key in value for key in ("tier", "model", "provider")):
        raise WorkflowSchemaError(
            f"{label}: native args cannot override the declared model selection"
        )
    result["args"] = args
    return result


@dataclass(frozen=True)
class WorkflowNode:
    id: str
    kind: str
    task: str
    owner: str | None
    worker: str | None
    launch: dict[str, Any] | None
    timeout_ms: int
    deadline_ms: int | None
    role: str
    first_output_eta: str | None
    human_gates: Any

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "task": self.task,
            "owner": self.owner,
            "worker": self.worker,
            "launch": self.launch,
            "timeoutMs": self.timeout_ms,
            "deadlineMs": self.deadline_ms,
            "role": self.role,
            "firstOutputEta": self.first_output_eta,
            "humanGates": self.human_gates,
            "ownership": "borrowed" if self.owner else "workflow",
        }


@dataclass(frozen=True)
class WorkflowSpec:
    name: str
    summary: str
    nodes: tuple[WorkflowNode, ...]
    edges: tuple[tuple[str, str, str], ...]
    on_failure: str
    escalate_to: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 2,
            "name": self.name,
            "summary": self.summary,
            "nodes": [n.to_json() for n in self.nodes],
            "edges": [{"from": a, "to": b, "kind": k} for a, b, k in self.edges],
            "onFailure": self.on_failure,
            "escalateTo": self.escalate_to,
            "completion": "owner flag with current request evidence",
            "workerCleanup": "graph close; borrowed actors are never stopped",
        }

    @property
    def canonical(self) -> str:
        return json.dumps(self.to_json(), sort_keys=True, ensure_ascii=False)


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(
    loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False
) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise WorkflowSchemaError("duplicate or non-string YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def load_workflow_text(raw: str) -> WorkflowSpec:
    if not isinstance(raw, str) or len(raw.encode()) > 1024 * 1024:
        raise WorkflowSchemaError("workflow must be UTF-8 YAML of at most 1 MiB")
    try:
        doc = yaml.load(raw, Loader=_UniqueLoader)
    except (yaml.YAMLError, RecursionError) as error:
        raise WorkflowSchemaError(f"invalid workflow YAML: {error}") from error
    root = mapping(
        doc,
        {
            "version",
            "name",
            "summary",
            "nodes",
            "edges",
            "workers",
            "defaults",
            "on_failure",
            "escalate_to",
        },
        "workflow",
    )
    if type(root.get("version")) is not int or root["version"] != 2:
        raise WorkflowSchemaError(
            "workflow.version must be 2; legacy targets/await/retry/report semantics are retired"
        )
    workflow_name = text(root.get("name"), "workflow.name", maximum=128)
    summary = root.get("summary", "")
    if not isinstance(summary, str) or len(summary) > MAX_TEXT:
        raise WorkflowSchemaError("workflow.summary must be bounded text")
    policy = root.get("on_failure", "terminate")
    if policy not in POLICIES:
        raise WorkflowSchemaError(f"workflow.on_failure must be one of {POLICIES}")
    escalation = (
        principal(root["escalate_to"], "escalate_to") if "escalate_to" in root else None
    )
    defaults = mapping(
        root.get("defaults", {}),
        {"launch", "timeout", "role", "first_output_eta", "human_gates"},
        "defaults",
    )
    default_launch = defaults.get("launch", {"tier": "fast"})
    launch(default_launch, "defaults.launch")
    workers = root.get("workers", {})
    if not isinstance(workers, dict):
        raise WorkflowSchemaError("workers must be a mapping")
    worker_specs = {
        name(k, "worker key"): launch(v, f"workers.{k}") for k, v in workers.items()
    }
    nodes_raw = root.get("nodes")
    if not isinstance(nodes_raw, list) or not 1 <= len(nodes_raw) <= MAX_NODES:
        raise WorkflowSchemaError(f"nodes must contain 1..{MAX_NODES} nodes")
    nodes = []
    edges: list[tuple[str, str, str]] = []
    identifiers: set[str] = set()
    for i, raw_node in enumerate(nodes_raw):
        label = f"nodes[{i}]"
        item = mapping(
            raw_node,
            {
                "id",
                "kind",
                "task",
                "owner",
                "worker",
                "launch",
                "timeout",
                "deadline_ms",
                "after",
                "role",
                "first_output_eta",
                "human_gates",
            },
            label,
        )
        node_id = name(item.get("id"), label + ".id")
        if node_id in identifiers:
            raise WorkflowSchemaError(f"duplicate node {node_id}")
        identifiers.add(node_id)
        kind = item.get("kind", "task")
        if kind not in ("task", "approval", "report", "end"):
            raise WorkflowSchemaError(
                f"{label}.kind must be task, approval, report or end"
            )
        owner = principal(item["owner"], label + ".owner") if "owner" in item else None
        if kind in ("approval", "end") and owner is None:
            raise WorkflowSchemaError(f"{label}: {kind} requires an explicit owner")
        if owner and any(key in item for key in ("worker", "launch")):
            raise WorkflowSchemaError(
                f"{label}: an explicit owner is borrowed; worker/launch cannot adopt it"
            )
        worker = None if owner else name(item.get("worker", node_id), label + ".worker")
        if worker in worker_specs and "worker" not in item:
            raise WorkflowSchemaError(
                f"{label}: sharing worker {worker!r} requires an explicit worker field"
            )
        if "worker" in item and worker not in worker_specs:
            raise WorkflowSchemaError(
                f"{label}.worker must reference a declared workers key"
            )
        if "worker" in item and "launch" in item:
            raise WorkflowSchemaError(
                f"{label}: shared worker launch belongs in workers"
            )
        runtime = (
            None
            if owner
            else (
                worker_specs[worker]
                if worker in worker_specs
                else launch(item.get("launch", default_launch), label + ".launch")
            )
        )
        task = text(item.get("task"), label + ".task")
        try:
            timeout = int(
                parse_duration(
                    item.get("timeout", defaults.get("timeout", "1h")),
                    label + ".timeout",
                )
                * 1000
            )
        except DurationParseError as error:
            raise WorkflowSchemaError(str(error)) from error
        if timeout <= 0:
            raise WorkflowSchemaError(f"{label}.timeout must be positive")
        deadline = item.get("deadline_ms")
        if deadline is not None and (type(deadline) is not int or deadline <= 0):
            raise WorkflowSchemaError(f"{label}.deadline_ms must be a positive integer")
        role = item.get(
            "role",
            defaults.get(
                "role", "review" if kind in ("approval", "end") else "execute"
            ),
        )
        if role not in ("execute", "plan", "dispatch", "review"):
            raise WorkflowSchemaError(f"{label}.role is invalid")
        eta = item.get("first_output_eta", defaults.get("first_output_eta"))
        if eta is not None:
            text(eta, label + ".first_output_eta", maximum=512)
        gates = item.get("human_gates", defaults.get("human_gates"))
        if gates is not None and gates != "none":
            if not isinstance(gates, list) or not gates:
                raise WorkflowSchemaError(
                    f"{label}.human_gates must be none or nonempty list"
                )
            for gate in gates:
                gate = mapping(gate, {"who", "what"}, label + ".human_gates")
                text(gate.get("who"), "human gate who", maximum=512)
                text(gate.get("what"), "human gate what")
        nodes.append(
            WorkflowNode(
                node_id,
                kind,
                task,
                owner,
                worker,
                runtime,
                timeout,
                deadline,
                role,
                eta,
                gates,
            )
        )
        after = item.get("after", [])
        if not isinstance(after, list):
            raise WorkflowSchemaError(f"{label}.after must be a list")
        edges.extend(
            (name(pred, label + ".after"), node_id, "forward") for pred in after
        )
    edges_raw = root.get("edges", [])
    if not isinstance(edges_raw, list):
        raise WorkflowSchemaError("edges must be a list")
    for edge in edges_raw:
        edge = mapping(edge, {"from", "to", "kind"}, "edge")
        kind = edge.get("kind", "forward")
        if kind not in ("forward", "back"):
            raise WorkflowSchemaError("edge.kind must be forward or back")
        edges.append(
            (name(edge.get("from"), "edge.from"), name(edge.get("to"), "edge.to"), kind)
        )
    adjacency: dict[str, set[str]] = {n: set() for n in identifiers}
    pairs = set()
    for a, b, kind in edges:
        if a not in identifiers or b not in identifiers or a == b or (a, b) in pairs:
            raise WorkflowSchemaError(f"invalid or duplicate edge {a} -> {b}")
        pairs.add((a, b))
        if kind == "forward":
            adjacency[b].add(a)
    end_nodes = {n.id for n in nodes if n.kind == "end"}
    if any(source in end_nodes for source, _, _ in edges):
        raise WorkflowSchemaError("end nodes cannot have outgoing edges")
    try:
        tuple(TopologicalSorter(adjacency).static_order())
    except CycleError as error:
        raise WorkflowSchemaError(
            "forward edges must be acyclic; declare rework as a back edge"
        ) from error
    used_workers = {n.worker for n in nodes if n.worker is not None}
    if set(worker_specs) - used_workers:
        raise WorkflowSchemaError("workers contains unused declarations")
    if any(kind == "back" for _, _, kind in edges) and not any(
        n.kind == "end" for n in nodes
    ):
        raise WorkflowSchemaError(
            "a workflow with back edges needs an explicit end node"
        )
    for worker in used_workers:
        launches = {
            json.dumps(n.launch, sort_keys=True) for n in nodes if n.worker == worker
        }
        if len(launches) != 1:
            raise WorkflowSchemaError(
                f"worker {worker!r} has conflicting launch declarations"
            )
    return WorkflowSpec(
        workflow_name, summary, tuple(nodes), tuple(edges), policy, escalation
    )


def load_workflow(path: Path) -> WorkflowSpec:
    try:
        return load_workflow_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise WorkflowSchemaError(str(error)) from error
