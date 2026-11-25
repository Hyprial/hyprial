# Decisions:
# - Phase D, package T2: the sync, presence and catch-up tests that used the
#   journal mesh's private buses run on ``StateOrgFsRuntime`` through the
#   contract suite's ``FakeNetwork`` and D2's ``Peer`` / ``Peers``; this module
#   only adds the three transport conditions those tests need and the
#   in-process network does not model:
#   * a per-observer presence verdict (``hide`` / ``show``): the bus still
#     delivers, but the observer's presence says the peer is offline, which is
#     the race the presence tests pin (a commit arrives before liveliness);
#   * a dropped broadcast (``drop_updates``): protocol 3 update puts are lost
#     while every pull still answers, which is a lost fire-and-forget frame;
#   * a transport rebuild (``begin_rebuild`` / ``finish_rebuild``): while a
#     node's callback history is incomplete its presence answers offline for
#     every peer, and finishing runs the session's ``on_rebuild_complete``
#     hooks, the seam ``RuntimeDirectory`` and ``StateSpaceSync`` attach to.
# - ``StatePeers`` is D2's ``Peers`` on this network; nothing here reads a
#   store or a sync's private state.  ``open_state_peers`` isolates the home
#   as ``runtime_state/conftest.py`` does; each test module wraps it in its
#   own ``state_peers`` fixture (several of these directories are at the
#   seven-file limit, so there is no shared conftest).
"""Transport conditions for the state runtime's sync and presence tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.transport import KeySpace

from tests.daemon.orgfs.contracts.support.network import FakeNetwork, FakeSession
from tests.daemon.orgfs.runtime_state.support import Peer, Peers

_UPDATE_PREFIX = f"{KeySpace().prefix}/org/fs3/update/"


class StateSession(FakeSession):
    """A ``FakeSession`` with rebuild hooks and a droppable broadcast."""

    network: "StateNetwork"

    def __init__(self, network: "StateNetwork", node: str) -> None:
        super().__init__(network, node)
        self._hooks: list[Callable[[], object]] = []

    def put(self, key: str, payload: bytes) -> None:
        if self.network.drop_updates and key.startswith(_UPDATE_PREFIX):
            self.network.dropped += 1
            return
        super().put(key, payload)

    def projection(self) -> SimpleNamespace:
        return SimpleNamespace(
            callbacks_complete=self.node not in self.network.rebuilding, closed=False
        )

    def on_rebuild_complete(self, callback: Callable[[], object]) -> Callable[[], None]:
        self._hooks.append(callback)

        def stop() -> None:
            if callback in self._hooks:
                self._hooks.remove(callback)

        return stop

    def run_rebuild_hooks(self) -> None:
        for callback in tuple(self._hooks):
            callback()


class StateNetwork(FakeNetwork):
    """``FakeNetwork`` plus per-observer presence, dropped broadcasts and rebuilds."""

    def __init__(self) -> None:
        super().__init__()
        self.drop_updates = False
        self.dropped = 0
        self.rebuilding: set[str] = set()
        self.hidden: dict[str, set[str]] = {}
        self.sessions: dict[str, StateSession] = {}

    def add_node(self, node: str) -> StateSession:  # type: ignore[override]
        super().add_node(node)
        session = StateSession(self, node)
        self.sessions[node] = session
        return session

    def online_for(self, node: str) -> Callable[[str], bool]:
        reachable = super().online_for(node)

        def supplier_online(peer: str) -> bool:
            if node in self.rebuilding:
                return False
            if peer in self.hidden.get(node, set()):
                return False
            return reachable(peer)

        return supplier_online

    def hide(self, observer: str, peer: str) -> None:
        self.hidden.setdefault(observer, set()).add(peer)

    def show(self, observer: str, peer: str, *, edge: bool = True) -> None:
        self.hidden.get(observer, set()).discard(peer)
        if edge:
            self.emit_online(observer=observer, peer=peer)

    def begin_rebuild(self, node: str) -> None:
        self.rebuilding.add(node)

    def finish_rebuild(self, node: str) -> None:
        self.rebuilding.discard(node)
        self.sessions[node].run_rebuild_hooks()


class StatePeers(Peers):
    """D2's ``Peers`` on a :class:`StateNetwork`."""

    network: StateNetwork

    def __init__(self, root) -> None:
        super().__init__(root)
        self.network = StateNetwork()


def shared_space(peers: Peers, owner: Peer, *members: Peer, name: str = "team", mode: str = "rw") -> str:
    """Create a space on ``owner``, invite and join every member; return its id."""

    space = owner.fs.create_space(name)
    for member in members:
        owner.fs.invite(space.space_id, member.author, mode)
    for member in members:
        assert member.fs.join(space.space_id) == space
    return space.space_id


def open_state_peers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[StatePeers]:
    """Isolated home and a :class:`StatePeers` closed after the test (fixture body)."""

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    group = StatePeers(tmp_path / "nodes")
    try:
        yield group
    finally:
        group.close()


__all__ = ["StateNetwork", "StatePeers", "StateSession", "open_state_peers", "shared_space"]
