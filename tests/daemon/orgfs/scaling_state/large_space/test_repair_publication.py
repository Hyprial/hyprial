# Decisions (phase F1 round 2, F1 report §9-3, 2026-11-25):
# - Finding: the frontier repairs verification commits are local tree
#   commits like any other: each is published and the peer has it within
#   milliseconds.  What kept ``synced`` false in the first measurement was
#   that the writer's only observation of the peer predated the repairs and
#   nothing pulled again; the due tick's "local vectors not observed" pull
#   (ruling on F1 item 4) closes that.
# - The C6 synthetic rows carry a ``contentFrontier`` behind their content,
#   so verifying them produces repair commits; 600 documents are enough.
"""Verification's frontier repairs are published and reach the peer."""

from __future__ import annotations

from pathlib import Path
import shutil
import time

import pytest

from hyprial.daemon.impl.orgfs.sync import covers

from ...runtime_state.support import Peers, eventually
from ..synthetic import AUTHOR
from .test_runtime_open import _build


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _tree(peer, space_id: str) -> bytes:
    return peer.fs.sync_host(space_id).read_vectors(("tree",))["tree"]


def test_repair_commits_are_published_reach_the_peer_and_one_due_tick_restores_synced(tmp_path: Path) -> None:
    root = tmp_path / "nodes"
    space_id = _build(root / "node-a", 600)
    shutil.copytree(root / "node-a", root / "node-b")
    peers = Peers(root)
    try:
        a = peers.add("node-a", AUTHOR)
        b = peers.add("node-b", AUTHOR)
        assert a.runtime is not None
        a.sync(space_id).sync_from("node-b")
        sync = a.sync(space_id)
        published: list[str] = []
        original = sync.publish

        def publish(doc_id: str, update: bytes, vector: bytes) -> None:
            published.append(doc_id)
            original(doc_id, update, vector)

        sync.publish = publish
        registry = a.fs._registry(space_id)  # noqa: SLF001
        before = registry.generation

        a.runtime.anti_entropy_tick(time.monotonic())

        repairs = registry.generation - before
        assert repairs > 0
        assert published.count("tree") == repairs
        eventually(lambda: _assert(covers(_tree(b, space_id), _tree(a, space_id))), timeout=5.0)
        assert a.runtime.status(space_id).synced is not True
        a.runtime._maintain_spaces(time.monotonic())  # noqa: SLF001 - one due anti-entropy tick
        eventually(lambda: _assert(a.runtime.status(space_id).synced is True), timeout=5.0)
    finally:
        peers.close()


def _assert(condition: bool) -> None:
    assert condition
