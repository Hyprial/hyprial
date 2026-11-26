# Decisions (package F8b, review of F8 items 1-6, 8 and 11, 2026-11-26):
# - A join removes ``join.pending`` before the space is listed, so a space a
#   caller can write to is never discarded: not by a failing tail (the
#   resident sync, the announce), not by the startup scan after a crash.  A
#   tail that fails after the listing reports ``unavailable`` and keeps the
#   space; the next join finishes it.  The crash is a copy of the node's state
#   directory taken right after ``complete_join`` and a write, opened by a
#   fresh runtime.
# - The resident sync is forced to fail while ``failing`` is set by wrapping
#   ``_new_sync`` for ``node_level`` syncs; every creation fails, including
#   the one a local write's broadcast effect asks for, so the outcome does not
#   depend on which thread creates the sync first.
# - Repeated joins of one space wait for the running join's outcome instead
#   of each holding a lifecycle worker: six joins of a slow space must not
#   delay the join of another space.
# - Join errors are ``OrgFsError`` with a message that names no local path.
"""A join's tail never drops a listed space; repeated joins share one worker."""

from __future__ import annotations

import errno
from pathlib import Path
import shutil
import threading
import time
from typing import Any

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from hyprial.daemon.impl.orgfs.composition.state import sync as join_module
from hyprial.daemon.impl.orgfs.composition.state.stores import JOIN_MARKER
from hyprial.kernel import atomic_json_write

from ..support import Events, Peer, Peers, eventually, text
from .writes import assert_joined, assert_not_joined, space_ids

OWNER = "user:owner@example"
MEMBER = "user:member@example"


def _pair(peers: Peers, *names: str) -> tuple[Peer, Peer, list[str]]:
    owner = peers.add("node-owner", OWNER)
    member = peers.add("node-member", MEMBER)
    spaces = []
    for name in names or ("team",):
        space_id = owner.fs.create_space(name).space_id
        owner.fs.invite(space_id, MEMBER, "rw")
        owner.fs.write_text(space_id, "base.md", "base")
        spaces.append(space_id)
    return owner, member, spaces


class _ResidentFailure:
    def __init__(self, peer: Peer, error: BaseException) -> None:
        runtime = peer.runtime
        assert runtime is not None
        original = runtime._new_sync  # noqa: SLF001
        self.failing = threading.Event()
        self.failed = 0

        def new_sync(space_id: str, gate: Any, *, node_level: bool) -> Any:
            if node_level and self.failing.is_set():
                self.failed += 1
                raise error
            return original(space_id, gate, node_level=node_level)

        runtime._new_sync = new_sync  # noqa: SLF001


def _write_in_tail(peer: Peer, path: str, content: str, after: Any = None) -> list[object]:
    facade = peer.fs
    original = facade.complete_join
    outcomes: list[object] = []

    def complete_join(space_id: str, *args: Any, **kwargs: Any) -> object:
        info = original(space_id, *args, **kwargs)
        result: list[object] = []

        def write() -> None:
            try:
                facade.write_text(space_id, path, content)
                result.append("ok")
            except BaseException as exc:  # noqa: BLE001 - reported below
                result.append(exc)

        writer = threading.Thread(target=write)
        writer.start()
        writer.join(timeout=20.0)
        outcomes.append(result[0] if result else "timeout")
        if after is not None:
            after(space_id)
        return info

    facade.complete_join = complete_join
    return outcomes


def _reaches(peers: Peers, holder: Peer, space_id: str, path: str, content: str) -> None:
    peers.pull_all(space_id)
    assert text(holder.fs, space_id, path) == content


def _strip_resident_sync(peer: Peer, space_id: str) -> None:
    runtime = peer.runtime
    assert runtime is not None
    with runtime._resource_lock:  # noqa: SLF001 - rebuild the R5-1 leftover
        mesh = runtime._meshes.pop(space_id)  # noqa: SLF001
    peer.fs.detach_sync(space_id)
    mesh.close()
    atomic_json_write(peer.spaces_root / space_id / JOIN_MARKER, {"spaceId": space_id})


