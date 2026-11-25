# Decision record (file header; no inline comments by repository rule):
# - Agent teleport slice 1 pins four internal routine capabilities on
#   RoutineFacade (docs/design/agent-teleport-acceptance.md, 切片 1). These
#   fakes are shared by the slice's facade tests so each file stays small.
# - FakePac records every start and every rearm_boundary call: resume() is
#   the only facade path that asks for rearm_boundary, so an empty record
#   proves "no auto-resume" and an empty start record proves "no dispatch".
# - Graph state is flipped by the test under a lock; the routine registry
#   settles an in_flight row only on that authoritative pac.status answer.
"""Shared fakes and YAML builders for the agent-teleport routine tests."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from pathlib import Path

import yaml

from hyprial.biz.impl.routine.execution.service import RoutineService

OWNER = "user:op"
ALPHA = "agent:o:n:alpha"
BETA = "agent:o:n:beta"
ALPHA_ALIAS = "agent:o:n:alpha-alias"
START_MS = 1_793_710_000_000


class Clock:
    def __init__(self) -> None:
        self.now_ms = START_MS

    def __call__(self) -> int:
        return self.now_ms


class Alarm:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def escalate(self, **fields: object) -> None:
        self.calls.append(dict(fields))


class FakePac:
    """One graph per (routine, task); the test decides when a graph settles."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.states: dict[str, str] = {}
        self.graphs: dict[tuple[str, str], str] = {}
        self.starts: list[str] = []
        self.rearm_calls: list[str] = []

    def start_idempotent(self, *, routine_name: str, task_uuid: str, **_: object):
        with self.lock:
            self.starts.append(routine_name)
            graph_id = self.graphs.get((routine_name, task_uuid))
            if graph_id is None:
                graph_id = f"routine-{routine_name}-{len(self.graphs):08x}"
                self.graphs[(routine_name, task_uuid)] = graph_id
                self.states[graph_id] = "running"
            return {"graphId": graph_id, "state": self.states[graph_id]}

    def status(self, *, graph_id: str) -> dict[str, object]:
        with self.lock:
            state = self.states.get(graph_id)
        if state is None:
            raise RuntimeError(f"routine graph {graph_id} is missing")
        return {"graphId": graph_id, "state": state}

    def close(self, *, graph_id: str, actor: str) -> None:
        del graph_id, actor

    def rearm_boundary(self, *, routine_name: str) -> int:
        with self.lock:
            self.rearm_calls.append(routine_name)
        return 0

    def settle(self, routine_name: str) -> None:
        with self.lock:
            for (routine, _task), graph_id in self.graphs.items():
                if routine == routine_name:
                    self.states[graph_id] = "done"


class SourceRecorder:
    def __init__(self) -> None:
        self.filters: list[str] = []

    def __call__(self, source_filter: str) -> list:
        self.filters.append(source_filter)
        return []


def make_service(
    tmp_path: Path,
    *,
    pac: FakePac | None = None,
    clock: Clock | None = None,
    source: Callable[[str], list] | None = None,
    binding_key: Callable[[str], Hashable] | None = None,
) -> RoutineService:
    return RoutineService(
        pac=pac or FakePac(),
        alarm=Alarm(),
        state_dir=tmp_path / "state",
        clock_ms=clock or Clock(),
        source_query=source or SourceRecorder(),
        binding_key=binding_key,
    )


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def scheduled_yaml(
    name: str,
    *,
    actor: str | None = None,
    escalate_to: str = OWNER,
    interval: str = "60s",
) -> str:
    document: dict[str, object] = {
        "version": 2,
        "mode": "scheduled",
        "name": name,
        "schedule": {"interval": interval},
    }
    if actor is not None:
        document["actor"] = actor
    document["task"] = "Heartbeat and pick up work."
    document["on_task_timeout"] = {"action": "escalate", "escalate_to": escalate_to}
    return yaml.safe_dump(document, sort_keys=False)


def produced_source_yaml(
    name: str, *, produces: str, escalate_to: str = OWNER
) -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "name": name,
            "schedule": {"interval": "15m"},
            "produces": produces,
            "source": {"kind": "taskwarrior", "filter": "+selfdrive"},
            "policy": {
                "routes": [
                    {"tag": "route:self", "target": "self"},
                    {"tag": "route:human", "escalate_to": escalate_to},
                ],
                "default": "escalate",
                "task_template": "{{task.description}} {{nonce}}",
            },
            "limits": {
                "max_in_flight": 3,
                "circuit_breaker": {
                    "window_runs": 5,
                    "escalate_ratio": 1.0,
                    "action": "pause+alarm",
                },
            },
            "on_task_timeout": {"action": "escalate", "escalate_to": escalate_to},
        },
        sort_keys=False,
    )


def dispatch_once(service: RoutineService, clock: Clock, name: str) -> None:
    """Drive one due timer tick until ``name`` holds an in_flight row."""

    clock.now_ms += 120_000
    service.submit_timer()
    wait_for(lambda: service.status(name=name)["inFlight"] != [])
