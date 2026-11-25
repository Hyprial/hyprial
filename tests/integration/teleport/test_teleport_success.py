"""Slice 5 scenario 1: a multi-chunk agent home teleports between two real daemons.

Decision record:

- Two arms on one daemon pair: the raw ``teleport.run`` request and the real
  CLI ``hyprial agent teleport NAME --to NODE --yes --json`` against the source
  daemon's socket.  Each arm moves its own agent, so the arms do not share
  state.
- The workspace carries ``3 * CHUNK`` random bytes plus small files, so the
  packed tar needs at least four ``TELEPORT_CHUNK_BYTES`` chunks.
- Success is read from the daemons' own witnesses, never only from the
  teleport result: the target's identity row and ``ps`` connector, the landed
  home's bytes, the target's ``lifecycle.start`` having resumed the original
  session ref (the source's transcript must have landed under the target's
  session root), the source's identity row and home directory being gone,
  and the stub log proving no ``ssh``/``rsync``/``scp`` (or the ``claude``
  stub) was ever invoked.
- On failure every assertion message carries the full daemon reply so the
  report can cite the code and message.
"""

from __future__ import annotations

from typing import Any

from .conftest import (
    CHUNK,
    SOURCE_NODE,
    TARGET_NODE,
    TELEPORT_WAIT_SECONDS,
    Pair,
    Worker,
    agent_home,
    agent_rows,
    connector,
    desired_harness,
    seed_worker,
    staging_entries,
    teleport,
    tree_digest,
    wait_running,
)

PAYLOAD = 3 * CHUNK + 4096


def _assert_moved(pair: Pair, worker: Worker, before: dict[str, str],
                  result: dict[str, Any]) -> None:
    assert result["ok"] is True, result
    assert result["dryRun"] is False, result
    assert result["agent"] == worker.name, result
    assert result["from"]["node"] == SOURCE_NODE, result
    assert result["to"]["node"] == TARGET_NODE, result
    assert result["bytes"] >= 3 * CHUNK, result
    assert result["commit"] == {"ok": True, "pausedRoutines": []}, result

    target_rows = agent_rows(pair.target)
    assert worker.name in target_rows, target_rows
    assert result["to"]["uri"] == target_rows[worker.name], (result, target_rows)
    assert f":{TARGET_NODE}:" in result["to"]["uri"], result

    landed = agent_home(pair.target, worker.name)
    landed_workspace = tree_digest(landed / "workspace")
    expected_workspace = {
        path.removeprefix("workspace/"): digest
        for path, digest in before.items()
        if path.startswith("workspace/")
    }
    assert landed_workspace == expected_workspace
    transcript_name = f"{worker.session_ref}.jsonl"
    landed_transcripts = list(
        (landed / "state" / "sessions" / "claude").glob(f"*/{transcript_name}")
    )
    assert len(landed_transcripts) == 1, landed_transcripts
    source_relative = str(worker.transcript.relative_to(worker.home))
    assert tree_digest(landed_transcripts[0].parent)[transcript_name] == before[
        source_relative
    ]

    assert wait_running(pair.target, worker.name), connector(pair.target, worker.name)
    item = connector(pair.target, worker.name)
    assert item is not None and item.get("running") is True, item
    desired = desired_harness(pair.target, worker.name)
    assert desired is not None, desired
    assert desired["session_ref"] == worker.session_ref, desired
    assert desired["cwd"] == str(landed / "workspace"), desired

    assert worker.name not in agent_rows(pair.source)
    assert connector(pair.source, worker.name) is None
    assert not worker.home.exists()
    assert staging_entries(pair.target) == []
    assert pair.stub_calls() == []


def test_multi_chunk_home_teleports_over_the_daemon_request(pair: Pair) -> None:
    worker = seed_worker(pair.source, "tp-move-ipc", payload_bytes=PAYLOAD)
    before = tree_digest(worker.home)

    document = teleport(pair.source, worker.name, TARGET_NODE)

    assert "error" not in document, document
    _assert_moved(pair, worker, before, document["result"])


def test_multi_chunk_home_teleports_through_the_cli(pair: Pair) -> None:
    worker = seed_worker(pair.source, "tp-move-cli", payload_bytes=PAYLOAD)
    before = tree_digest(worker.home)

    result = pair.source.command(
        "agent", "teleport", worker.name, "--to", TARGET_NODE, "--yes", "--json",
        exits=(0, 1),
        timeout=TELEPORT_WAIT_SECONDS,
    )

    assert result.get("ok") is True, result
    _assert_moved(pair, worker, before, result)