def test_a_write_made_during_the_tail_survives_a_resident_sync_that_fails(peers: Peers) -> None:
    owner, member, (space_id,) = _pair(peers)
    writes = _write_in_tail(member, "local.md", "written by the member")
    failure = _ResidentFailure(
        member, OSError(errno.EIO, "Input/output error", str(member.spaces_root / space_id / "x"))
    )
    failure.failing.set()

    with pytest.raises(OrgFsError) as error:
        member.fs.join(space_id)

    failure.failing.clear()
    assert failure.failed
    assert writes == ["ok"]
    assert space_id in space_ids(member)
    assert not (member.spaces_root / space_id / JOIN_MARKER).exists()
    assert text(member.fs, space_id, "local.md") == "written by the member"
    assert (error.value.code, error.value.details.get("reason")) == ("unavailable", "join-incomplete")
    assert error.value.message != error.value.code
    assert str(member.state_dir) not in str(error.value)
    member.fs.join(space_id)
    assert_joined(member, space_id)
    eventually(lambda: _reaches(peers, owner, space_id, "local.md", "written by the member"), timeout=30.0)


def test_finishing_a_listed_space_with_local_writes_never_drops_it(peers: Peers) -> None:
    owner, member, (space_id,) = _pair(peers)
    member.fs.join(space_id)
    failure = _ResidentFailure(member, RuntimeError("transport hiccup"))
    failure.failing.set()
    _strip_resident_sync(member, space_id)
    member.fs.write_text(space_id, "mine.md", "kept")

    with pytest.raises(OrgFsError) as error:
        member.fs.join(space_id)

    failure.failing.clear()
    assert failure.failed
    assert error.value.code == "unavailable"
    assert space_id in space_ids(member)
    assert (member.spaces_root / space_id).is_dir()
    assert text(member.fs, space_id, "mine.md") == "kept"
    member.fs.join(space_id)
    assert_joined(member, space_id)
    eventually(lambda: _reaches(peers, owner, space_id, "mine.md", "kept"), timeout=30.0)


def test_a_space_listed_by_a_join_survives_a_crash_right_after(peers: Peers, tmp_path: Path) -> None:
    _owner, member, (space_id,) = _pair(peers)
    crash = tmp_path / "crash-copy"
    _write_in_tail(
        member, "local.md", "written before the crash",
        after=lambda _space: shutil.copytree(member.state_dir, crash),
    )

    member.fs.join(space_id)

    restarted = OrgFsRuntime(crash, node_id="node-member", author=MEMBER, logger=Events())
    try:
        assert space_id in {info.space_id for info in restarted.facade.spaces()}
        assert text(restarted.facade, space_id, "local.md") == "written before the crash"
    finally:
        restarted.close()


def test_an_announce_that_fails_does_not_fail_the_join(peers: Peers) -> None:
    _owner, member, (space_id,) = _pair(peers)
    runtime = member.runtime
    assert runtime is not None
    original = runtime._new_sync  # noqa: SLF001

    def new_sync(space: str, gate: Any, *, node_level: bool) -> Any:
        sync = original(space, gate, node_level=node_level)
        if node_level:
            def announce() -> None:
                raise RuntimeError("session is rebuilding")

            sync.announce = announce
        return sync

    runtime._new_sync = new_sync  # noqa: SLF001

    info = member.fs.join(space_id)

    assert info.space_id == space_id
    assert_joined(member, space_id)
    assert "orgfs.announce.failed" in member.events.names()


