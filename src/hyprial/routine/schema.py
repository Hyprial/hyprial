"""``routine.yaml`` schema: load and validate, fail-loud (PAC schema house rules).

Unknown fields, bad enums, and missing required sections are rejected at
load; the daemon never sees a partially-valid routine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from hyprial.duration import DurationParseError, parse_duration
from hyprial.uri import delivery_address_error, parse_agent_uri

SCHEMA_VERSION = 1
MIN_INTERVAL_SECONDS = 60.0
DEFAULT_IDLE_THRESHOLD_SECONDS = 30 * 60.0

_TOP_KEYS = frozenset(
    {"version", "name", "schedule", "source", "policy", "limits", "on_task_timeout", "produces"}
)
_SCHEDULE_KEYS = frozenset({"interval"})
_SOURCE_KEYS = frozenset({"kind", "filter", "idle_threshold"})
_ROUTE_KEYS = frozenset({"tag", "target", "target_from", "escalate_to"})
_LIMITS_KEYS = frozenset({"max_in_flight", "circuit_breaker"})
_BREAKER_KEYS = frozenset({"window_runs", "escalate_ratio", "action"})
_ON_TIMEOUT_KEYS = frozenset({"action", "escalate_to"})
_SOURCE_KINDS = ("taskwarrior", "pac-journal")
_POLICY_KEYS = frozenset({"routes", "default", "task_template"})
_BREAKER_ACTIONS = ("pause+alarm",)
_TIMEOUT_ACTIONS = ("escalate",)
_TEMPLATE_VAR = re.compile(r"\{\{([\w.]+)\}\}")
_TEMPLATE_VARS = frozenset(
    {"nonce", "target", "reason"}
    | {"task.uuid", "task.description", "task.tags"}
)


class RoutineSchemaError(ValueError):
    """One routine schema violation, labelled with the field path."""


def _duration(value: object, label: str) -> float:
    try:
        return parse_duration(value, label)
    except DurationParseError as error:
        raise RoutineSchemaError(str(error)) from error


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoutineSchemaError(f"{label} must be a mapping")
    return value


def _reject_unknown(record: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(record) - allowed)
    if unknown:
        raise RoutineSchemaError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _string(record: dict[str, Any], key: str, label: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RoutineSchemaError(f"{label}.{key} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class RouteRule:
    """One routing rule: when the task carries `tag`, send it…

    `target_from == "tagarg"` reads the worker name from the tag itself
    (``route:worker:<name>``); `target == "self"` means the routine owner's
    own actor (the deterministic self-wake); `escalate_to` routes to a human.
    """

    tag: str
    kind: Literal["target", "self", "escalate"]
    value: str  # tagarg | self | the escalate destination


@dataclass(frozen=True, slots=True)
class CircuitBreaker:
    window_runs: int = 5
    escalate_ratio: float = 1.0
    action: Literal["pause+alarm"] = "pause+alarm"


@dataclass(frozen=True, slots=True)
class RoutineLimits:
    max_in_flight: int = 3
    circuit_breaker: CircuitBreaker = CircuitBreaker()


@dataclass(frozen=True, slots=True)
class RoutineSpec:
    name: str
    interval_seconds: float
    source_kind: str
    source_filter: str
    source_idle_threshold_seconds: float
    routes: tuple[RouteRule, ...]
    default_route: Literal["escalate", "self"]
    task_template: str
    limits: RoutineLimits
    escalate_to: str
    # Reserved declaration only; does not create, own, or start an actor.
    produces: str | None = None


def _check_vars(text: str, label: str) -> None:
    for name in _TEMPLATE_VAR.findall(text):
        if name not in _TEMPLATE_VARS:
            raise RoutineSchemaError(
                f"{label} uses unknown template variable {{{{{name}}}}}"
                f" (allowed: {', '.join(sorted(_TEMPLATE_VARS))})"
            )


def load_routine_text(text: str, *, label: str = "routine") -> RoutineSpec:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise RoutineSchemaError(f"{label} is not valid YAML: {error}") from error
    root = _mapping(document, label)
    _reject_unknown(root, _TOP_KEYS, label)
    if root.get("version") != SCHEMA_VERSION:
        raise RoutineSchemaError(f"{label}.version must be {SCHEMA_VERSION}")
    name = _string(root, "name", label)
    produces = None
    if "produces" in root:
        produces = _string(root, "produces", label)
        if parse_agent_uri(produces) is None:
            raise RoutineSchemaError(f"{label}.produces must be a canonical agent URI")

    schedule = _mapping(root.get("schedule"), f"{label}.schedule")
    _reject_unknown(schedule, _SCHEDULE_KEYS, f"{label}.schedule")
    interval = _duration(schedule.get("interval"), f"{label}.schedule.interval")
    if interval < MIN_INTERVAL_SECONDS:
        raise RoutineSchemaError(
            f"{label}.schedule.interval must be >= {MIN_INTERVAL_SECONDS:.0f}s (got {interval}s)"
        )

    source = _mapping(root.get("source"), f"{label}.source")
    _reject_unknown(source, _SOURCE_KEYS, f"{label}.source")
    kind = _string(source, "kind", f"{label}.source")
    if kind not in _SOURCE_KINDS:
        raise RoutineSchemaError(
            f"{label}.source.kind must be one of {_SOURCE_KINDS}, got {kind!r}"
        )
    if kind == "taskwarrior":
        source_filter = _string(source, "filter", f"{label}.source")
        if "idle_threshold" in source:
            raise RoutineSchemaError(
                f"{label}.source.idle_threshold is only valid for pac-journal"
            )
        source_idle_threshold = DEFAULT_IDLE_THRESHOLD_SECONDS
    else:
        if "filter" in source:
            raise RoutineSchemaError(f"{label}.source.filter is only valid for taskwarrior")
        source_filter = ""
        source_idle_threshold = _duration(
            source.get("idle_threshold", DEFAULT_IDLE_THRESHOLD_SECONDS),
            f"{label}.source.idle_threshold",
        )
        if source_idle_threshold <= 0:
            raise RoutineSchemaError(f"{label}.source.idle_threshold must be > 0")

    policy = _mapping(root.get("policy"), f"{label}.policy")
    _reject_unknown(policy, _POLICY_KEYS, f"{label}.policy")
    routes_raw = policy.get("routes")
    if not isinstance(routes_raw, list) or not routes_raw:
        raise RoutineSchemaError(f"{label}.policy.routes must be a non-empty list")
    routes: list[RouteRule] = []
    for index, item in enumerate(routes_raw):
        item_label = f"{label}.policy.routes[{index}]"
        route = _mapping(item, item_label)
        _reject_unknown(route, _ROUTE_KEYS, item_label)
        tag = _string(route, "tag", item_label)
        target_from = route.get("target_from")
        target = route.get("target")
        escalate_to = route.get("escalate_to")
        kinds = [
            k
            for k, v in (("target_from", target_from), ("target", target), ("escalate_to", escalate_to))
            if v is not None
        ]
        if len(kinds) != 1:
            raise RoutineSchemaError(
                f"{item_label} must set exactly one of target_from/target/escalate_to, got {kinds}"
            )
        if target_from is not None:
            if target_from != "tagarg":
                raise RoutineSchemaError(f"{item_label}.target_from must be 'tagarg'")
            routes.append(RouteRule(tag=tag, kind="target", value="tagarg"))
        elif target is not None:
            if target != "self":
                raise RoutineSchemaError(f"{item_label}.target must be 'self'")
            routes.append(RouteRule(tag=tag, kind="self", value="self"))
        else:
            if not isinstance(escalate_to, str) or not escalate_to.strip():
                raise RoutineSchemaError(f"{item_label}.escalate_to must be a non-empty string")
            address_error = delivery_address_error(escalate_to)
            if address_error is not None:
                raise RoutineSchemaError(f"{item_label}.escalate_to {address_error}")
            routes.append(RouteRule(tag=tag, kind="escalate", value=escalate_to.strip()))

    default_route = policy.get("default", "escalate")
    if default_route not in ("escalate", "self"):
        raise RoutineSchemaError(
            f"{label}.policy.default must be 'escalate' or 'self', got {default_route!r}"
        )
    task_template = _string(policy, "task_template", f"{label}.policy")
    _check_vars(task_template, f"{label}.policy.task_template")

    limits_raw = root.get("limits")
    limits = RoutineLimits()
    if limits_raw is not None:
        limits_map = _mapping(limits_raw, f"{label}.limits")
        _reject_unknown(limits_map, _LIMITS_KEYS, f"{label}.limits")
        max_in_flight = limits_map.get("max_in_flight", 3)
        if (
            isinstance(max_in_flight, bool)
            or not isinstance(max_in_flight, int)
            or not 1 <= max_in_flight <= 100
        ):
            raise RoutineSchemaError(f"{label}.limits.max_in_flight must be an integer in 1..100")
        breaker_raw = limits_map.get("circuit_breaker")
        breaker = CircuitBreaker()
        if breaker_raw is not None:
            breaker_map = _mapping(breaker_raw, f"{label}.limits.circuit_breaker")
            _reject_unknown(breaker_map, _BREAKER_KEYS, f"{label}.limits.circuit_breaker")
            window = breaker_map.get("window_runs", 5)
            ratio = breaker_map.get("escalate_ratio", 1.0)
            action = breaker_map.get("action", "pause+alarm")
            if isinstance(window, bool) or not isinstance(window, int) or not 2 <= window <= 100:
                raise RoutineSchemaError(
                    f"{label}.limits.circuit_breaker.window_runs must be an integer in 2..100"
                )
            if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 < float(ratio) <= 1:
                raise RoutineSchemaError(
                    f"{label}.limits.circuit_breaker.escalate_ratio must be in (0, 1]"
                )
            if action not in _BREAKER_ACTIONS:
                raise RoutineSchemaError(
                    f"{label}.limits.circuit_breaker.action must be one of {_BREAKER_ACTIONS}"
                )
            breaker = CircuitBreaker(window, float(ratio), action)
        limits = RoutineLimits(max_in_flight, breaker)

    timeout_raw = root.get("on_task_timeout")
    if timeout_raw is None:
        raise RoutineSchemaError(f"{label}.on_task_timeout is required (no silent default)")
    timeout_map = _mapping(timeout_raw, f"{label}.on_task_timeout")
    _reject_unknown(timeout_map, _ON_TIMEOUT_KEYS, f"{label}.on_task_timeout")
    action = timeout_map.get("action", "escalate")
    if action not in _TIMEOUT_ACTIONS:
        raise RoutineSchemaError(
            f"{label}.on_task_timeout.action must be one of {_TIMEOUT_ACTIONS}"
        )
    escalate_to = timeout_map.get("escalate_to")
    if not isinstance(escalate_to, str) or not escalate_to.strip():
        raise RoutineSchemaError(f"{label}.on_task_timeout.escalate_to is required")
    address_error = delivery_address_error(escalate_to)
    if address_error is not None:
        raise RoutineSchemaError(f"{label}.on_task_timeout.escalate_to {address_error}")

    return RoutineSpec(
        name=name,
        interval_seconds=interval,
        source_kind=kind,
        source_filter=source_filter,
        source_idle_threshold_seconds=source_idle_threshold,
        routes=tuple(routes),
        default_route=default_route,
        task_template=task_template,
        limits=limits,
        escalate_to=escalate_to.strip(),
        produces=produces,
    )


def load_routine(path: Path) -> RoutineSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RoutineSchemaError(f"cannot read {path}: {error}") from error
    return load_routine_text(text, label=str(path))
