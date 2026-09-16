"""PAC ``workflow.yaml`` schema: load, validate, and template expansion.

The schema is the contract reviewed in ``docs/design-pac-workflow.md`` §3.
Loading is fail-loud: unknown fields, wrong types, and unknown template
variables are rejected at load time, never tolerated mid-run.  The executor
(``executor.py``) only ever sees a fully validated :class:`WorkflowSpec`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from hyprial.dispatch.admission import DISPATCH_ROLES
from hyprial.duration import DurationParseError, parse_duration as _parse_duration
from hyprial.uri import parse_agent_uri

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_TARGETS = 50
MAX_TARGETS_CEILING = 1000
MAX_ATTEMPTS_CEILING = 10

_AWAIT_KINDS = ("reply", "ack")
_TIMEOUT_ACTIONS = ("retry", "escalate", "report")
_TEMPLATE_VARIABLES = frozenset({"nonce", "target"})

_TOP_LEVEL_KEYS = frozenset(
    {"version", "name", "summary", "task", "targets", "await", "on_timeout", "report_to", "limits", "hooks", "first_output_eta", "human_gates"}
)
_AWAIT_KEYS = frozenset({"kind", "timeout", "match"})
_ON_TIMEOUT_KEYS = frozenset({"action", "max_attempts", "backoff", "escalate_to"})
_LIMITS_KEYS = frozenset({"max_targets"})

_TEMPLATE_REF = re.compile(r"\{\{(\w+)\}\}")


class WorkflowSchemaError(ValueError):
    """One workflow-v1 schema violation, labelled with its field path."""


def _workflow_duration(value: object, label: str) -> float:
    """Translate the shared duration error into this v1 schema's API."""

    try:
        return _parse_duration(value, label)
    except DurationParseError as error:
        raise WorkflowSchemaError(str(error)) from error


def _reject_unknown(record: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(record) - allowed)
    if unknown:
        raise WorkflowSchemaError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowSchemaError(f"{label} must be a mapping")
    return value


