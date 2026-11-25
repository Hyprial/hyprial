"""G0 coverage manifest and snapshot pins for the request-surface goldens.

The manifest names every daemon request method exactly once, as covered (with
the scenarios that record it) or unprotected (with the reason).  A covered
entry also says how: ``coverage: result`` when at least one recorded step
returned a result, ``refusal-only`` when every recorded step is an error (a
degraded daemon's ``*_UNAVAILABLE``, a schema refusal), with the recorded
``errorCodes``.  A refusal-only entry pins only that refusal, so it carries a
reason like an unprotected one, and every phase A method must reach
``result``.  ``harnessArtifacts`` lists recorded values that differ from a
production home only because of the harness (see ``tests/support/golden``).  The method
universe is the dispatch registry plus the router's cost-counted names plus
the two link-era body methods, so a method added anywhere must be classified
here.  The snapshot files are pinned by sha256: changing a recorded response
needs both a re-record and a visible pin change in this manifest.

``HYPRIAL_GOLDEN_RECORD=1`` re-derives the pins and the covered entries from
the snapshots and keeps the authored reasons; a newly unprotected method
without a reason still fails.  Record the scenarios first, then re-pin, then
run once plainly (the mutation probes are skipped while recording):

    HYPRIAL_GOLDEN_RECORD=1 uv run pytest tests/daemon/golden -k "not manifest"
    HYPRIAL_GOLDEN_RECORD=1 uv run pytest tests/daemon/golden/test_golden_manifest.py
    uv run pytest tests/daemon/golden

A pin left stale by recording in one pass fails the next plain run.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import IPC_MESSAGE_READ_BODY, IPC_MESSAGE_SETTLE_HINT
from tests.support.golden.snapshot import SNAPSHOT_ROOT, SNAPSHOT_SCHEMA, recording, render

MANIFEST = SNAPSHOT_ROOT / "manifest.json"
RAW_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
MANIFEST_SCHEMA = "hyprial-golden-manifest/v1"

PHASE_A_REQUIRED = frozenset(
    {
        "message.send", "message.reply", "message.query",
        "message.pending.list", "message.pending.batch", "message.pending.wait",
        "message.status", "message.ack", "message.read_body", "message.history",
        "message.send.lookup", "message.settle_hint",
        "session.register", "session.register_guarded", "session.heartbeat",
        "session.refresh", "session.unregister", "session.turn.started",
        "session.turn.ended", "session.turn.refresh",
        "routine.add", "routine.list", "routine.status", "routine.audit",
        "routine.remove", "routine.set", "routine.pause", "routine.resume",
        "workflow.plan", "workflow.start",
        "pac.graph.activate", "pac.graph.close", "pac.flag.set", "pac.flag.reset",
        "outbox.list", "agent.create", "agent.list",
    }
)


def universe() -> frozenset[str]:
    return frozenset(
        set(_IPC_STATS_METHODS)
        | set(METHOD_REGISTRY.names)
        | {IPC_MESSAGE_READ_BODY, IPC_MESSAGE_SETTLE_HINT}
    )


def _snapshot_files() -> list[Path]:
    return sorted(
        path for path in SNAPSHOT_ROOT.rglob("*.json") if path != MANIFEST
    )


def _relative(path: Path) -> str:
    return path.relative_to(SNAPSHOT_ROOT).as_posix()


def _recorded_methods() -> dict[str, dict]:
    scenarios: dict[str, set[str]] = {}
    results: dict[str, int] = {}
    codes: dict[str, set[str]] = {}
    for path in _snapshot_files():
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema"] == SNAPSHOT_SCHEMA, path
        scenario = f"{document['group']}/{document['scenario']}"
        for step in document["steps"]:
            if "method" not in step:
                continue
            name = step["method"]
            scenarios.setdefault(name, set()).add(scenario)
            results.setdefault(name, 0)
            codes.setdefault(name, set())
            if "result" in step:
                results[name] += 1
            else:
                codes[name].add(step["error"]["code"])
    return {
        name: {
            "covered": True,
            "coverage": "result" if results[name] else "refusal-only",
            "scenarios": sorted(scenarios[name]),
            "errorCodes": sorted(codes[name]),
        }
        for name in scenarios
    }


def _pins() -> dict[str, str]:
    return {
        _relative(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _snapshot_files()
    }


def _load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _rerecord() -> None:
    previous = _load() if MANIFEST.is_file() else {"methods": {}}
    recorded = _recorded_methods()
    methods: dict[str, dict] = {}
    for name in sorted(universe()):
        prior = previous["methods"].get(name, {})
        if name in recorded:
            methods[name] = dict(recorded[name])
            if recorded[name]["coverage"] == "refusal-only":
                methods[name]["reason"] = prior.get("reason", "")
            continue
        methods[name] = {"covered": False, "reason": prior.get("reason", "")}
    MANIFEST.write_text(
        render(
            {
                "schema": MANIFEST_SCHEMA,
                "harnessArtifacts": previous.get("harnessArtifacts", []),
                "snapshots": _pins(),
                "methods": methods,
            }
        ),
        encoding="utf-8",
    )


def test_manifest_classifies_the_whole_request_surface_once() -> None:
    if recording():
        _rerecord()
    manifest = _load()
    assert manifest["schema"] == MANIFEST_SCHEMA
    entries = manifest["methods"]
    covered = {name for name, entry in entries.items() if entry["covered"] is True}
    unprotected = {name for name, entry in entries.items() if entry["covered"] is False}
    assert covered.isdisjoint(unprotected)
    assert covered | unprotected == set(entries)
    assert covered | unprotected == universe(), (
        sorted(universe() - set(entries)),
        sorted(set(entries) - universe()),
    )
    partial = {
        name for name in covered if entries[name]["coverage"] == "refusal-only"
    }
    missing_reason = sorted(
        name
        for name in unprotected | partial
        if not entries[name].get("reason", "").strip()
    )
    assert missing_reason == [], (
        f"unprotected and refusal-only methods need a reason: {missing_reason}"
    )


def test_covered_entries_are_exactly_what_the_snapshots_record() -> None:
    entries = _load()["methods"]
    recorded = _recorded_methods()
    covered = {
        name: {key: value for key, value in entry.items() if key != "reason"}
        for name, entry in entries.items()
        if entry["covered"]
    }
    assert covered == recorded


def test_every_phase_a_method_records_a_result() -> None:
    entries = _load()["methods"]
    weak = sorted(
        name
        for name in PHASE_A_REQUIRED
        if entries.get(name, {}).get("coverage") != "result"
    )
    assert weak == [], "phase A methods covered only by refusals or not at all"


def test_harness_artifacts_are_declared() -> None:
    artifacts = _load()["harnessArtifacts"]
    assert artifacts and all(isinstance(item, str) and item.strip() for item in artifacts)


def test_snapshot_files_match_their_pinned_sha256() -> None:
    pinned = _load()["snapshots"]
    assert pinned == _pins(), "re-record with HYPRIAL_GOLDEN_RECORD=1 and review the pin diff"


def test_snapshot_paths_stay_inside_the_snapshot_root() -> None:
    for path in _snapshot_files():
        assert Path(SNAPSHOT_ROOT) in path.parents
        assert path.parent != SNAPSHOT_ROOT, f"{path} must live in a group directory"


def test_snapshots_hold_no_raw_uuid4() -> None:
    raw = {
        _relative(path): sorted(set(RAW_UUID4.findall(path.read_text(encoding="utf-8"))))
        for path in _snapshot_files()
    }
    assert {name: found for name, found in raw.items() if found} == {}, (
        "a uuid4 drawn outside the seeded sequence reached a snapshot"
    )
