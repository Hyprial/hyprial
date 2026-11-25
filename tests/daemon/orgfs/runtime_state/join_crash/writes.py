# Decisions (package F8, real-node finding R5-1, 2026-11-25):
# - A holder that keeps writing keeps broadcasting, so a joining node's
#   temporary sync never sees an empty ingress queue.  The real node applied
#   each broadcast slowly enough (a 20k-document space) that the queued
#   frames outlived the five-second close budget; in process, ``slow_ingress``
#   makes every apply that runs on an ingress thread sleep, which reproduces
#   the same queue without slowing the join's own pull; ``slow_join_round``
#   stands for the seconds a 20k-document pull takes, during which the
#   broadcasts fill the queue (capacity 128).
# - ``Writer`` writes one new document after another on the holder from a
#   background thread until stopped, and reports how many it wrote.
# - ``assert_joined`` / ``assert_not_joined`` are the two outcomes ruling 1
#   allows, each checked on the space list, the status and the directory.
"""Helpers for joins that run while a holder keeps writing."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state.stores import JOIN_MARKER

from ..support import Peer


def slow_ingress(peer: Peer, delay: float) -> None:
    facade = peer.fs
    original = facade.apply_remote

    def apply_remote(*args: Any, **kwargs: Any) -> object:
        if threading.current_thread().name.startswith("orgfs3-ingress"):
            time.sleep(delay)
        return original(*args, **kwargs)

    facade.apply_remote = apply_remote


def slow_join_round(peer: Peer, delay: float) -> None:
    facade = peer.fs
    original = facade.complete_join

    def complete_join(space_id: str) -> object:
        time.sleep(delay)
        return original(space_id)

    facade.complete_join = complete_join


class Writer:
    """Writes new documents on ``peer`` until ``stop``."""

    def __init__(self, peer: Peer, space_id: str) -> None:
        self._peer = peer
        self._space_id = space_id
        self._stop = threading.Event()
        self.written = 0
        self.errors: list[BaseException] = []
        self._thread = threading.Thread(target=self._run, name="test-writer", daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._peer.fs.write_text(self._space_id, f"w-{self.written}.md", f"doc {self.written}")
            except BaseException as exc:  # noqa: BLE001 - reported by stop()
                self.errors.append(exc)
                return
            self.written += 1

    def __enter__(self) -> Writer:
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while self.written < 50 and time.monotonic() < deadline:
            time.sleep(0.01)
        return self

    def stop(self) -> int:
        self._stop.set()
        self._thread.join(timeout=30.0)
        assert self.errors == [], repr(self.errors)
        return self.written

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=30.0)


def space_ids(peer: Peer) -> set[str]:
    return {info.space_id for info in peer.fs.spaces()}


def assert_joined(peer: Peer, space_id: str) -> None:
    assert peer.runtime is not None
    assert space_id in space_ids(peer)
    peer.runtime.status(space_id)
    assert (peer.spaces_root / space_id).is_dir()
    assert not (peer.spaces_root / space_id / JOIN_MARKER).exists()
    assert peer.runtime._mesh(space_id) is not None  # noqa: SLF001 - the resident sync


def assert_not_joined(peer: Peer, space_id: str) -> None:
    assert peer.runtime is not None
    assert space_id not in space_ids(peer)
    with pytest.raises(OrgFsError) as error:
        peer.runtime.status(space_id)
    assert error.value.code == "unknown-space"
    assert not (peer.spaces_root / space_id).exists()


__all__ = ["Writer", "assert_joined", "assert_not_joined", "slow_ingress", "slow_join_round", "space_ids"]