def _require_string(record: dict[str, Any], key: str, label: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkflowSchemaError(f"{label}.{key} must be a non-empty string")
    return value


def _check_template_vars(text: str, label: str) -> None:
    for name in _TEMPLATE_REF.findall(text):
        if name not in _TEMPLATE_VARIABLES:
            raise WorkflowSchemaError(
                f"{label} uses unknown template variable {{{{{name}}}}}"
                f" (allowed: {', '.join(sorted(_TEMPLATE_VARIABLES))})"
            )


def expand_template(text: str, *, nonce: str, target: str) -> str:
    """Expand ``{{nonce}}`` / ``{{target}}`` — the only two variables."""
    return (
        text.replace("{{nonce}}", nonce)
        .replace("{{target}}", target)
    )


def looks_like_canonical_agent_uri(value: str) -> bool:
    """Is the target a canonical ``agent:<owner>:<machine>:<actor>`` URI?

    The shared parser lives in the dependency-free ``hyprial.uri`` leaf module,
    so schema loading does not execute the eager ``hyprial.daemon`` package.
    """

    return parse_agent_uri(value) is not None


def pinned_node_warnings(spec: "WorkflowSpec") -> tuple[str, ...]:
    """Y1 (transfer footgun): warn — never reject — when a target pins a node.

    A canonical ``agent:<owner>:<node>:<actor>`` URI passes through the
    daemon's resolve-at-send boundary untouched, so a transfer of that actor
    mid-run strands every (re-)dispatch on the old node until the budget
    burns out (design-pac-workflow Q1 analysis, 2026-08-21).  A logical name
    re-resolves on every dispatch and follows the registration to the new
    node.  Pinning MAY be deliberate, so this is advisory only.
    """

    return tuple(
        f"target {target.name!r} 焊死物理 node:被派 actor 若 transfer 跨机,投递将悬空烧预算;"
        "建议用逻辑名(nickname),除非钉死是故意"
        for target in spec.targets
        if looks_like_canonical_agent_uri(target.name)
    )


@dataclass(frozen=True, slots=True)
class HumanGate:
    who: str
    what: str


def _dispatch_fields(record: dict[str, Any], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if "first_output_eta" in record:
        if not isinstance(record["first_output_eta"], str):
            raise WorkflowSchemaError(f"{label}.first_output_eta must be a string")
        result["first_output_eta"] = record["first_output_eta"]
    if "human_gates" in record:
        raw = record["human_gates"]
        if raw == "none":
            result["human_gates"] = "none"
        elif isinstance(raw, list):
            gates = []
            for index, item in enumerate(raw):
                item_label = f"{label}.human_gates[{index}]"
                gate = _require_mapping(item, item_label)
                _reject_unknown(gate, frozenset({"who", "what"}), item_label)
                gates.append(HumanGate(
                    _require_string(gate, "who", item_label),
                    _require_string(gate, "what", item_label),
                ))
            result["human_gates"] = tuple(gates)
        else:
            raise WorkflowSchemaError(f"{label}.human_gates must be 'none' or a list of {{who, what}}")
    return result


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """One dispatch target: a name, plus an optional per-target task override.

    The override exists because the first realistic duty script (three workers
    review three different PRs) needs per-target task text; without it the
    shared task must indirect through {{target}} lookups, which the example
    proved awkward (design-pac-workflow §3, ergonomics amendment).
    """

    name: str
    task: str | None = None
    role: str = "execute"
    first_output_eta: str | None = None
    human_gates: str | tuple[HumanGate, ...] | None = None


@dataclass(frozen=True, slots=True)
class AwaitSpec:
    kind: Literal["reply", "ack"] = "reply"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    match: str | None = None


@dataclass(frozen=True, slots=True)
class OnTimeoutSpec:
    action: Literal["retry", "escalate", "report"] = "report"
    max_attempts: int = 1
    backoff_seconds: tuple[float, ...] = ()
    escalate_to: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    name: str
    task: str
    targets: tuple[TargetSpec, ...]
    await_: AwaitSpec
    on_timeout: OnTimeoutSpec
    summary: str | None = None
    report_to: str | None = None
    max_targets: int = DEFAULT_MAX_TARGETS
    first_output_eta: str | None = None
    human_gates: str | tuple[HumanGate, ...] | None = None

    def dispatch_fields_for(self, target: TargetSpec) -> tuple[str | None, bool]:
        """Target declarations override root values; absence inherits."""
        eta = target.first_output_eta if target.first_output_eta is not None else self.first_output_eta
        gates = target.human_gates if target.human_gates is not None else self.human_gates
        return eta, gates is not None

    def task_for(self, target: str) -> str:
        """The effective task text for one target (its override, else the shared task)."""
        for spec in self.targets:
            if spec.name == target:
                return spec.task if spec.task is not None else self.task
        raise KeyError(target)

    @property
    def worst_case_seconds(self) -> float:
        """Upper bound for one target: timeout x attempts plus all backoffs."""
        return (
            self.await_.timeout_seconds * self.on_timeout.max_attempts
            + sum(self.on_timeout.backoff_seconds)
        )


def load_workflow_text(text: str, *, label: str = "workflow") -> WorkflowSpec:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise WorkflowSchemaError(f"{label} is not valid YAML: {error}") from error
    root = _require_mapping(document, label)
    _reject_unknown(root, _TOP_LEVEL_KEYS, label)

    version = root.get("version")
    if version != SCHEMA_VERSION:
        raise WorkflowSchemaError(
            f"{label}.version must be {SCHEMA_VERSION}, got {version!r}"
        )

    name = _require_string(root, "name", label)
    summary = root.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise WorkflowSchemaError(f"{label}.summary must be a string")
    task = _require_string(root, "task", label)
    _check_template_vars(task, f"{label}.task")

    # ── limits (parsed before targets: it bounds their count) ────────────
    limits_raw = root.get("limits")
    max_targets = DEFAULT_MAX_TARGETS
    if limits_raw is not None:
        limits = _require_mapping(limits_raw, f"{label}.limits")
        _reject_unknown(limits, _LIMITS_KEYS, f"{label}.limits")
        raw_max = limits.get("max_targets", DEFAULT_MAX_TARGETS)
        if (
            isinstance(raw_max, bool)
            or not isinstance(raw_max, int)
            or not 1 <= raw_max <= MAX_TARGETS_CEILING
        ):
            raise WorkflowSchemaError(
                f"{label}.limits.max_targets must be an integer in 1..{MAX_TARGETS_CEILING}"
            )
        max_targets = raw_max

    # ── targets ─────────────────────────────────────────────────────────
    targets_raw = root.get("targets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise WorkflowSchemaError(f"{label}.targets must be a non-empty list")
    targets: list[TargetSpec] = []
    for index, item in enumerate(targets_raw):
        item_label = f"{label}.targets[{index}]"
        if isinstance(item, str):
            if not item.strip():
                raise WorkflowSchemaError(f"{item_label} must be a non-empty string")
            targets.append(TargetSpec(name=item.strip()))
            continue
        if isinstance(item, dict):
            _reject_unknown(item, frozenset({"name", "task", "role", "first_output_eta", "human_gates"}), item_label)
            target_name = _require_string(item, "name", item_label)
            target_task = item.get("task")
            if target_task is not None:
                if not isinstance(target_task, str) or not target_task.strip():
                    raise WorkflowSchemaError(f"{item_label}.task must be a non-empty string")
                _check_template_vars(target_task, f"{item_label}.task")
            role = item.get("role", "execute")
            if not isinstance(role, str) or role not in DISPATCH_ROLES:
                raise WorkflowSchemaError(f"{item_label}.role must be plan, dispatch, review, or execute")
            targets.append(TargetSpec(
                name=target_name, task=target_task, role=role,
                **_dispatch_fields(item, item_label),
            ))
            continue
        raise WorkflowSchemaError(
            f"{item_label} must be a name string or a {{name, task?}} mapping"
        )
    names = [target.name for target in targets]
    if len(set(names)) != len(names):
        raise WorkflowSchemaError(f"{label}.targets contains duplicates")
    if len(targets) > max_targets:
        raise WorkflowSchemaError(
            f"{label}.targets has {len(targets)} entries, above the "
            f"{max_targets} limit (raise limits.max_targets deliberately)"
        )

    # ── await ────────────────────────────────────────────────────────────
    await_raw = root.get("await")
    await_spec = AwaitSpec()
    if await_raw is not None:
        await_map = _require_mapping(await_raw, f"{label}.await")
        _reject_unknown(await_map, _AWAIT_KEYS, f"{label}.await")
        kind = await_map.get("kind", "reply")
        if kind not in _AWAIT_KINDS:
            raise WorkflowSchemaError(
                f"{label}.await.kind must be one of {_AWAIT_KINDS}, got {kind!r}"
            )
        match = await_map.get("match")
        if match is not None:
            if not isinstance(match, str) or not match.strip():
                raise WorkflowSchemaError(f"{label}.await.match must be a non-empty string")
            _check_template_vars(match, f"{label}.await.match")
        await_spec = AwaitSpec(
            kind=kind,
            timeout_seconds=_workflow_duration(
                await_map.get("timeout", DEFAULT_TIMEOUT_SECONDS), f"{label}.await.timeout"
            ),
            match=match,
        )

    # ── on_timeout ───────────────────────────────────────────────────────
    timeout_raw = root.get("on_timeout")
    on_timeout = OnTimeoutSpec()
    if timeout_raw is not None:
        timeout_map = _require_mapping(timeout_raw, f"{label}.on_timeout")
        _reject_unknown(timeout_map, _ON_TIMEOUT_KEYS, f"{label}.on_timeout")
        action = timeout_map.get("action", "report")
        if action not in _TIMEOUT_ACTIONS:
            raise WorkflowSchemaError(
                f"{label}.on_timeout.action must be one of {_TIMEOUT_ACTIONS}, got {action!r}"
            )
        max_attempts = timeout_map.get("max_attempts", 1)
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= MAX_ATTEMPTS_CEILING
        ):
            raise WorkflowSchemaError(
                f"{label}.on_timeout.max_attempts must be an integer in "
                f"1..{MAX_ATTEMPTS_CEILING}"
            )
        backoff_raw = timeout_map.get("backoff", [])
        if not isinstance(backoff_raw, list):
            raise WorkflowSchemaError(f"{label}.on_timeout.backoff must be a list of durations")
        backoff = tuple(
            _workflow_duration(item, f"{label}.on_timeout.backoff[{index}]")
            for index, item in enumerate(backoff_raw)
        )
        escalate_to = timeout_map.get("escalate_to")
        if escalate_to is not None and (not isinstance(escalate_to, str) or not escalate_to.strip()):
            raise WorkflowSchemaError(f"{label}.on_timeout.escalate_to must be a non-empty string")
        if action == "escalate" and escalate_to is None:
            raise WorkflowSchemaError(
                f"{label}.on_timeout.action=escalate requires escalate_to"
            )
        on_timeout = OnTimeoutSpec(
            action=action,
            max_attempts=max_attempts,
            backoff_seconds=backoff,
            escalate_to=escalate_to,
        )

    report_to = root.get("report_to")
    if report_to is not None and (not isinstance(report_to, str) or not report_to.strip()):
        raise WorkflowSchemaError(f"{label}.report_to must be a non-empty string")

    # ── hooks (v2 extension slot; v1 accepts only an empty mapping) ───────
    hooks_raw = root.get("hooks")
    if hooks_raw is not None:
        hooks = _require_mapping(hooks_raw, f"{label}.hooks")
        if hooks:
            raise WorkflowSchemaError(
                f"{label}.hooks names {sorted(hooks)} but v1 ships no named hooks "
                "— this section is the v2 extension slot (design-pac-workflow §4.7)"
            )

    return WorkflowSpec(
        name=name,
        summary=summary,
        task=task,
        targets=tuple(targets),
        await_=await_spec,
        on_timeout=on_timeout,
        report_to=report_to,
        max_targets=max_targets,
        **_dispatch_fields(root, label),
    )


def load_workflow(path: Path) -> WorkflowSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise WorkflowSchemaError(f"cannot read {path}: {error}") from error
    return load_workflow_text(text, label=str(path))
