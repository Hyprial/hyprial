"""The one param reader: a pure resolver over a snapshot (plan §2.0, S7a).

Decision notes (phase A, S7a):

- ``resolve_param(snapshot, spec, subject)`` is the only way code reads a
  cell param (S3's ``policy_int`` is gone).  It is pure over its inputs: no
  runtime, no OrgFS, no clock, so it is safe on any thread and in any slice.
  Its one shared read is the process ``PARAM_REGISTRY``: a spec that is not
  declared, or differs from its declaration, raises ``ParamSpecError``,
  with or without a snapshot, so a misdeclared reader fails in its tests.
- ``subject=None`` applies every trusted source (for ``min``, the most
  conservative value across all agents).  A per-agent decision passes the
  agent's URI, ``a.`` id or agent ``c.`` id, never None.
- A None or empty snapshot (no cell runtime composed, plan wave 1 open
  items) resolves to the spec default; callers never branch on availability.
- Sources applying to the subject are read in snapshot order; a value of the
  wrong shape is ignored (``ignored`` reason ``type``), never coerced.
- ``min`` clamps the combined value; ``unique`` with disagreeing sources
  falls back to the default and flags ``conflict``; ``any_false`` is False
  once any source says False; ``collect`` returns every value as a tuple.
- Values and defaults are returned as fresh plain JSON (``thaw_json`` or a
  deep copy), so a caller cannot mutate the snapshot or the spec.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.cells.model import thaw_json
from hyprial.daemon.impl.cells.params.snapshot import CellParamSnapshot
from hyprial.daemon.impl.cells.params.spec import PARAM_REGISTRY, ParamSpec

IgnoreReason = str


@dataclass(frozen=True, slots=True)
class ParamResolution:
    """A resolved value and how it was reached."""

    key: str
    value: Any
    defaulted: bool
    sources: tuple[str, ...] = ()
    ignored: tuple[tuple[str, IgnoreReason], ...] = ()
    conflict: bool = False
    clamped: bool = False

    def __hash__(self) -> int:
        return hash((self.key, self.sources, self.defaulted))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _default(spec: ParamSpec) -> Any:
    return copy.deepcopy(spec.default)


def _combine(spec: ParamSpec, values: list[Any]) -> tuple[Any, bool, bool]:
    if spec.combine == "collect":
        return tuple(values), False, False
    if spec.combine == "min":
        smallest = min(values)
        bounded = spec.clamp(smallest)
        return bounded, False, bounded != smallest
    if spec.combine == "any_false":
        return all(values), False, False
    distinct = {_canonical(value) for value in values}
    if len(distinct) == 1:
        return values[0], False, False
    return _default(spec), True, False


def resolve_param_detail(
    snapshot: CellParamSnapshot | None, spec: ParamSpec, subject: str | None = None
) -> ParamResolution:
    """Resolve *spec* for *subject* from *snapshot*, with provenance."""

    PARAM_REGISTRY.require(spec)
    if snapshot is None:
        return ParamResolution(spec.key, _default(spec), defaulted=True)
    contributed: list[str] = []
    ignored: list[tuple[str, IgnoreReason]] = []
    values: list[Any] = []
    for source in snapshot.sources_for(subject):
        if spec.key not in source.params:
            continue
        raw = source.params[spec.key]
        if not spec.accepts(raw):
            ignored.append((source.cell_id, "type"))
            continue
        contributed.append(source.cell_id)
        values.append(thaw_json(raw))
    if not values:
        return ParamResolution(
            spec.key, _default(spec), defaulted=True, ignored=tuple(ignored)
        )
    value, conflict, clamped = _combine(spec, values)
    return ParamResolution(
        spec.key,
        value,
        defaulted=conflict,
        sources=tuple(contributed),
        ignored=tuple(ignored),
        conflict=conflict,
        clamped=clamped,
    )


def resolve_param(
    snapshot: CellParamSnapshot | None, spec: ParamSpec, subject: str | None = None
) -> Any:
    """The value of *spec* for *subject*; the default without sources."""

    return resolve_param_detail(snapshot, spec, subject).value


__all__ = ["ParamResolution", "resolve_param", "resolve_param_detail"]
