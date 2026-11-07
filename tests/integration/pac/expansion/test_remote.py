"""Remote expansion custody binds text, digest, attempt, and frame size."""

import json
import sqlite3
from types import MethodType

import pytest
import yaml

from hyprial.daemon.impl.dispatch.remote.expansion import (
    enqueue_outcome_row,
    expansion_outcome_fields,
    validate_expansion_outcome,
)
from hyprial.identity import PAC_EXPANSION_INVALID, PacError

from .support import child_document
from .support import policy_home, repository


def test_remote_expansion_round_trip_and_digest_mismatch_refusal():
    raw = child_document(1)
    fields = expansion_outcome_fields(raw, attempt_no=3)
    assert fields["expansion"] == raw
    assert fields["attemptNo"] == 3
    assert validate_expansion_outcome(fields) == raw

    with pytest.raises(PacError) as caught:
        validate_expansion_outcome({**fields, "expansionDigest": "0" * 64})
    assert caught.value.code == PAC_EXPANSION_INVALID
    assert caught.value.data == {"field": "expansionDigest", "reason": "transport-size"}


def test_remote_expansion_rejects_invalid_attempt_and_oversize_text():
    with pytest.raises(PacError):
        expansion_outcome_fields(child_document(1), attempt_no=-1)
    with pytest.raises(PacError):
        expansion_outcome_fields("x" * 65_537, attempt_no=0)


def test_only_definitive_invalid_result_allows_corrected_attempt():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE remote_workflow_outbox (request_id TEXT PRIMARY KEY,"
        "action TEXT,reason_ref TEXT,result_json TEXT,attempted_at INTEGER,"
        "output_text TEXT,expansion_text TEXT,expansion_digest TEXT,"
        "attempt_no INTEGER NOT NULL DEFAULT 0)"
    )
    first = expansion_outcome_fields(child_document(1), attempt_no=0)
    enqueue_outcome_row(
        db,
        request_id="request-1",
        action="complete",
        reason="evidence:first",
        output_text=None,
        expansion_text=first["expansion"],
        expansion_digest_value=first["expansionDigest"],
    )
    db.execute(
        "UPDATE remote_workflow_outbox SET result_json=? WHERE request_id=?",
        (
            json.dumps(
                {
                    "returnState": "rejected",
                    "error": {"code": PAC_EXPANSION_INVALID},
                }
            ),
            "request-1",
        ),
    )
    corrected = child_document(1).replace("do work 1", "corrected work")
    corrected_fields = expansion_outcome_fields(corrected, attempt_no=1)
    enqueue_outcome_row(
        db,
        request_id="request-1",
        action="complete",
        reason="evidence:corrected",
        output_text=None,
        expansion_text=corrected,
        expansion_digest_value=corrected_fields["expansionDigest"],
    )
    row = db.execute("SELECT * FROM remote_workflow_outbox").fetchone()
    assert row["attempt_no"] == 1
    assert row["result_json"] is None
    assert row["expansion_digest"] == corrected_fields["expansionDigest"]

    db.execute(
        "UPDATE remote_workflow_outbox SET result_json=?",
        (json.dumps({"returnState": "accepted"}),),
    )
    with pytest.raises(PacError, match="queued or accepted"):
        enqueue_outcome_row(
            db,
            request_id="request-1",
            action="complete",
            reason="evidence:third",
            output_text=None,
            expansion_text=child_document(1).replace("do work 1", "third"),
            expansion_digest_value="f" * 64,
        )
    db.close()


def test_two_daemon_remote_expansion_is_preflighted_on_origin(tmp_path):
    from tests.daemon.pac.workflows.delivery.test_pac_remote_workflow import (
        Mesh,
        application,
    )
    from hyprial.daemon.impl.pac.contracts.expansion import (
        load_expansion_policy,
        validate_parent,
    )

    mesh = Mesh()
    origin = application(tmp_path, "origin", mesh)
    executor = application(tmp_path, "executor", mesh)
    repo, _base = repository(tmp_path)
    home, _work_root = policy_home(tmp_path, repo)
    origin.hyprial_home = home
    executor.hyprial_home = tmp_path / "executor-home"
    origin._workflow_admit = MethodType(lambda _self, spec, _sender: spec, origin)

    def admit(spec, _sender):
        validated = validate_parent(spec, policy=load_expansion_policy(home))
        for node in validated.nodes:
            if node.owner and origin.remote.remote(node.owner):
                origin.remote.admit(node)
        return validated

    origin._workflow_service.admit = admit
    try:
        graph_id = origin._workflow_service.start(
            yaml_text=yaml.safe_dump(
                {
                    "version": 2,
                    "name": "remote-expand",
                    "nodes": [
                        {
                            "id": "planner",
                            "owner": "agent:test:executor:borrowed",
                            "role": "plan",
                            "expands": "slot",
                            "task": "plan",
                        },
                        {
                            "id": "slot",
                            "kind": "expansion",
                            "after": ["planner"],
                            "timeout": "5m",
                            "limits": {
                                "maxNodes": 1,
                                "worktree": {"repo": str(repo), "base": "HEAD"},
                            },
                        },
                    ],
                }
            ),
            sender="user:test",
        )["graphId"]
        origin._workflow_service._tick()
        grant = executor.remote._lookup(
            graph_id=graph_id, actor="agent:test:executor:borrowed"
        )
        assert grant["features"] == ["pac-expansion-v1"]
        result = executor.remote.forward(
            "workflow.complete",
            {
                "graphId": graph_id,
                "nodeId": "planner",
                "requestId": grant["requestId"],
                "reasonRef": "evidence:remote-expansion",
                "expansion": child_document(1),
            },
            "agent:test:executor:borrowed",
        )
        assert result["returnState"] == "accepted"
        parent = origin._workflow_service.status(run_id=graph_id)
        placeholder = next(node for node in parent["nodes"] if node["nodeId"] == "slot")
        assert placeholder["childGraphId"].startswith("wf-")
    finally:
        for app in (origin, executor):
            app.remote.close()
            app._workflow_service.close()
