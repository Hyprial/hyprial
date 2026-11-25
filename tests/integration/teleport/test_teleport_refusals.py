"""Slice 5 scenarios 2, 4 and 5: teleports that must change neither node.

Decision record:

- Scenario 2: the target already has an agent with the same name (created
  through the target's own ``agent.create``), so ``prepare`` answers
  ``TELEPORT_AGENT_EXISTS`` and the source never stops its worker.
- Scenario 4: ``dryRun`` runs the preflight and ``prepare`` only; the result
  says so (``dryRun: true``, ``to.uri: null``, ``bytes: 0``, no ``commit``).
- Scenario 5: an unknown ``--to`` gets no prepare answer within the 10 s
  budget and is ``TELEPORT_WRONG_TARGET``; naming the source's own node is
  refused locally with the same code before any request is sent.
- "Unchanged" is read from witnesses: the source worker is still running on
  the same session ref (``ps``), every byte of its home is identical, its
  identity row is still there, the target has no row for it and its teleport
  staging root is empty, and no ssh/rsync/scp stub was called.
"""

from __future__ import annotations

from .conftest import (
    CHUNK,
    SOURCE_NODE,
    TARGET_NODE,
    Pair,
    Worker,
    agent_rows,
    connector,
    desired_harness,
    ok,
    running,
    seed_worker,
    staging_entries,
    teleport,
    tree_digest,
)

PAYLOAD = 3 * CHUNK + 4096


def _pid(pair: Pair, worker: Worker) -> int | None:
    item = connector(pair.source, worker.name)
    return None if item is None else item.get("pid")


def _assert_source_unchanged(pair: Pair, worker: Worker, before: dict[str, str],
                             source_uri: str, pid: int | None) -> None:
    assert running(pair.source, worker.name), connector(pair.source, worker.name)
    assert pid is not None and _pid(pair, worker) == pid
    desired = desired_harness(pair.source, worker.name)
    assert desired is not None and desired["session_ref"] == worker.session_ref, desired
    assert tree_digest(worker.home) == before
    assert agent_rows(pair.source).get(worker.name) == source_uri
    assert pair.stub_calls() == []


def test_name_taken_on_target_is_refused_and_source_is_untouched(pair: Pair) -> None:
    worker = seed_worker(pair.source, "tp-taken", payload_bytes=PAYLOAD)
    ok(pair.target, "agent.create", {"name": worker.name})
    occupant = agent_rows(pair.target)[worker.name]
    source_uri = agent_rows(pair.source)[worker.name]
    before = tree_digest(worker.home)
    pid = _pid(pair, worker)

    document = teleport(pair.source, worker.name, TARGET_NODE)

    error = document.get("error")
    assert isinstance(error, dict), document
    assert error["code"] == "TELEPORT_AGENT_EXISTS", error
    _assert_source_unchanged(pair, worker, before, source_uri, pid)
    assert agent_rows(pair.target)[worker.name] == occupant
    assert staging_entries(pair.target) == []


def test_dry_run_changes_neither_node(pair: Pair) -> None:
    worker = seed_worker(pair.source, "tp-dry", payload_bytes=PAYLOAD)
    source_uri = agent_rows(pair.source)[worker.name]
    before = tree_digest(worker.home)
    pid = _pid(pair, worker)
    target_before = agent_rows(pair.target)

    document = teleport(pair.source, worker.name, TARGET_NODE, dry_run=True)

    assert "error" not in document, document
    result = document["result"]
    assert result["ok"] is True, result
    assert result["dryRun"] is True, result
    assert result["agent"] == worker.name, result
    assert result["from"] == {"node": SOURCE_NODE, "uri": source_uri}, result
    assert result["to"] == {"node": TARGET_NODE, "uri": None}, result
    assert result["bytes"] == 0, result
    assert "commit" not in result, result
    _assert_source_unchanged(pair, worker, before, source_uri, pid)
    assert agent_rows(pair.target) == target_before
    assert worker.name not in agent_rows(pair.target)
    assert staging_entries(pair.target) == []


def test_dry_run_through_the_cli_says_nothing_changed(pair: Pair) -> None:
    worker = seed_worker(pair.source, "tp-dry-cli", payload_bytes=PAYLOAD)
    source_uri = agent_rows(pair.source)[worker.name]
    before = tree_digest(worker.home)
    pid = _pid(pair, worker)

    result = pair.source.command(
        "agent", "teleport", worker.name, "--to", TARGET_NODE, "--dry-run", "--json",
        exits=(0, 1),
        timeout=120.0,
    )

    assert result.get("ok") is True, result
    assert result["dryRun"] is True, result
    assert result["to"]["uri"] is None, result
    _assert_source_unchanged(pair, worker, before, source_uri, pid)
    assert worker.name not in agent_rows(pair.target)
    assert staging_entries(pair.target) == []


def test_unknown_or_own_target_node_is_wrong_target(pair: Pair) -> None:
    worker = seed_worker(pair.source, "tp-nowhere", payload_bytes=PAYLOAD)
    source_uri = agent_rows(pair.source)[worker.name]
    before = tree_digest(worker.home)
    pid = _pid(pair, worker)

    unknown = teleport(pair.source, worker.name, "tp-no-such-node")
    own = teleport(pair.source, worker.name, SOURCE_NODE)

    for document in (unknown, own):
        error = document.get("error")
        assert isinstance(error, dict), document
        assert error["code"] == "TELEPORT_WRONG_TARGET", error
    _assert_source_unchanged(pair, worker, before, source_uri, pid)
    assert worker.name not in agent_rows(pair.target)
    assert staging_entries(pair.target) == []
