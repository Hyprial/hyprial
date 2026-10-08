"""Pure parent and child expansion validators."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.pac.contracts.expansion.document import (
    parse_expansion_document,
)
from hyprial.daemon.impl.pac.contracts.expansion.policy import (
    _contains,
    effective_limits,
)
from hyprial.daemon.impl.pac.contracts.workflow import (
    WorkflowSchemaError,
    WorkflowSpec,
    launch,
    load_workflow_text,
)
from hyprial.identity import (
    PAC_EXPANSION_INVALID,
    PAC_EXPANSION_POLICY_UNAVAILABLE,
    PacError,
)
from hyprial.kernel import parse_duration, tier_for_model

_CHILD_ROOT_KEYS = {"version", "nodes", "edges"}
_CHILD_NODE_KEYS = {
    "id",
    "kind",
    "task",
    "role",
    "owner",
    "launch",
    "after",
    "timeout",
    "deadline_ms",
    "first_output_eta",
    "human_gates",
}


def _valid_branch_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and value not in {"", ".", ".."}
        and not value.startswith(".")
        and not value.endswith((".", ".lock"))
        and ".." not in value
        and not any(character in value for character in "~^:?*[\\\x00")
        and not any(ord(character) <= 32 or ord(character) == 127 for character in value)
    )


def _invalid(field: str, reason: str, message: str) -> None:
    raise PacError(PAC_EXPANSION_INVALID, message, {"field": field, "reason": reason})


def _schema(error: Exception, field: str = "document") -> None:
    message = str(error)
    reason = "syntax"
    for candidate in ("unknown", "required", "kind", "cycle", "timeout", "args", "owner", "cwd"):
        if candidate in message:
            reason = candidate
            break
    _invalid(field, reason, message)


def _limits_shape(limits: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(limits, dict):
        _invalid("limits", "required", "effective expansion limits are required")
    required = {"maxNodes", "tiers", "owners", "cwdUnder"}
    if not required.issubset(limits):
        _invalid("limits", "required", "effective expansion limits are incomplete")
    if type(limits["maxNodes"]) is not int or limits["maxNodes"] < 1:
        _invalid("limits.maxNodes", "count", "maxNodes must be positive")
    if not isinstance(limits["tiers"], list) or not isinstance(limits["owners"], list) or not isinstance(limits["cwdUnder"], list):
        _invalid("limits", "unexpected", "effective expansion limits have invalid list fields")
    return limits


def _check_cwd(cwd: str, limits: dict[str, Any], field: str) -> str:
    path = Path(cwd)
    if not path.is_absolute():
        _invalid(field, "cwd", "cwd must be absolute")
    canonical = path.resolve(strict=False)
    allowed = [Path(item).resolve(strict=False) for item in limits["cwdUnder"]]
    if not any(_contains(root, canonical) for root in allowed):
        _invalid(field, "cwd", "cwd is outside the trusted cwdUnder roots")
    return str(canonical)


def _check_tier(raw_launch: dict[str, Any], normalized: dict[str, Any], limits: dict[str, Any], field: str) -> None:
    args = raw_launch.get("args", [])
    if args:
        _invalid(field + ".args", "args", "child launches cannot carry native args")
    model_tier = tier_for_model(normalized.get("model"))
    if model_tier is None or model_tier not in limits["tiers"]:
        _invalid(field, "tier", "launch model exceeds or is absent from the trusted tier ceiling")


def _child_document(raw: dict[str, Any], limits: dict[str, Any], child_graph_id: str, deadline_ms: int, at: int) -> dict[str, Any]:
    unknown = set(raw) - _CHILD_ROOT_KEYS
    if unknown:
        _invalid("document", "unknown-field", f"child root has unknown fields: {', '.join(sorted(unknown))}")
    if type(raw.get("version")) is not int or raw["version"] != 2:
        _invalid("version", "kind", "child document version must be 2")
    if not isinstance(raw.get("nodes"), list):
        _invalid("nodes", "required", "child document.nodes is required")
    if len(raw["nodes"]) > limits["maxNodes"]:
        _invalid("nodes", "count", "child node count exceeds maxNodes")
    if not isinstance(child_graph_id, str) or not child_graph_id or Path(child_graph_id).name != child_graph_id:
        _invalid("childGraphId", "syntax", "child graph id must be a single path-safe identifier")
    if type(deadline_ms) is not int or type(at) is not int or deadline_ms <= at:
        _invalid("deadlineMs", "timeout", "child deadline must be after validation time")
    nodes: list[dict[str, Any]] = []
    for index, item in enumerate(raw["nodes"]):
        field = f"nodes[{index}]"
        if not isinstance(item, dict):
            _invalid(field, "syntax", "child nodes must be objects")
        unknown = set(item) - _CHILD_NODE_KEYS
        if unknown:
            _invalid(field, "unknown-field", f"child node has unknown fields: {', '.join(sorted(unknown))}")
        if not _valid_branch_component(item.get("id")):
            _invalid(
                field + ".id",
                "syntax",
                "child node id is not a valid git branch component",
            )
        kind = item.get("kind", "task")
        if kind not in ("task", "report"):
            _invalid(field + ".kind", "kind", "child nodes may only be task or report")
        has_owner = "owner" in item
        has_launch = "launch" in item
        if has_owner and has_launch:
            _invalid(field, "owner", "child node must choose owner or launch")
        if has_owner and not isinstance(item["owner"], str):
            _invalid(field + ".owner", "owner", "child owner must be text")
        if has_owner and item["owner"] not in limits["owners"]:
            _invalid(field + ".owner", "owner", "child owner is not trusted by policy")
        role = item.get("role", "execute")
        if not has_owner:
            raw_launch = item.get("launch", {"tier": "fast"})
            if not isinstance(raw_launch, dict):
                _invalid(field + ".launch", "syntax", "child launch must be an object")
            try:
                normalized = launch(raw_launch, field + ".launch")
            except WorkflowSchemaError as error:
                _schema(error, field + ".launch")
            _check_tier(raw_launch, normalized, limits, field + ".launch")
            cwd = raw_launch.get("cwd")
            if cwd is not None:
                raw_launch = dict(raw_launch)
                raw_launch["cwd"] = _check_cwd(cwd, limits, field + ".launch.cwd")
            elif role != "execute":
                _invalid(field + ".launch.cwd", "cwd", "non-execute owned nodes require an explicit cwd")
            else:
                managed = Path(limits.get("workRoot", ".")) / ".pac-worktrees" / child_graph_id / str(item.get("id", ""))
                if not any(_contains(Path(root).resolve(strict=False), managed.resolve(strict=False)) for root in limits["cwdUnder"]):
                    _invalid(field + ".launch.cwd", "cwd", "managed worktree root is outside cwdUnder")
            item = dict(item)
            item["launch"] = raw_launch
        timeout_value = item.get("timeout", "1h")
        try:
            timeout_ms = int(parse_duration(timeout_value, field + ".timeout") * 1000)
        except (ValueError, TypeError) as error:
            _schema(error, field + ".timeout")
        if timeout_ms > deadline_ms - at:
            _invalid(field + ".timeout", "timeout", "child timeout exceeds expansion deadline")
        node_deadline = item.get("deadline_ms")
        if node_deadline is not None and (type(node_deadline) is not int or not at < node_deadline <= deadline_ms):
            _invalid(field + ".deadline_ms", "timeout", "child deadline is outside the expansion window")
        nodes.append(item)
    result = {"version": 2, "name": str(raw.get("name", "child")), "nodes": nodes}
    if "edges" in raw:
        result["edges"] = raw["edges"]
    return result


def validate_expansion(raw: str, *, limits: dict, child_graph_id: str, deadline_ms: int, at: int, name: str, escalate_to: str | None) -> WorkflowSpec:
    """Validate an authored child graph without persistence or external effects."""

    limits = _limits_shape(limits)
    parsed = parse_expansion_document(raw)
    document = _child_document(parsed, limits, child_graph_id, deadline_ms, at)
    document["name"] = name
    try:
        spec = load_workflow_text(json.dumps(document))
    except WorkflowSchemaError as error:
        _schema(error)
    if any(kind == "back" for _, _, kind in spec.edges):
        _invalid("edges", "cycle", "child expansion cannot contain back edges")
    return replace(spec, on_failure="terminate", escalate_to=escalate_to)


def validate_parent(spec: WorkflowSpec, *, policy: dict) -> WorkflowSpec:
    """Validate the one-level parent binding and attach a trusted snapshot."""

    expanding = [node for node in spec.nodes if node.expands is not None]
    placeholders = [node for node in spec.nodes if node.kind == "expansion"]
    if not expanding and not placeholders:
        return spec
    if not policy or not isinstance(policy, dict) or "limits" not in policy or "workRoot" not in policy:
        raise PacError(PAC_EXPANSION_POLICY_UNAVAILABLE, "trusted expansion policy is unavailable", {"field": "policy", "reason": "unavailable"})
    if len(expanding) != 1 or len(placeholders) != 1:
        _invalid("nodes", "unexpected", "a parent must contain exactly one planner and placeholder")
    planner = expanding[0]
    placeholder = placeholders[0]
    if planner.kind != "task" or planner.role != "plan":
        _invalid(f"nodes.{planner.id}", "kind", "expansion planner must be a task with role plan")
    if planner.limits is not None:
        _invalid(f"nodes.{planner.id}.limits", "unexpected", "limits belong on the expansion placeholder")
    if planner.expands != placeholder.id:
        _invalid(f"nodes.{planner.id}.expands", "required", "planner must name the expansion placeholder")
    incoming = [(source, kind) for source, target, kind in spec.edges if target == placeholder.id]
    if incoming != [(planner.id, "forward")]:
        _invalid(f"nodes.{placeholder.id}", "unexpected", "placeholder must have one planner forward predecessor")
    if any(kind == "back" and (source in {planner.id, placeholder.id} or target in {planner.id, placeholder.id}) for source, target, kind in spec.edges):
        _invalid("edges", "cycle", "expansion endpoints cannot be touched by back edges")
    if any(node.kind == "expansion" and node is not placeholder for node in spec.nodes):
        _invalid("nodes", "unexpected", "only one expansion placeholder is permitted")
    effective_limits(placeholder.limits, policy=policy)
    return replace(spec, expansion_policy=policy)


__all__ = ["validate_expansion", "validate_parent"]
