# Decisions (package F8, real-node finding R5-1, rulings 1, 3, 4 and 6, 2026-11-25):
# - A join ends in one of two outcomes only: joined (listed, a status, the
#   directory without ``join.pending`` and a resident sync) or not joined
#   (not listed, ``unknown-space`` status, no directory, and a later join
#   behaves like the first).  ``_settled`` checks whichever one the join
#   reported.  A failure is an ``OrgFsError`` with a message, never a bare
#   runtime error (which the daemon reports as ``DAEMON_ERROR``).
# - The temporary sync's close is forced to fail by wrapping the instances
#   ``_new_sync`` builds without ``node_level``; the marker removal is forced
#   to fail by wrapping ``Path.unlink`` for that one file.
# - A space left with ``join.pending`` (and no resident sync) in a running
#   daemon is built by hand from a completed join, which is what the R5-1
#   failure left behind; a leftover hidden temporary space is built with
#   ``begin_join`` and the marker.  A second join must repair both.
# - Two joins of one space at the same time both report the joined space,
#   and each one's success is already true on disk when it returns.
"""What a join leaves behind when its tail fails, repeats or overlaps."""

from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state.stores import JOIN_MARKER
from hyprial.kernel import atomic_json_write

from ..support import Peer, Peers, eventually, text
from .writes import assert_joined, assert_not_joined, slow_join_round

OWNER = "user:owner@example"
MEMBER = "user:member@example"


def _pair(peers: Peers, *, invite: bool = True) -> tuple[Peer, Peer, str]:
    owner = peers.add("node-owner", OWNER)
    member = peers.add("node-member", MEMBER)
    space_id = owner.fs.create_space("team").space_id
    if invite:
        owner.fs.invite(space_id, MEMBER, "rw")
    owner.fs.write_text(space_id, "base.md", "base")
    return owner, member, space_id


def _failing_temporary_close(peer: Peer, error: BaseException) -> list[bool]:
    runtime = peer.runtime
    assert runtime is not None
    original = runtime._new_sync  # noqa: SLF001
    failed: list[bool] = []

    def new_sync(space_id: str, gate: Any, *, node_level: bool) -> Any:
        sync = original(space_id, gate, node_level=node_level)
        if not node_level:
            close = sync.close

            def failing_close(timeout: float = 5.0) -> None:
                close(timeout)
                failed.append(True)
                raise error

            sync.close = failing_close
        return sync

    runtime._new_sync = new_sync  # noqa: SLF001
    return failed


def _settled(peer: Peer, owner: Peer, space_id: str, join: Any) -> str:
    try:
        join()
    except OrgFsError as error:
        assert error.message != error.code
        assert_not_joined(peer, space_id)
        peer.fs.join(space_id)
        assert_joined(peer, space_id)
        return "not-joined"
    assert_joined(peer, space_id)
    owner.fs.write_text(space_id, "after.md", "after the join")
    eventually(lambda: _text(peer, space_id, "after.md", "after the join"), timeout=20.0)
    return "joined"


def _text(peer: Peer, space_id: str, path: str, expected: str) -> None:
    assert peer.runtime is not None
    peer.runtime.anti_entropy_tick(time.monotonic())
    assert text(peer.fs, space_id, path) == expected


@pytest.mark.parametrize("error", [TimeoutError("orgfs sync ingress did not drain"), RuntimeError("boom")])
def test_a_temporary_close_that_fails_after_the_join_completed_does_not_half_join(
    peers: Peers, error: BaseException
) -> None:
    owner, member, space_id = _pair(peers)
    failed = _failing_temporary_close(member, error)

    _settled(member, owner, space_id, lambda: member.fs.join(space_id))

    assert failed


def test_a_temporary_close_that_fails_after_a_refusal_keeps_the_refusal(peers: Peers) -> None:
    _owner, member, space_id = _pair(peers, invite=False)
    failed = _failing_temporary_close(member, TimeoutError("orgfs sync ingress did not drain"))

    with pytest.raises(OrgFsError) as refused:
        member.fs.join(space_id)

    assert refused.value.code == "not-a-member"
    assert failed
    assert_not_joined(member, space_id)


def test_a_marker_that_cannot_be_removed_leaves_one_of_the_two_outcomes(
    peers: Peers, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, member, space_id = _pair(peers)
    original = Path.unlink
    refused: list[Path] = []

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == JOIN_MARKER and not refused:
            refused.append(self)
            raise OSError("disk refused the unlink")
        original(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    _settled(member, owner, space_id, lambda: member.fs.join(space_id))

    assert refused


def test_a_space_left_with_join_pending_joins_again_without_a_restart(peers: Peers) -> None:
    owner, member, space_id = _pair(peers)
    member.fs.join(space_id)
    runtime = member.runtime
    assert runtime is not None
    with runtime._resource_lock:  # noqa: SLF001 - rebuild the R5-1 leftover
        mesh = runtime._meshes.pop(space_id)  # noqa: SLF001
    member.fs.detach_sync(space_id)
    mesh.close()
    atomic_json_write(member.spaces_root / space_id / JOIN_MARKER, {"spaceId": space_id})
    owner.fs.write_text(space_id, "missed.md", "written while no sync ran")

    member.fs.join(space_id)

    assert_joined(member, space_id)
    eventually(lambda: _text(member, space_id, "missed.md", "written while no sync ran"), timeout=20.0)


def test_a_leftover_temporary_space_is_joined_afresh(peers: Peers) -> None:
    owner, member, space_id = _pair(peers)
    member.fs.begin_join(space_id)
    atomic_json_write(member.spaces_root / space_id / JOIN_MARKER, {"spaceId": space_id})

    member.fs.join(space_id)

    assert_joined(member, space_id)
    eventually(lambda: _text(member, space_id, "base.md", "base"), timeout=20.0)


def _join_twice(member: Peer, space_id: str) -> list[object]:
    outcomes: list[object] = []
    lock = threading.Lock()

    def run() -> None:
        try:
            member.fs.join(space_id)
            assert_joined(member, space_id)
            outcome: object = "joined"
        except BaseException as exc:  # noqa: BLE001 - reported below
            outcome = exc
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=run) for _ in range(2)]
    threads[0].start()
    time.sleep(0.3)
    threads[1].start()
    for thread in threads:
        thread.join(timeout=60.0)
    return outcomes


def test_two_joins_during_the_pull_both_report_the_joined_space(peers: Peers) -> None:
    _owner, member, space_id = _pair(peers)
    slow_join_round(member, 1.0)

    assert _join_twice(member, space_id) == ["joined", "joined"]


def test_two_joins_during_the_tail_both_report_the_joined_space(peers: Peers) -> None:
    _owner, member, space_id = _pair(peers)
    facade = member.fs
    original = facade.complete_join

    def complete_join(space: str, **kwargs: Any) -> object:
        info = original(space, **kwargs)
        time.sleep(1.0)
        return info

    facade.complete_join = complete_join

    assert _join_twice(member, space_id) == ["joined", "joined"]
