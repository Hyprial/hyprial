"""The config-rebind fence must exclude every desired-state write.

Reviewer finding B1 rests on one primitive: while the identity owner holds the
desired-state authority for a binding mutation, no other thread may commit
desired state. This pins that primitive directly.
"""

from __future__ import annotations

import threading
from pathlib import Path

from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec


def _spec(tmp_path: Path) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="pi", name="fenced", headless=True, cwd=str(tmp_path), args=()
    )


def test_config_rebind_fence_blocks_concurrent_desired_state_writes(
    tmp_path: Path,
) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")
    started = threading.Event()
    finished = threading.Event()

    def writer() -> None:
        started.set()
        store.upsert_harness(_spec(tmp_path))
        finished.set()

    with store.config_rebind_fence():
        # Reads stay available to the fencing owner (reentrant authority).
        assert store.load() is not None
        thread = threading.Thread(target=writer)
        thread.start()
        assert started.wait(5.0)
        assert not finished.wait(1.0)
    thread.join(5.0)

    assert finished.is_set()
    assert [item.name for item in store.load().harnesses] == ["fenced"]


def test_config_rebind_fence_is_reentrant_for_its_owner(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired.json")

    with store.config_rebind_fence():
        with store.config_rebind_fence():
            store.upsert_harness(_spec(tmp_path))

    assert [item.name for item in store.load().harnesses] == ["fenced"]