def test_a_marker_that_cannot_be_written_is_an_orgfs_error(
    peers: Peers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner, member, (space_id,) = _pair(peers)

    def refuse(path: Path, _value: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device", str(path))

    monkeypatch.setattr(join_module, "atomic_json_write", refuse)

    with pytest.raises(OrgFsError) as error:
        member.fs.join(space_id)

    assert error.value.code == "unavailable"
    assert error.value.message != error.value.code
    assert str(member.state_dir) not in str(error.value)
    assert_not_joined(member, space_id)
    monkeypatch.undo()
    member.fs.join(space_id)
    assert_joined(member, space_id)


def test_a_discard_that_fails_keeps_the_error_and_leaves_a_space_the_restart_drops(
    peers: Peers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner, member, (space_id,) = _pair(peers)
    original = join_module.fsync_directory
    calls: list[Path] = []

    def fsync_directory(path: Path) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise OSError(errno.EIO, "Input/output error", str(path))
        original(path)

    def discard(_space: str) -> None:
        raise RuntimeError("discard refused")

    monkeypatch.setattr(join_module, "fsync_directory", fsync_directory)
    member.fs._store_discard = discard  # noqa: SLF001

    with pytest.raises(OrgFsError) as error:
        member.fs.join(space_id)

    assert (error.value.code, error.value.details.get("reason")) == ("unavailable", "join-incomplete")
    assert space_id not in space_ids(member)
    assert (member.spaces_root / space_id / JOIN_MARKER).exists()
    monkeypatch.undo()
    member.reopen()
    assert "orgfs.space.unfinished-discarded" in member.events.names()
    assert not (member.spaces_root / space_id).exists()
    member.fs.join(space_id)
    assert_joined(member, space_id)


def test_a_failed_join_leaves_no_sync_registered(peers: Peers) -> None:
    owner = peers.add("node-owner", OWNER)
    member = peers.add("node-member", MEMBER)
    space_id = owner.fs.create_space("closed").space_id
    runtime = member.runtime
    assert runtime is not None
    closed: list[bool] = []

    class Stray:
        def announce(self) -> None:
            return None

        def schedule_sync_from(self, _node: str) -> bool:
            return False

        def close(self, timeout: float = 5.0) -> None:
            closed.append(True)

    original = member.fs.begin_join

    def begin_join(space: str) -> None:
        original(space)
        with runtime._resource_lock:  # noqa: SLF001
            runtime._meshes[space] = Stray()  # noqa: SLF001

    member.fs.begin_join = begin_join

    with pytest.raises(OrgFsError) as error:
        member.fs.join(space_id)

    assert error.value.code == "not-a-member"
    assert space_id not in runtime._meshes  # noqa: SLF001
    assert closed
    assert_not_joined(member, space_id)


def test_repeated_joins_of_one_space_do_not_hold_up_another(peers: Peers) -> None:
    _owner, member, (slow, quick) = _pair(peers, "slow", "quick")
    original = member.fs.complete_join

    def complete_join(space_id: str, *args: Any, **kwargs: Any) -> object:
        if space_id == slow:
            time.sleep(5.0)
        return original(space_id, *args, **kwargs)

    member.fs.complete_join = complete_join
    outcomes: list[object] = []
    lock = threading.Lock()

    def join_slow() -> None:
        try:
            member.fs.join(slow)
            assert_joined(member, slow)
            outcome: object = "joined"
        except BaseException as exc:  # noqa: BLE001 - reported below
            outcome = exc
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=join_slow) for _ in range(6)]
    for thread in threads:
        thread.start()
        time.sleep(0.05)
    time.sleep(0.5)

    started = time.monotonic()
    member.fs.join(quick)
    elapsed = time.monotonic() - started

    for thread in threads:
        thread.join(timeout=60.0)
    assert elapsed < 2.5
    assert_joined(member, quick)
    assert outcomes == ["joined"] * 6


def test_join_locks_do_not_outlive_their_joins(peers: Peers) -> None:
    _owner, member, (space_id,) = _pair(peers)
    runtime = member.runtime
    assert runtime is not None

    for index in range(20):
        with pytest.raises(OrgFsError) as error:
            member.fs.join(f"not-a-space-{index}")
        assert error.value.code == "unknown-space"
    member.fs.join(space_id)

    assert runtime._join_locks == {}  # noqa: SLF001
