"""§4 success path and dry-run of ``AgentTeleportService.run``."""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.teleport.protocol import (
    CommitRequest,
    PrepareRequest,
    ReceiveRequest,
)

from .fakes import (
    AGENT,
    ARGS,
    MANIFEST_DIGEST,
    OPERATION_ID,
    OWNER,
    PUBLIC_KEY,
    SESSION_REF,
    SOURCE_NODE,
    SOURCE_URI,
    STREAM_SHA256,
    STREAM_TOTAL,
    TARGET_NODE,
    TARGET_URI,
    rig,
)

SUCCESS_ORDER = [
    "describe",
    "require_resumable",
    "prepare",
    "quiesce",
    "export",
    "send",
    "receive",
    "remove",
    "commit",
]


def _prepare(dry_run: bool) -> PrepareRequest:
    return PrepareRequest(
        operation_id=OPERATION_ID,
        source_node=SOURCE_NODE,
        target_node=TARGET_NODE,
        owner=OWNER,
        agent=AGENT,
        source_agent_uri=SOURCE_URI,
        harness="claude",
        routines=(),
        dry_run=dry_run,
    )


def test_dry_run_stops_after_prepare_and_returns_the_plan(tmp_path: Path) -> None:
    r = rig(tmp_path)
    result = r.run(dry_run=True)
    assert result == {
        "ok": True,
        "operationId": OPERATION_ID,
        "dryRun": True,
        "agent": AGENT,
        "from": {"node": SOURCE_NODE, "uri": SOURCE_URI},
        "to": {"node": TARGET_NODE, "uri": None},
        "bytes": 0,
        "routines": [],
    }
    assert r.names() == ["describe", "require_resumable", "prepare"]
    assert r.events[-1] == ("prepare", TARGET_NODE, _prepare(True))
    assert r.leftovers() == []
    assert [c for c in r.routines.calls if c[0] != "executed_by"] == []


def test_success_runs_every_step_in_order(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.run()
    assert r.names() == SUCCESS_ORDER


def test_success_result_names_both_ends_and_a_clean_commit(tmp_path: Path) -> None:
    r = rig(tmp_path)
    assert r.run() == {
        "ok": True,
        "operationId": OPERATION_ID,
        "dryRun": False,
        "agent": AGENT,
        "from": {"node": SOURCE_NODE, "uri": SOURCE_URI},
        "to": {"node": TARGET_NODE, "uri": TARGET_URI},
        "bytes": STREAM_TOTAL,
        "routines": [],
        "commit": {"ok": True, "pausedRoutines": []},
    }


def test_success_sends_the_expected_messages(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.run()
    by_name = {event[0]: event for event in r.events}
    assert by_name["prepare"] == ("prepare", TARGET_NODE, _prepare(False))
    export = by_name["export"]
    assert export[1] == AGENT
    assert export[2] == PUBLIC_KEY
    workdir = export[3]
    assert r.workdir_root in workdir.parents
    assert by_name["send"] == ("send", TARGET_NODE, OPERATION_ID, workdir / "bundle.tar")
    assert by_name["receive"] == (
        "receive",
        TARGET_NODE,
        ReceiveRequest(
            operation_id=OPERATION_ID,
            manifest_digest=MANIFEST_DIGEST,
            stream_sha256=STREAM_SHA256,
            harness="claude",
            session_ref=SESSION_REF,
            routines=(),
            args=ARGS,
            strict_timeout_seconds=60.0,
        ),
    )
    assert by_name["commit"] == ("commit", TARGET_NODE, CommitRequest(OPERATION_ID))
    assert "abort" not in by_name and "restart" not in by_name


def test_success_removes_the_export_directory(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.run()
    assert r.host.workdirs and not r.host.workdirs[0].exists()
    assert r.leftovers() == []


def test_each_run_gets_a_fresh_operation_id(tmp_path: Path) -> None:
    ids = iter(["op-a", "op-b"])
    r = rig(tmp_path, ids=lambda: next(ids))
    assert r.run(dry_run=True)["operationId"] == "op-a"
    assert r.run(dry_run=True)["operationId"] == "op-b"


def test_dry_run_and_run_share_one_entry_point(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.run(dry_run=True)
    r.run()
    prepares = [event[2] for event in r.events if event[0] == "prepare"]
    assert [p.dry_run for p in prepares] == [True, False]
