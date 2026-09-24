"""Task sources for self-drive routines.

PAC journal reads PAC v2 snapshots through its public subscription outlet. It
does not inspect PAC tables or infer ownership: only active assignments owned
by the routine's declared coordinator become ``route:self`` wake tasks.

Retry discipline (hard constraint, 2026-08-21 incident): every retry has a
cap, and errors that can NEVER succeed are never retried.  A missing `task`
binary is permanent — zero retries, immediate pause+alarm.  Transient query
failures count toward a consecutive-error cap (default 10); hitting it pauses
the routine and alarms.  Nothing here ever retries forever.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from hyprial.uri import parse_agent_uri

#: Hard cap on consecutive transient source errors before pause+alarm (Allen: 默认 10 次后报失败).
SOURCE_ERROR_CAP = 10
_QUERY_TIMEOUT_SECONDS = 10.0
_QUERY_OUTPUT_CAP_BYTES = 1 << 20


class SourcePermanentError(RuntimeError):
    """The query can never succeed (missing binary, bad invocation). Zero retries."""


class SourceTransientError(RuntimeError):
    """The query failed but may succeed later; counts toward the error cap."""


@dataclass(frozen=True, slots=True)
class SourceTask:
    uuid: str
    description: str
    tags: tuple[str, ...]


class PacJournalV2(Protocol):
    """The public PAC v2 snapshot outlet consumed by routines."""

    def snapshots(self) -> list[dict[str, object]]: ...


class FilePacJournal:
    """Adapter over PAC's store/subscription boundaries, not its SQL tables."""

    def __init__(self, database: Path) -> None:
        self.database = Path(database)

    def snapshots(self) -> list[dict[str, object]]:
        if not self.database.exists():
            return []
        from hyprial.pac.store import PacGraphStore
        from hyprial.pac.subscription import snapshot

        store = PacGraphStore(self.database)
        try:
            graph_ids = store.graph_ids()
        finally:
            store.close()
        return [snapshot(self.database, graph_id) for graph_id in graph_ids]


def _record(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SourceTransientError(f"{label} is not an object")
    return value


def _records(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise SourceTransientError(f"{label} is not an array")
    return [_record(item, f"{label}[{index}]") for index, item in enumerate(value)]


def query_pac_journal(
    journal: PacJournalV2,
    *,
    coordinator: str,
    now_ms: int,
    idle_threshold_seconds: float,
) -> list[SourceTask]:
    """Project idle PAC v2 coordinator assignments into deterministic wake tasks."""

    principal = parse_agent_uri(coordinator)
    if principal is None:
        raise SourcePermanentError("routine coordinator is not a canonical agent URI")
    threshold_ms = int(idle_threshold_seconds * 1000)
    tasks: list[SourceTask] = []
    for index, raw_snapshot in enumerate(journal.snapshots()):
        item = _record(raw_snapshot, f"pac.snapshot[{index}]")
        graph_id = item.get("graphId")
        if not isinstance(graph_id, str) or not graph_id:
            raise SourceTransientError(f"pac.snapshot[{index}].graphId is invalid")
        if item.get("active") is None or item.get("closed") is not None:
            continue
        assignments = _records(item.get("assignments"), f"pac.snapshot({graph_id}).assignments")
        for target_index, assignment in enumerate(assignments):
            if assignment.get("owner") != coordinator or assignment.get("blockedOn") != "agent":
                continue
            requests = _records(assignment.get("requests"), f"pac.snapshot({graph_id}).assignments[{target_index}].requests")
            if not requests:
                continue
            request_times = [request.get("at") for request in requests]
            if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in request_times):
                raise SourceTransientError(f"pac.snapshot({graph_id}).assignments[{target_index}].requests.at is invalid")
            last_at = max(request_times)
            idle_ms = now_ms - last_at
            if idle_ms <= threshold_ms:
                continue
            activation_id = assignment.get("activationId")
            if not isinstance(activation_id, str) or not activation_id:
                raise SourceTransientError(f"pac.snapshot({graph_id}).assignments[{target_index}].activationId is invalid")
            tasks.append(SourceTask(
                uuid=f"pac-journal:{activation_id}",
                description=f"PAC graph {graph_id} assignment {activation_id} owned by {coordinator} idle for {_format_idle(idle_ms)}",
                tags=("route:self",),
            ))
    return tasks


def _format_idle(idle_ms: int) -> str:
    total_seconds = max(0, idle_ms // 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def query_taskwarrior(filter_expr: str, *, task_bin: str = "task") -> list[SourceTask]:
    """Run `task export` with the given filter and parse the JSON array.

    Bounded on every axis: timeout, output cap, and parse validation.  A
    missing binary raises :class:`SourcePermanentError` (never retried); any
    other failure raises :class:`SourceTransientError`.
    """
    try:
        completed = subprocess.run(
            [task_bin, *filter_expr.split(), "export"],
            capture_output=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as error:
        raise SourcePermanentError(f"taskwarrior binary not found: {task_bin}") from error
    except subprocess.TimeoutExpired as error:
        raise SourceTransientError(
            f"task export timed out after {_QUERY_TIMEOUT_SECONDS}s"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace")[:300]
        raise SourceTransientError(f"task export exited {completed.returncode}: {detail}")
    raw = completed.stdout[: _QUERY_OUTPUT_CAP_BYTES]
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SourceTransientError(f"task export returned invalid JSON: {error}") from error
    if not isinstance(document, list):
        raise SourceTransientError("task export did not return a JSON array")
    tasks: list[SourceTask] = []
    for index, item in enumerate(document):
        if not isinstance(item, dict):
            raise SourceTransientError(f"task export item {index} is not an object")
        uuid = item.get("uuid")
        description = item.get("description")
        if not isinstance(uuid, str) or not uuid:
            raise SourceTransientError(f"task export item {index} has no uuid")
        tags = item.get("tags") or []
        tasks.append(
            SourceTask(
                uuid=uuid,
                description=description if isinstance(description, str) else "",
                tags=tuple(t for t in tags if isinstance(t, str)),
            )
        )
    return tasks


def route_tag(tags: tuple[str, ...], prefix: str) -> str | None:
    """The tag starting with `prefix` (e.g. 'route:worker:'), else None."""
    return next((t for t in tags if t.startswith(prefix)), None)


def route_decision(
    tags: tuple[str, ...],
    routes: tuple,
    default_route: Literal["escalate", "self"],
) -> tuple[Literal["target", "self", "escalate"], str | None]:
    """The routing-table verdict for one task — a lookup, never a judgment.

    Returns (kind, value): ("target", worker-name) | ("self", None) |
    ("escalate", escalate_to-or-None-meaning-default).
    """
    tags_set = set(tags)
    for rule in routes:
        if rule.kind == "target":
            hit = route_tag(tags, rule.tag + ":")
            if hit is not None:
                worker = hit[len(rule.tag) + 1 :]
                if worker:
                    return ("target", worker)
        elif rule.tag in tags_set:
            if rule.kind == "self":
                return ("self", None)
            return ("escalate", rule.value)
    if default_route == "self":
        return ("self", None)
    return ("escalate", None)
