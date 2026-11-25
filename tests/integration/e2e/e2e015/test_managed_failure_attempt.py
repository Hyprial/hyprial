"""A failed managed worker has one attempt under its issued native identity."""

import json
from pathlib import Path
import runpy

import pytest

GRAPH = "wf-fixture"
NAME = f"{GRAPH}-worker"
OWNER = f"agent:e2e:pac-test:{NAME}"
ACTOR = "agent:e2e:pac-test:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER = "agent:e2e:pac-test:a.bbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture(scope="module")
def check_attempt():
    runner = Path(__file__).resolve().parents[4] / "contract/e2e/run-pac-workflow-unified.py"
    return runpy.run_path(str(runner))["assert_failed_worker_attempt"]


def write_events(root, *, receipt_actor=ACTOR, receipt_name=NAME, conversation=None, receipt=True):
    rows = [{
        "event": "worker.turn.started", "name": NAME, "actorId": OWNER,
        "conversationId": conversation or f"pac-{GRAPH}",
    }]
    if receipt:
        rows.insert(0, {
            "event": "worker.environment.receipt", "name": receipt_name, "actor": receipt_actor,
        })
    target = root / "state/logs/workers" / f"codex-{NAME}.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("".join(json.dumps(row) + "\n" for row in rows))


def protocol(attempts=1):
    return [{"method": "turn/start", "fixtureActor": ACTOR}] * attempts + [
        {"method": "turn/start", "fixtureActor": OTHER},
    ]


def test_official_receipt_selects_one_failed_worker_turn(check_attempt, tmp_path):
    write_events(tmp_path)
    check_attempt(tmp_path, GRAPH, NAME, OWNER, protocol())


@pytest.mark.parametrize("attempts", [0, 2])
def test_missing_or_repeated_failed_worker_attempt_is_rejected(check_attempt, tmp_path, attempts):
    write_events(tmp_path)
    with pytest.raises(AssertionError, match="exactly one"):
        check_attempt(tmp_path, GRAPH, NAME, OWNER, protocol(attempts))


@pytest.mark.parametrize("changes", [
    {"receipt": False},
    {"receipt_name": "other-worker"},
    {"receipt_actor": OWNER},
    {"receipt_actor": "agent:other:pac-test:a.aaaaaaaaaaaaaaaaaaaaaaaaaa"},
])
def test_missing_or_mismatched_receipt_is_rejected(check_attempt, tmp_path, changes):
    write_events(tmp_path, **changes)
    with pytest.raises(AssertionError, match="receipt"):
        check_attempt(tmp_path, GRAPH, NAME, OWNER, protocol())


def test_receipt_requires_a_turn_for_this_graph_and_worker(check_attempt, tmp_path):
    write_events(tmp_path, conversation="pac-wf-other")
    with pytest.raises(AssertionError, match="graph/worker"):
        check_attempt(tmp_path, GRAPH, NAME, OWNER, protocol())
