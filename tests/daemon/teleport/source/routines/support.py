"""Fakes for the slice 6 (routine) source-side teleport tests.

Behaviour contract pinned by this package (docs/design/agent-teleport-protocol.md
§2, §4 rows 0-8, §6 "切片 6"; agent-teleport-handoff.md §7).  Decisions on
details the plan leaves open are marked D and recorded only here.

Selection
- The migrating routines are exactly what ``SourceRoutines.executed_by``
  returns for the agent's URI, in that order (the port already filters by
  executing identity; references that only appear as escalation targets are
  the port's business).  ``select_routines(listed)`` returns them all; disabled
  routines travel too (D: ``select_routines`` returns ``tuple(listed)``).
- prepare's ``routines`` carries every migrating name, enabled or not, in the
  listed order; a dry run still carries them and touches no routine.
- The result's ``routines`` lists ``{"name", "enabled"}`` with the enabled
  state seen at step 0.

Step 2 (between prepare and quiesce)
- After a successful (non dry-run) prepare and before ``host.quiesce``: pause
  every originally-enabled routine (listed order), then call
  ``wait_drained(<all migrating names>, timeout)`` once (D: disabled routines
  are waited for too, because a routine disabled earlier may still hold
  in-flight rows that the source would otherwise delete with it).  With no
  migrating routine neither pause nor ``wait_drained`` is called.
- D: the drain timeout is ``AgentTeleportService(..., drain_timeout_seconds=
  ROUTINE_DRAIN_TIMEOUT_SECONDS)``, the module constant
  ``ROUTINE_DRAIN_TIMEOUT_SECONDS = 120.0`` in
  ``hyprial.daemon.impl.teleport.source.service`` (§2 "routine 在途等待上限缺省
  120 s"); the keyword overrides it per service.
- D: then ``export(name)`` for every migrating routine (listed order), still
  before ``host.quiesce``, so an unreadable definition fails before the home
  is touched.
- ``wait_drained`` returning a non-empty list, or raising, is
  ``TELEPORT_IN_FLIGHT_TIMEOUT`` (D for the raising case).  A failing
  ``pause`` or ``export`` is ``TELEPORT_SOURCE_NOT_QUIESCED`` (D).  In all of
  them: resume exactly the routines this run paused, send one abort, never
  call ``quiesce``, ``export_stream``, ``restart``, ``send`` or ``receive``,
  ``data`` is ``None``, and the export work directory is gone.  The timeout
  message names the routines still busy.

Later failures (§4 rows 3-6 with ``ok:false``)
- Unchanged codes from slice 5; additionally every routine this run paused is
  resumed after ``host.restart`` (D: the agent is back before its routines can
  dispatch to it).  Disabled routines are never resumed.  A resume that
  raises never changes the code; its routine name is appended to the message.
- ``TELEPORT_UNRESOLVED``: nothing is resumed or removed; ``pausedRoutines``
  lists the routines this run paused, in listed order.

Receive, cleanup and commit
- receive's ``routines`` is one ``RoutinePayload(name, yaml, enabled)`` per
  migrating routine in listed order: ``yaml`` from ``export``, ``enabled`` the
  step-0 state (D: not export's ``enabled``, which reads ``False`` after the
  pause).
- Step 7: ``routines.remove(name)`` for every migrating routine (listed
  order) BEFORE ``host.remove(agent)`` (D); the first failure stops the
  cleanup (the agent is then not removed) and is
  ``TELEPORT_SOURCE_CLEANUP_FAILED`` without commit, abort, restart or resume.
- D: ``pausedRoutines`` in ``TELEPORT_SOURCE_CLEANUP_FAILED`` (and in the
  result's ``commit`` when commit gets no reply) lists the receive reply's
  routines whose ``restoreEnabled`` is true: the target routines that stay
  paused although they were enabled on the source.
- D: a ``RoutineOutcome``'s ``enabled`` is the state the target meant to
  restore (``restoreEnabled``); ``resumed`` is true only for a routine
  actually resumed.  The result's ``commit.pausedRoutines`` lists outcomes
  with ``enabled`` true and ``resumed`` false; a disabled routine
  (``enabled=False, resumed=False``) is not a warning.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.teleport.protocol import (
    CommitReply,
    CommitRequest,
    ReceiveReply,
    ReceiveRequest,
    RoutineOutcome,
    RoutineRestore,
)

from ..fakes import (
    AGENT,
    OPERATION_ID,
    SOURCE_URI,
    TARGET_NODE,
    TARGET_URI,
    FakeHost,
    FakeLink,
    make_agent,
    source,
)

NIGHTLY = "nightly"
WEEKLY = "weekly"
HOURLY = "hourly"
LISTED = ((NIGHTLY, True), (WEEKLY, False), (HOURLY, True))
ENABLED = [NIGHTLY, HOURLY]
ALL = [NIGHTLY, WEEKLY, HOURLY]
DEFAULT_DRAIN_TIMEOUT = 120.0
ROUTINE_EVENTS = frozenset(
    {
        "routine_executed_by",
        "routine_pause",
        "routine_resume",
        "routine_wait",
        "routine_export",
        "routine_remove",
    }
)


def yaml_for(name: str) -> str:
    return f"version: 2\nname: {name}\nactor: {SOURCE_URI}\ntask: work\n"


@dataclass
class RecordingRoutines:
    """A ``SourceRoutines`` sharing the run's event log and tracking state."""

    events: list[tuple]
    listed: Sequence[tuple[str, bool]] = LISTED
    busy: list[str] = field(default_factory=list)
    failures: dict[str, BaseException] = field(default_factory=dict)
    enabled: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.enabled = {name: flag for name, flag in self.listed}

    def _maybe_fail(self, key: str) -> None:
        if key in self.failures:
            raise self.failures[key]

    def executed_by(self, agent_uri: str) -> list:
        self.events.append(("routine_executed_by", agent_uri))
        return [source().RoutineRef(name, flag) for name, flag in self.listed]

    def export(self, name: str) -> Any:
        self.events.append(("routine_export", name))
        self._maybe_fail(f"export:{name}")
        return source().RoutineExport(name, yaml_for(name), self.enabled[name])

    def pause(self, name: str) -> None:
        self.events.append(("routine_pause", name))
        self._maybe_fail(f"pause:{name}")
        self.enabled[name] = False

    def resume(self, name: str) -> None:
        self.events.append(("routine_resume", name))
        self._maybe_fail(f"resume:{name}")
        self.enabled[name] = True

    def wait_drained(self, names: Sequence[str], timeout: float) -> list[str]:
        self.events.append(("routine_wait", tuple(names), timeout))
        self._maybe_fail("wait")
        return list(self.busy)

    def remove(self, name: str) -> None:
        self.events.append(("routine_remove", name))
        self._maybe_fail(f"remove:{name}")
        del self.enabled[name]


