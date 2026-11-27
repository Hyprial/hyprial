# Decisions (phase F1, item 4 and its ruling, 2026-11-25):
# - Two state runtimes hold byte-identical copies of the C6 synthetic space
#   (25,000 content documents, a 50,000 row tree) on the in-process network.
#   Node A is reopened, so every content document is unverified (design §9
#   R4).  The synthetic rows carry a ``contentFrontier`` behind their
#   content, which verification repairs with local tree commits; a settle
#   pass (open, verify, close) runs once before the copy so the measured
#   verification is the steady case: no commits.
# - Ruling on item 4: one ``anti_entropy_tick`` call (one maintenance pass)
#   verifies the whole backlog in VERIFY_BATCH batches; the bound is pinned
#   by counting batches and passes, not by wall time.  One due tick
#   (``_maintain_spaces``) after verification, and one after a local write
#   that the peer already received by broadcast, schedules the pull that
#   turns ``synced`` true; the test waits for that pull's worker only.
# - Times go to ``F1_SYNCED_OUT`` (JSON) and are recorded, not asserted.
#   ``ORGFS_SCALING_SYNCED_DOCS`` shrinks the space for quick runs.
"""How long until ``status.synced`` is true on a large reopened space."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import threading
import time

import pytest

from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from hyprial.daemon.impl.orgfs.composition.state.sync import VERIFY_BATCH

from ...runtime_state.support import Peers
from ..synthetic import AUTHOR
from .test_runtime_open import _build


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _unverified(peer, space_id: str) -> int:
    return len(peer.fs._registry(space_id).unverified)  # noqa: SLF001 - the R4 queue


def _settle(state_dir: Path, space_id: str) -> int:
    """Verify once so the synthetic rows' lagging frontiers are repaired on disk."""

    runtime = OrgFsRuntime(state_dir, node_id="node-a", author=AUTHOR)
    try:
        registry = runtime.facade._registry(space_id)  # noqa: SLF001
        before = registry.generation
        while registry.unverified:
            runtime.facade.sync_host(space_id).pending_content(VERIFY_BATCH)
        return registry.generation - before
    finally:
        runtime.close()


def _covers(supplier, requester, space_id: str) -> bool:
    from hyprial.daemon.impl.orgfs.sync import covers

    mine = requester.fs.sync_host(space_id).read_vectors(("tree",))["tree"]
    theirs = supplier.fs.sync_host(space_id).read_vectors(("tree",))["tree"]
    return covers(theirs, mine)


def _wait_synced(peer, space_id: str, timeout: float = 10.0) -> float | None:
    """Seconds until ``status.synced`` is true (pull rounds finish on the worker)."""

    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if peer.runtime.status(space_id).synced is True:
            return round(time.monotonic() - started, 3)
        time.sleep(0.01)
    return None


def test_one_maintenance_pass_verifies_a_reopened_space_and_one_due_tick_restores_synced(
    tmp_path: Path,
) -> None:
    documents = int(os.environ.get("ORGFS_SCALING_SYNCED_DOCS", "25000"))
    root = tmp_path / "nodes"
    started = time.perf_counter()
    space_id = _build(root / "node-a", documents)
    settle_commits = _settle(root / "node-a", space_id)
    shutil.copytree(root / "node-a", root / "node-b")
    build_s = time.perf_counter() - started
    peers = Peers(root)
    try:
        opened = time.perf_counter()
        a = peers.add("node-a", AUTHOR)
        b = peers.add("node-b", AUTHOR)
        open_s = time.perf_counter() - opened
        assert a.runtime is not None
        runtime = a.runtime
        unverified_at_open = _unverified(a, space_id)
        status_at_open = runtime.status(space_id)
        a.sync(space_id).sync_from("node-b")
        synced_after_first_pull = runtime.status(space_id).synced

        batches = _count_batches(a)
        generation_before = a.fs._registry(space_id).generation  # noqa: SLF001
        tick = time.perf_counter()
        runtime.anti_entropy_tick(time.monotonic())
        verify_s = time.perf_counter() - tick
        drain_batches = len(batches)
        status_verified = runtime.status(space_id)
        generation_after = a.fs._registry(space_id).generation  # noqa: SLF001
        runtime._maintain_spaces(time.monotonic())  # noqa: SLF001 - one due anti-entropy tick
        synced_after_verification = _wait_synced(a, space_id)

        a.fs.write_text(space_id, "f1-new.md", "written after verification")
        synced_after_write = runtime.status(space_id).synced
        b_has_it = _wait_for(lambda: "f1-new.md" in {node.name for node in b.fs.listdir(space_id, "")})
        runtime._maintain_spaces(time.monotonic())  # noqa: SLF001 - one due anti-entropy tick
        synced_after_write_and_one_tick = _wait_synced(a, space_id)

        result = {
            "documents": documents,
            "build_s": round(build_s, 2),
            "synthetic_rows_repaired_by_settle_commits": settle_commits,
            "open_two_nodes_s": round(open_s, 2),
            "unverified_at_open": unverified_at_open,
            "status_at_open": {"synced": status_at_open.synced, "content_pending": status_at_open.content_pending},
            "synced_after_first_pull_before_verification": synced_after_first_pull,
            "verify_batch": VERIFY_BATCH,
            "verification_batches_in_one_pass": drain_batches,
            "maintenance_passes_to_verify": 1,
            "verify_total_s": round(verify_s, 3),
            "batch_mean_ms": round(1000 * verify_s / max(1, drain_batches), 1),
            "batch_max_ms": round(1000 * max(batches[:drain_batches], default=0.0), 1),
            "per_document_us": round(1e6 * verify_s / max(1, unverified_at_open), 1),
            "local_commits_during_verification": generation_after - generation_before,
            "content_pending_after_verification": status_verified.content_pending,
            "synced_after_one_due_tick_s": synced_after_verification,
            "synced_right_after_local_write": synced_after_write,
            "peer_received_the_write_by_broadcast": b_has_it,
            "synced_after_write_and_one_due_tick_s": synced_after_write_and_one_tick,
        }
        out = os.environ.get("F1_SYNCED_OUT")
        if out:
            Path(out).write_text(json.dumps(result, indent=2))
        print(json.dumps(result))

        assert unverified_at_open == documents
        assert synced_after_first_pull is not True
        assert _unverified(a, space_id) == 0
        assert drain_batches <= -(-documents // VERIFY_BATCH)
        assert status_verified.content_pending == 0
        assert generation_after == generation_before
        assert synced_after_verification is not None
        assert synced_after_write is False
        assert b_has_it
        assert synced_after_write_and_one_tick is not None
    finally:
        peers.close()


def _count_batches(peer) -> list[float]:
    """Count and time the verification batches run on the calling thread.

    Sync workers call the same method for their read-first requests; only the
    maintenance thread's (this test's) calls are the drain being measured.
    """

    calls: list[float] = []
    original = peer.fs._pending_content_locked  # noqa: SLF001
    caller = threading.get_ident()

    def counted(space_id: str, limit: int):
        started = time.perf_counter()
        try:
            return original(space_id, limit)
        finally:
            if threading.get_ident() == caller:
                calls.append(time.perf_counter() - started)

    peer.fs._pending_content_locked = counted  # noqa: SLF001
    return calls


def _wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False
