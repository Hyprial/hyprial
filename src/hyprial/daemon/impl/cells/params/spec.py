"""Declared cell parameters: ``ParamSpec`` and the process registry (plan §1.1, S7a).

Decision notes (phase A, S7a):

- A param is declared once by the module that reads it, as a ``ParamSpec``
  (key, value type, combine mode, default, integer clamp).  Cell data stays
  free-form (``Cell.params``); only a declared spec gives a key meaning.
- Combine modes are a closed set: ``unique`` (every applicable source must
  agree, else the default), ``min`` (smallest integer), ``any_false`` (one
  ``False`` wins, the safe direction for permissive flags) and ``collect``
  (every value, in source order, for readers that merge themselves).
- ``min`` needs ``int``; ``any_false`` needs ``bool``; a clamp needs
  ``min`` (and so ``int``): it bounds the combined value, and no other mode
  applies one, so a clamp elsewhere would be silently ignored.  ``collect`` always defaults to the empty tuple.  The default
  must itself satisfy the spec, so an unavailable runtime resolves to a
  legal value.
- Types are strict JSON shapes: ``int`` refuses booleans and floats,
  ``object`` is a JSON object.  Wrong shapes are ignored at resolve time,
  never coerced.
- The registry refuses a second, different declaration of one key; an equal
  re-declaration (a module imported twice) returns the first spec.  The
  resolver reads only declared specs and refuses one that differs from the
  declaration, so two readers cannot read one key with different modes.
- ``delivery.bodyRetentionMs`` is declared here, not in ``delivery``,
  because delivery must not import cells: it is the single retention key of
  plan Q9 (combine ``min``, clamp 1 h..365 d, default 7 d), and the
  composition side hands delivery the resolved integer.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

ParamType = Literal["int", "bool", "string", "object", "any"]
CombineMode = Literal["unique", "min", "any_false", "collect"]

PARAM_TYPES: frozenset[str] = frozenset({"int", "bool", "string", "object", "any"})
COMBINE_MODES: frozenset[str] = frozenset({"unique", "min", "any_false", "collect"})
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z][A-Za-z0-9_-]*)*")
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


class ParamSpecError(ValueError):
    """An inconsistent spec or a conflicting declaration."""


def _is_json(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


def value_matches(type_: str, value: Any) -> bool:
    """Whether *value* has the JSON shape of *type_* (strict, no coercion)."""

    if type_ == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_ == "bool":
        return isinstance(value, bool)
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "object":
        return isinstance(value, Mapping)
    return type_ == "any"


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """One declared cell parameter."""

    key: str
    type: ParamType
    combine: CombineMode = "unique"
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or _KEY.fullmatch(self.key) is None:
            raise ParamSpecError(f"param key {self.key!r} is not a dotted name")
        if self.type not in PARAM_TYPES:
            raise ParamSpecError(f"unknown param type {self.type!r}")
        if self.combine not in COMBINE_MODES:
            raise ParamSpecError(f"unknown combine mode {self.combine!r}")
        if self.combine == "min" and self.type != "int":
            raise ParamSpecError("combine 'min' needs type 'int'")
        if self.combine == "any_false" and self.type != "bool":
            raise ParamSpecError("combine 'any_false' needs type 'bool'")
        for bound in (self.minimum, self.maximum):
            if bound is None:
                continue
            if self.combine != "min":
                raise ParamSpecError("a clamp needs combine 'min'")
            if not value_matches("int", bound):
                raise ParamSpecError("clamp bounds are integers")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ParamSpecError("clamp minimum exceeds maximum")
        if self.combine == "collect":
            if self.default not in (None, ()):
                raise ParamSpecError("combine 'collect' defaults to ()")
            object.__setattr__(self, "default", ())
            return
        if self.default is None:
            return
        if not value_matches(self.type, self.default) or not _is_json(self.default):
            raise ParamSpecError(f"default of {self.key} is not a {self.type}")
        if self.type == "int" and self.clamp(self.default) != self.default:
            raise ParamSpecError(f"default of {self.key} is outside its clamp")

    def __hash__(self) -> int:
        return hash((self.key, self.type, self.combine, self.minimum, self.maximum))

    def accepts(self, value: Any) -> bool:
        """Whether a source value has this spec's type."""

        return value_matches(self.type, value)

    def clamp(self, value: int) -> int:
        """*value* bounded by the spec's clamp (identity without one)."""

        if self.minimum is not None and value < self.minimum:
            return self.minimum
        if self.maximum is not None and value > self.maximum:
            return self.maximum
        return value


class ParamRegistry:
    """Declared specs by key; one declaration per key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._specs: dict[str, ParamSpec] = {}

    def declare(self, spec: ParamSpec) -> ParamSpec:
        with self._lock:
            held = self._specs.get(spec.key)
            if held is None:
                self._specs[spec.key] = spec
                return spec
        if held == spec:
            return held
        raise ParamSpecError(f"param {spec.key} is already declared differently")

    def get(self, key: str) -> ParamSpec | None:
        with self._lock:
            return self._specs.get(key)

    def specs(self) -> tuple[ParamSpec, ...]:
        with self._lock:
            return tuple(spec for _, spec in sorted(self._specs.items()))

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._specs

    def require(self, spec: ParamSpec) -> ParamSpec:
        """*spec* when it is the declared spec of its key, else an error."""

        held = self.get(spec.key)
        if held is None:
            raise ParamSpecError(f"param {spec.key} is not declared")
        if held != spec:
            raise ParamSpecError(f"param {spec.key} differs from its declaration")
        return held


PARAM_REGISTRY = ParamRegistry()


def declare_param(spec: ParamSpec) -> ParamSpec:
    """Declare *spec* in the process registry; returns the registered spec."""

    return PARAM_REGISTRY.declare(spec)


DELIVERY_BODY_RETENTION_MS = declare_param(
    ParamSpec(
        "delivery.bodyRetentionMs",
        "int",
        combine="min",
        default=7 * DAY_MS,
        minimum=HOUR_MS,
        maximum=365 * DAY_MS,
    )
)


__all__ = [
    "COMBINE_MODES",
    "CombineMode",
    "DELIVERY_BODY_RETENTION_MS",
    "PARAM_REGISTRY",
    "PARAM_TYPES",
    "ParamRegistry",
    "ParamSpec",
    "ParamSpecError",
    "ParamType",
    "declare_param",
    "value_matches",
]