@dataclass
class RoutineLink(FakeLink):
    """A link whose target echoes the routines it was sent."""

    commit_outcomes: tuple | None = None

    def receive(self, target_node: str, request: ReceiveRequest) -> ReceiveReply:
        self.events.append(("receive", target_node, request))
        self._maybe_fail("receive")
        return ReceiveReply(
            operation_id=request.operation_id,
            agent_uri=TARGET_URI,
            session_ref=request.session_ref,
            routines=tuple(
                RoutineRestore(name=item.name, restore_enabled=item.enabled)
                for item in request.routines
            ),
        )

    def commit(self, target_node: str, request: CommitRequest) -> CommitReply:
        self.events.append(("commit", target_node, request))
        self._maybe_fail("commit")
        outcomes = self.commit_outcomes
        if outcomes is None:
            outcomes = tuple(
                RoutineOutcome(name=name, enabled=flag, resumed=flag)
                for name, flag in LISTED
            )
        return CommitReply(operation_id=request.operation_id, routines=outcomes)


@dataclass
class RoutineRig:
    events: list[tuple]
    host: FakeHost
    routines: RecordingRoutines
    link: RoutineLink
    workdir_root: Path
    service: Any

    def names(self) -> list[str]:
        return [event[0] for event in self.events]

    def index(self, *event: Any) -> int:
        for position, recorded in enumerate(self.events):
            if recorded[: len(event)] == event:
                return position
        raise AssertionError(f"{event!r} not in {self.names()}")

    def routine_calls(self, kind: str) -> list[str]:
        return [event[1] for event in self.events if event[0] == kind]

    def requests(self, op: str) -> list:
        return [event[2] for event in self.events if event[0] == op]

    def run(self, *, dry_run: bool = False) -> dict[str, Any]:
        return self.service.run(AGENT, to=TARGET_NODE, dry_run=dry_run)

    def leftovers(self) -> list[Path]:
        return sorted(self.workdir_root.iterdir())


def routine_rig(tmp_path: Path, **service_kwargs: Any) -> RoutineRig:
    events: list[tuple] = []
    home = tmp_path / "agents" / AGENT
    (home / "workspace").mkdir(parents=True, exist_ok=True)
    agent = make_agent(home)
    host = FakeHost(events=events, agents={agent.name: agent})
    routines = RecordingRoutines(events=events)
    link = RoutineLink(events=events)
    workdir_root = tmp_path / "teleport-work"
    workdir_root.mkdir()
    service = source().AgentTeleportService(
        host,
        routines,
        link,
        workdir_root=workdir_root,
        new_operation_id=lambda: OPERATION_ID,
        **service_kwargs,
    )
    return RoutineRig(events, host, routines, link, workdir_root, service)


def service_module() -> Any:
    return importlib.import_module("hyprial.daemon.impl.teleport.source.service")


def original_state() -> dict[str, bool]:
    return dict(LISTED)
