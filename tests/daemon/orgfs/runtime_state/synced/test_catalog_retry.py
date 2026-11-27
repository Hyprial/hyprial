# Decisions (phase F4, item 2, real-node finding R2-1, 2026-11-25):
# - A large tree travels as a temporary sync blob (design §4.3).  When its
#   fetch fails the round still records the supplier's observation, whose
#   vectors cover the local ones, so the F1 due-tick rule ("no observation
#   covers the local vectors") never pulls again and nothing is pending: the
#   member keeps an empty tree until the holder happens to write.
# - The due tick therefore also pulls when an online holder's observation is
#   ahead of the local meta or tree vector, with a per-space backoff of
#   CATALOG_RETRY_BASE_SECONDS doubling up to CATALOG_RETRY_MAX_SECONDS; the
#   backoff resets once no observation is ahead.  Times are the ``now`` the
#   tick is given, so the test drives them without sleeping.
# - The page budget is lowered to the protocol minimum so a few hundred rows
#   make the tree a blob, and the member's transport drops blob queries while
#   ``failing`` is set.
# - Every blob chunk is asked for with SYNC_BLOB_FETCH_TIMEOUT_SECONDS, not
#   the round's (possibly almost spent) query timeout.
# - Item 2 (c), ruled after F4 (design question 2, option 1): ``synced`` is
#   also False while any online observation is ahead of the local meta or
#   tree vector, so an empty tree behind its holder is never reported synced.
"""A member whose tree blob failed pulls again on a due tick, with backoff."""

from __future__ import annotations

from threading import Event
from typing import Any

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state.sync import (
    CATALOG_RETRY_BASE_SECONDS,
    CATALOG_RETRY_MAX_SECONDS,
)
from hyprial.daemon.impl.orgfs.sync import pull as pull_module
from hyprial.daemon.impl.orgfs.sync.interface import SYNC_BLOB_FETCH_TIMEOUT_SECONDS, SYNC_MIN_PAGE_BYTES

from ..support import Peer, Peers, eventually

OWNER = "user:owner@example"
MEMBER = "user:member@example"
FOLDERS = 120


def _blob_queries(peer: Peer, failing: Event) -> list[float]:
    runtime = peer.runtime
    assert runtime is not None
    session = runtime._session  # noqa: SLF001 - the member's transport
    original = session.query
    timeouts: list[float] = []

    def query(key_expr: str, payload: bytes, **kwargs: Any) -> list[Any]:
        if "/org/fs/blob/" in key_expr:
            timeouts.append(float(kwargs.get("timeout", 0.0)))
            if failing.is_set():
                return []
        return original(key_expr, payload, **kwargs)

    session.query = query
    return timeouts


def _scheduled(peer: Peer, space_id: str, *, run: bool) -> list[str]:
    sync = peer.sync(space_id)
    calls: list[str] = []
    original = sync.schedule_sync_from

    def schedule(node: str) -> bool:
        calls.append(node)
        return original(node) if run else True

    sync.schedule_sync_from = schedule
    return calls


def _joined_with_a_failed_tree(peers: Peers, monkeypatch: pytest.MonkeyPatch) -> tuple[Peer, Peer, str, Event, list[float]]:
    monkeypatch.setattr(pull_module, "ORGFS_PAGE_BYTES", SYNC_MIN_PAGE_BYTES)
    owner = peers.add("node-owner", OWNER)
    member = peers.add("node-member", MEMBER)
    space_id = owner.fs.create_space("team").space_id
    for index in range(FOLDERS):
        owner.fs.mkdir(space_id, f"folder-{index:03}")
        owner.fs.write_text(space_id, f"folder-{index:03}/doc-{index:03}.md", f"body {index}")
    owner.fs.invite(space_id, MEMBER, "rw")
    failing = Event()
    failing.set()
    timeouts = _blob_queries(member, failing)

    member.fs.join(space_id)

    sync = member.sync(space_id)
    eventually(lambda: _assert(bool(sync.observations())))
    eventually(lambda: _assert(not sync._sync_pending and not sync._sync_active))  # noqa: SLF001
    assert _count(member, space_id) == 0
    assert timeouts
    return owner, member, space_id, failing, timeouts


def test_a_due_tick_pulls_again_when_the_holder_is_ahead_of_the_local_tree(
    peers: Peers, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, member, space_id, failing, _timeouts = _joined_with_a_failed_tree(peers, monkeypatch)
    assert member.runtime is not None
    failing.clear()
    scheduled = _scheduled(member, space_id, run=True)

    member.runtime._maintain_spaces(1000.0)  # noqa: SLF001 - one due anti-entropy tick

    assert scheduled == [owner.node_id]
    eventually(lambda: _assert(_count(member, space_id) == 2 * FOLDERS))
    eventually(lambda: _assert(member.runtime.status(space_id).synced is True))


def test_retries_back_off_while_the_holder_stays_ahead_and_reset_once_caught_up(
    peers: Peers, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, member, space_id, failing, _timeouts = _joined_with_a_failed_tree(peers, monkeypatch)
    runtime = member.runtime
    assert runtime is not None
    scheduled = _scheduled(member, space_id, run=False)
    start = 1000.0
    ticks = [start + 60.0 * step for step in range(40)]
    pulled_at: list[float] = []
    for now in ticks:
        before = len(scheduled)
        runtime._maintain_spaces(now)  # noqa: SLF001
        if len(scheduled) > before:
            pulled_at.append(now)

    gaps = [later - earlier for earlier, later in zip(pulled_at, pulled_at[1:])]
    assert pulled_at[0] == start
    assert gaps == sorted(gaps)
    assert gaps[0] >= CATALOG_RETRY_BASE_SECONDS
    assert max(gaps) <= CATALOG_RETRY_MAX_SECONDS + 60.0
    assert len(pulled_at) < len(ticks) // 2

    failing.clear()

    def caught_up() -> None:
        member.sync(space_id).sync_from(owner.node_id)
        assert _count(member, space_id) == 2 * FOLDERS
        assert runtime.status(space_id).synced is True

    eventually(caught_up)
    scheduled.clear()
    runtime._maintain_spaces(ticks[-1] + 60.0)  # noqa: SLF001
    assert scheduled == []


def test_each_blob_chunk_gets_its_own_fetch_timeout(
    peers: Peers, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, member, space_id, failing, timeouts = _joined_with_a_failed_tree(peers, monkeypatch)
    failing.clear()
    timeouts.clear()

    member.sync(space_id).sync_from(owner.node_id, timeout=0.25)

    assert timeouts
    assert set(timeouts) == {SYNC_BLOB_FETCH_TIMEOUT_SECONDS}


def test_status_does_not_call_an_empty_tree_synced_while_the_holder_is_ahead(
    peers: Peers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner, member, space_id, _failing, _timeouts = _joined_with_a_failed_tree(peers, monkeypatch)
    assert member.runtime is not None

    assert member.runtime.status(space_id).synced is not True


def _assert(condition: bool) -> None:
    assert condition


def _count(peer: Peer, space_id: str) -> int:
    try:
        return len(peer.fs.listdir(space_id, "", recursive=True))
    except OrgFsError as error:
        if error.code != "unknown-doc":
            raise
        return 0
