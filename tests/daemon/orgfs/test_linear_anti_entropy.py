"""Linear periodic OrgFS anti-entropy and durable leave filtering."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.org.network.service import OrgNetworkService
from hyprial.daemon.impl.org.network.state import clear_left_org, read_left_orgs
from hyprial.daemon.impl.orgfs.composition import directory as directory_composition
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import KeySpace, TransportSample
from tests.support.tailnet_membership import FakeMembershipSidecar, member


_INTERVAL = 60.0


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.setattr(directory_composition, "_directory_sync_jitter", lambda: 0.0)
    monkeypatch.setattr(directory_composition.random, "randrange", lambda _stop: 0)


class _RecordingMesh:
    def __init__(self, space_id: str, calls: list[tuple[str, str]]) -> None:
        self._space_id = space_id
        self._calls = calls

    def schedule_sync_from(self, peer: str) -> bool:
        self._calls.append((self._space_id, peer))
        return True


def _configure_spaces(
    runtime: OrgFsRuntime,
    monkeypatch: pytest.MonkeyPatch,
    *,
    spaces: tuple[SimpleNamespace, ...],
    holders: set[str],
    calls: list[tuple[str, str]],
) -> None:
    monkeypatch.setattr(runtime.facade, "spaces", lambda: spaces)
    runtime._holders = {space.space_id: set(holders) for space in spaces}
    runtime._supplier_online = lambda _peer: True
    meshes = {
        space.space_id: _RecordingMesh(space.space_id, calls) for space in spaces
    }
    monkeypatch.setattr(runtime, "_mesh", meshes.get)


@pytest.mark.parametrize("member_count", [3, 6, 10])
def test_due_round_schedules_one_pull_per_member_per_org_space(
    tmp_path, monkeypatch, member_count
) -> None:
    """T1: a due round is N pulls per space, never N(N-1)."""

    node_ids = tuple(f"node-{index:02d}" for index in range(member_count))
    spaces = (
        SimpleNamespace(space_id="space-directory", name="group-linear"),
        SimpleNamespace(space_id="space-acl", name="group-linear-acl"),
    )
    calls: list[tuple[str, str]] = []
    runtimes: list[OrgFsRuntime] = []
    try:
        for index, node_id in enumerate(node_ids):
            runtime = OrgFsRuntime(
                tmp_path / f"node-{index:02d}",
                node_id=node_id,
                author=f"user:member-{index:02d}@example",
            )
            runtimes.append(runtime)
            _configure_spaces(
                runtime,
                monkeypatch,
                spaces=spaces,
                holders=set(node_ids),
                calls=calls,
            )
            assert runtime.anti_entropy_tick(0.0) == 0

        scheduled = sum(
            runtime.anti_entropy_tick(_INTERVAL) for runtime in runtimes
        )

        assert scheduled == member_count * len(spaces)
        assert len(calls) == member_count * len(spaces)
    finally:
        for runtime in reversed(runtimes):
            runtime.close()


def test_rotation_reaches_last_holder_within_bounded_ticks(
    tmp_path, monkeypatch
) -> None:
    """T3: a row held only by the last rotation peer arrives by tick k."""

    peers = tuple(f"node-peer-{index}" for index in range(4))
    space = SimpleNamespace(space_id="space-directory", name="group-linear")
    calls: list[tuple[str, str]] = []
    runtime = OrgFsRuntime(
        tmp_path / "reader", node_id="node-reader", author="user:reader@example"
    )
    try:
        _configure_spaces(
            runtime,
            monkeypatch,
            spaces=(space,),
            holders={runtime.node_id, *peers},
            calls=calls,
        )
        assert runtime.anti_entropy_tick(0.0) == 0

        arrived_on_tick = None
        for tick in range(1, len(peers) + 1):
            assert runtime.anti_entropy_tick(tick * _INTERVAL) == 1
            if calls[-1][1] == peers[-1]:
                arrived_on_tick = tick
                break

        assert [peer for _space_id, peer in calls] == list(peers)
        assert arrived_on_tick == len(peers)
    finally:
        runtime.close()



def test_a_continuously_online_holder_is_reached_while_others_flap(
    tmp_path, monkeypatch
) -> None:
    """Every continuously-online holder is pulled from within k due rounds.

    ``A`` is online only on even rounds and ``Z`` only on odd ones; ``H`` never
    leaves.  The flapping goes through the real bookkeeping: a liveliness
    delete (``_settle_peer_liveliness``) removes the peer from ``_holders``
    and its return re-announces it.  An index into either holder list then
    shifts every round and yields A, Z, A, Z, never H.
    """

    holders = ("node-a", "node-h", "node-z")
    space = SimpleNamespace(space_id="space-directory", name="group-linear")
    calls: list[tuple[str, str]] = []
    runtime = OrgFsRuntime(
        tmp_path / "reader", node_id="node-reader", author="user:reader@example"
    )
    try:
        _configure_spaces(
            runtime,
            monkeypatch,
            spaces=(space,),
            holders={runtime.node_id, *holders},
            calls=calls,
        )
        runtime._supplier_online = lambda peer: peer in runtime._lively_peers

        def set_online(peer: str, online: bool) -> None:
            key = KeySpace().actor_liveliness(peer)
            if online:
                runtime._settle_peer_liveliness(TransportSample(key, b""))
                runtime._holders[space.space_id].add(peer)  # its re-announce
            else:
                runtime._settle_peer_liveliness(
                    TransportSample(key, b"", kind="delete")
                )

        for peer in holders:
            set_online(peer, True)
        assert runtime.anti_entropy_tick(0.0) == 0

        for tick in range(1, 4 * len(holders) + 1):
            even = (tick - 1) % 2 == 0
            set_online("node-a", even)
            set_online("node-z", not even)
            assert runtime.anti_entropy_tick(tick * _INTERVAL) == 1
            assert "node-h" in runtime._holders[space.space_id]

        picked = [peer for _space_id, peer in calls]
        windows = [picked[start : start + len(holders)] for start in range(len(picked) - 2)]
        assert all("node-h" in window for window in windows), picked
    finally:
        runtime.close()

class _LeaveStore:
    def remove_device(self, *_args, **_kwargs) -> bool:
        return True

    def put_leave_request(self, *_args, **_kwargs) -> None:
        return None


def _leave_org(home, monkeypatch) -> None:
    authority = FakeMembershipSidecar(owner="owner@example", device_id="node-authority")
    authority.seed("space-left-directory", members=[
        authority.local, member("reader@example", "node-reader"),
    ])
    sidecar = authority.for_device("reader@example", "node-reader")
    sidecar.membership_request("roster-fetch", {"tailnetId": "space-left-directory"})
    service = OrgNetworkService(
        fs=object(),
        home=home,
        node_id="node-reader",
        owner="user:reader@example",
        store=_LeaveStore(),
        membership_request=sidecar.membership_request,
    )
    monkeypatch.setattr(service, "_require_allows", lambda **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_require_space",
        lambda _org: SimpleNamespace(space_id="space-left-directory"),
    )
    from tests.support.tailnet_membership import confirm_removal
    result = confirm_removal(service.leave, "group-left")
    assert result["phase"] == "complete"
    assert result["deviceIds"] == ["node-reader"]
    assert authority.tailnets["space-left-directory"]["members"]["node-reader"]["state"] == "tombstone"


def test_left_org_spaces_stay_out_across_restart_and_rejoin_restores_them(
    tmp_path, monkeypatch
) -> None:
    """T4: leave is durable; explicit re-join clears the exclusion."""

    home = tmp_path / "home"
    state_dir = tmp_path / "state"
    spaces = (
        SimpleNamespace(space_id="space-left-directory", name="group-left"),
        SimpleNamespace(space_id="space-left-acl", name="group-left-acl"),
        SimpleNamespace(space_id="space-live-directory", name="group-live"),
        SimpleNamespace(space_id="space-live-acl", name="group-live-acl"),
    )
    _leave_org(home, monkeypatch)
    assert read_left_orgs(home) == ["group-left"]

    def new_runtime() -> tuple[OrgFsRuntime, list[tuple[str, str]]]:
        calls: list[tuple[str, str]] = []
        runtime = OrgFsRuntime(
            state_dir,
            node_id="node-reader",
            author="user:reader@example",
            excluded_orgs=lambda: frozenset(read_left_orgs(home)),
        )
        _configure_spaces(
            runtime,
            monkeypatch,
            spaces=spaces,
            holders={"node-reader", "node-peer"},
            calls=calls,
        )
        return runtime, calls

    first, first_calls = new_runtime()
    try:
        assert first.anti_entropy_tick(0.0) == 0
        assert first.anti_entropy_tick(_INTERVAL) == 2
        assert {space_id for space_id, _peer in first_calls} == {
            "space-live-directory",
            "space-live-acl",
        }
    finally:
        first.close()

    restarted, restarted_calls = new_runtime()
    try:
        assert restarted.anti_entropy_tick(0.0) == 0
        assert restarted.anti_entropy_tick(_INTERVAL) == 2
        assert {space_id for space_id, _peer in restarted_calls} == {
            "space-live-directory",
            "space-live-acl",
        }

        clear_left_org(home, "group-left")
        assert restarted.anti_entropy_tick(2 * _INTERVAL) == 4
        second_round = restarted_calls[2:]
        assert {space_id for space_id, _peer in second_round} == {
            space.space_id for space in spaces
        }
    finally:
        restarted.close()


def test_the_daemon_wires_durable_left_orgs_into_the_runtime(tmp_path) -> None:
    """The composition root, not just the runtime seam, reads left-org state."""

    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.org.network.state import add_left_org

    home = tmp_path / "home"
    home.mkdir()
    app = object.__new__(DaemonApplication)
    app.state_dir = tmp_path / "state"
    app.node_id = "node-reader"
    app.owner = "reader@example"
    app.hyprial_home = home
    app._orgfs_runtime = None
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runtime = app._require_orgfs_runtime()
    try:
        assert runtime._excluded_orgs() == frozenset()
        add_left_org(home, "group-left")
        assert runtime._excluded_orgs() == frozenset({"group-left"})
        clear_left_org(home, "group-left")
        assert runtime._excluded_orgs() == frozenset()
    finally:
        runtime.close()



def test_require_orgfs_runtime_tolerates_a_partially_built_application(
    tmp_path,
) -> None:
    """`_trace_presence_enabled` is set by ``__init__``; a composition root
    assembled without it (``object.__new__(DaemonApplication)``) must still
    wire the runtime, with the instrumentation switch defaulting to OFF."""

    from hyprial.daemon.impl.application import DaemonApplication

    home = tmp_path / "home"
    home.mkdir()
    app = object.__new__(DaemonApplication)
    app.state_dir = tmp_path / "state"
    app.node_id = "node-writer"
    app.owner = "writer@example"
    app.hyprial_home = home
    app._orgfs_runtime = None
    app._log = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    runtime = app._require_orgfs_runtime()
    try:
        assert runtime._announce_instrumentation_enabled is False
        assert runtime._excluded_orgs() == frozenset()
    finally:
        runtime.close()
