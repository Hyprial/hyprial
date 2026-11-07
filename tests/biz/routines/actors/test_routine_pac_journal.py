"""Selfdrive PAC source consumes only the public v2 snapshot outlet."""

from __future__ import annotations
from dataclasses import dataclass
import pytest
from hyprial.biz.impl.routine.execution.source import SourcePermanentError, query_pac_journal

NOW_MS = 2_000_000
COORDINATOR = "agent:test:isolated:coordinator"


@dataclass
class Journal:
    rows: list[dict[str, object]]

    def snapshots(self) -> list[dict[str, object]]:
        return self.rows


def request(at: int) -> dict[str, object]:
    return {"at": at}


def assignment(
    *, owner: str = COORDINATOR, blocked_on: str = "agent", at: int
) -> dict[str, object]:
    return {
        "activationId": "g:work:1",
        "nodeId": "work",
        "owner": owner,
        "blockedOn": blocked_on,
        "requests": [request(at)],
    }


def snap(*assignments: dict[str, object], active: bool = True, closed: bool = False):
    return {
        "graphId": "g",
        "active": {"at": 1} if active else None,
        "closed": {"at": 2} if closed else None,
        "assignments": list(assignments),
    }


def query(rows: list[dict[str, object]]):
    return query_pac_journal(
        Journal(rows),
        coordinator=COORDINATOR,
        now_ms=NOW_MS,
        idle_threshold_seconds=30 * 60,
    )


def test_pac_journal_projects_only_idle_owned_actor_assignment() -> None:
    tasks = query(
        [
            snap(
                assignment(at=NOW_MS - 31 * 60_000),
                assignment(owner="other", at=NOW_MS - 40 * 60_000),
                assignment(blocked_on="human", at=NOW_MS - 40 * 60_000),
            ),
            snap(assignment(at=0), active=False),
            snap(assignment(at=0), closed=True),
        ]
    )
    assert len(tasks) == 1
    assert tasks[0].uuid == "pac-journal:g:work:1"
    assert tasks[0].tags == ("route:self",)
    assert COORDINATOR in tasks[0].description


def test_pac_journal_threshold_is_strict_and_uses_latest_request() -> None:
    row = assignment(at=NOW_MS - 31 * 60_000)
    row["requests"] = [request(NOW_MS - 31 * 60_000), request(NOW_MS - 30 * 60_000)]
    assert query([snap(row)]) == []


def test_pac_journal_requires_canonical_coordinator() -> None:
    with pytest.raises(SourcePermanentError, match="canonical"):
        query_pac_journal(
            Journal([]), coordinator="short", now_ms=NOW_MS, idle_threshold_seconds=1
        )


def test_pac_journal_rejects_malformed_public_projection() -> None:
    with pytest.raises(Exception, match="requests.at"):
        query([snap(assignment(at=None))])  # type: ignore[arg-type]


def test_managed_workflow_assignments_are_visible_to_the_real_source(tmp_path):
    import yaml
    from hyprial.daemon.impl.pac.graphs.reactor import NullSender
    from hyprial.daemon.impl.pac.graphs.subscription import snapshot
    from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

    service = GraphWorkflowService(
        state_dir=tmp_path,
        owner="test",
        machine="isolated",
        sender=NullSender(),
        start_thread=False,
        clock_ms=lambda: NOW_MS,
    )
    try:
        graph = service.start(
            yaml_text=yaml.safe_dump(
                {
                    "version": 2,
                    "name": "idle",
                    "nodes": [
                        {
                            "id": "work",
                            "owner": COORDINATOR,
                            "role": "dispatch",
                            "task": "Coordinate.",
                        }
                    ],
                }
            ),
            sender="user:test",
        )["graphId"]
        service._tick()
        view = snapshot(service.database, graph)
        import json
        import runpy
        from pathlib import Path
        import jsonschema

        root = Path(__file__).resolve().parents[4]
        schema = json.loads((root / "contract/pac/events/schema.json").read_text())
        jsonschema.validate(view, {"$ref": "#/$defs/snapshot", "$defs": schema["$defs"]})
        client = runpy.run_path(str(root / "contract/pac/events/client.py"))
        consumer = client["Consumer"](view)
        tasks = query_pac_journal(
            Journal([view]),
            coordinator=COORDINATOR,
            now_ms=NOW_MS + 31 * 60_000,
            idle_threshold_seconds=30 * 60,
        )
        assert len(tasks) == 1
        request_id = service.status(run_id=graph)["nodes"][0]["requestId"]
        assert tasks[0].uuid == f"pac-journal:{request_id}"
        assert (
            query_pac_journal(
                Journal([view]),
                coordinator="agent:test:other:coordinator",
                now_ms=NOW_MS + 31 * 60_000,
                idle_threshold_seconds=30 * 60,
            )
            == []
        )
        service.cancel(run_id=graph, actor="user:test")
        from hyprial.daemon.impl.pac.graphs.subscription import events_since

        events = events_since(service.database, graph, view["cursor"])["events"]
        with pytest.raises(client["ResyncRequired"]):
            consumer.apply(events[0])
        assert (
            query_pac_journal(
                Journal([snapshot(service.database, graph)]),
                coordinator=COORDINATOR,
                now_ms=NOW_MS + 31 * 60_000,
                idle_threshold_seconds=30 * 60,
            )
            == []
        )
    finally:
        service.close()
