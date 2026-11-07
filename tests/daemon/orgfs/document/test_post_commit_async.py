"""OrgFS commits keep IPC and local callbacks independent from broadcast."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from fnmatch import fnmatch
import threading
import time

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import TransportSample
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)
from tests.support.network_owner_context import FunctionPoint, OwnerContextTracer


class _Registration:
    def __init__(self, close) -> None:
        self._close = close

    def close(self) -> None:
        self._close()


class _ControlledSession:
    def __init__(self) -> None:
        self.subscribers: list[tuple[str, object]] = []
        self.handlers: list[tuple[str, object]] = []
        self.block_logs = False
        self.log_entered = threading.Event()
        self.release_logs = threading.Event()
        self.published_logs: list[str] = []

    def put(self, key: str, payload: bytes) -> None:
        if "/org/fs/" in key and "/log/" in key:
            if self.block_logs:
                self.log_entered.set()
                if not self.release_logs.wait(30):
                    raise TimeoutError("test transport remained black-holed")
            self.published_logs.append(key)
        for expression, callback in tuple(self.subscribers):
            if fnmatch(key, expression.replace("**", "*")):
                callback(TransportSample(key, payload))  # type: ignore[operator]

    def subscribe(self, key_expr: str, callback: object) -> _Registration:
        row = (key_expr, callback)
        self.subscribers.append(row)
        return _Registration(lambda: self.subscribers.remove(row))

    def declare_query_handler(self, key_expr: str, handler: object) -> _Registration:
        row = (key_expr, handler)
        self.handlers.append(row)
        return _Registration(lambda: self.handlers.remove(row))

    def query(self, key_expr: str, payload: bytes, **_kwargs: object) -> list[object]:
        replies: list[object] = []
        for expression, handler in tuple(self.handlers):
            if fnmatch(key_expr, expression.replace("**", "*")):
                replies.extend(  # type: ignore[arg-type]
                    TransportSample(key, body)
                    for key, body in handler(key_expr, payload)  # type: ignore[operator]
                )
        return replies


def _eventually(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def _application(tmp_path) -> DaemonApplication:  # type: ignore[no-untyped-def]
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-owner",
        hyprial_home=tmp_path / "home",
        owner="owner@example.com",
        **_deep_module_daemon_dependencies(),
    )
    app._restore_done.set()
    from hyprial.daemon.impl.ipc.ipc_request_owner import IpcRequestOwner

    app._ipc_request_owner = IpcRequestOwner()
    app.handle = (  # type: ignore[method-assign]
        lambda method, params, **kwargs: app._handle_orgfs(
            method, params, trusted_origin=kwargs.get("_trusted_message_origin")
        )
    )
    return app


def test_write_ipc_releases_capacity_while_real_mesh_broadcast_is_black_holed(
    tmp_path,
) -> None:
    app = _application(tmp_path)
    session = _ControlledSession()
    runtime = app._require_orgfs_runtime()
    runtime.bind_transport(session, supplier_online=lambda _node: True)
    try:
        space = app._handle_orgfs("orgfs.create", {"name": "team"})
        created = app._handle_orgfs(
            "orgfs.write",
            {"spaceId": space["space_id"], "path": "note.md", "text": "before"},
        )
        _eventually(
            lambda: runtime.facade._effect_lane is not None  # noqa: SLF001
            and runtime.facade._effect_lane.snapshot().outstanding == 0  # noqa: SLF001
        )
        _eventually(lambda: not tuple(runtime.stores[space["space_id"]].unbroadcast()))
        session.block_logs = True
        session.release_logs.clear()
        session.log_entered.clear()
        callback_ran = threading.Event()
        lock_was_free: list[bool] = []

        def callback(_event: object) -> None:
            acquired = runtime.facade._space_lock(space["space_id"]).acquire(  # noqa: SLF001
                blocking=False
            )
            lock_was_free.append(acquired)
            if acquired:
                runtime.facade._space_lock(space["space_id"]).release()  # noqa: SLF001
            callback_ran.set()

        runtime.facade.watch(space["space_id"], "**", callback)
        tracer = OwnerContextTracer(
            owners=(
                FunctionPoint.from_callable("ipc_request", app._timed_handle),
            ),
            boundaries=(
                FunctionPoint.from_callable("orgfs_transport", session.put),
            ),
        )
        started = time.monotonic()
        with tracer.installed(), ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(
                app._timed_handle,
                "orgfs.write",
                {
                    "spaceId": space["space_id"],
                    "path": "blocked.md",
                    "text": "after",
                },
                _trusted_message_origin="ipc",
            )
            deadline = time.monotonic() + 2
            while not session.log_entered.is_set() and not response.done():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            if response.done():
                response.result()
            try:
                result = response.result(timeout=0.75)
            except FutureTimeout:
                session.release_logs.set()
                response.result(timeout=2)
                pytest.fail("orgfs.write retained its IPC slot for broadcast")
        assert runtime.facade.effect_failure_snapshot() == ()
        assert result["node_id"] != created["node_id"]
        assert time.monotonic() - started < 1.5
        assert callback_ran.wait(1)
        assert lock_was_free == [True]
        _eventually(lambda: app._ipc_request_owner.projection().active == 0)
        assert tuple(runtime.stores[space["space_id"]].unbroadcast())

        session.release_logs.set()
        _eventually(lambda: not tuple(runtime.stores[space["space_id"]].unbroadcast()))
    finally:
        session.release_logs.set()
        app._close()
        app._close_home_lease()


def test_restart_binding_drains_the_durable_commit_journal(tmp_path) -> None:
    state = tmp_path / "state"
    first = OrgFsRuntime(state, node_id="node-owner", author="user:owner")
    space_id = ""
    try:
        space = first.facade.create_space("team")
        space_id = space.space_id
        first.facade.write_text(space_id, "restart.md", "durable")
        assert tuple(first.stores[space_id].unbroadcast())
    finally:
        first.close()

    restarted = OrgFsRuntime(state, node_id="node-owner", author="user:owner")
    session = _ControlledSession()
    try:
        unbroadcast = tuple(restarted.stores[space_id].unbroadcast())
        assert unbroadcast
        replay_record = unbroadcast[-1]
        assert restarted.facade._publication_records(  # noqa: SLF001
            space_id, (replay_record,), broadcast=False
        ) == ()
        replay = restarted.facade._publication_records(  # noqa: SLF001
            space_id, (replay_record,), broadcast=True
        )
        replay_keys = [
            (record.writer, record.seq, record.envelope_bytes) for record in replay
        ]
        assert len(replay_keys) == len(set(replay_keys))
        assert replay_keys.count(
            (replay_record.writer, replay_record.seq, replay_record.envelope_bytes)
        ) == 1
        restarted.bind_transport(session, supplier_online=lambda _node: True)
        _eventually(lambda: bool(session.published_logs))
        _eventually(lambda: not tuple(restarted.stores[space_id].unbroadcast()))
        counts = Counter(session.published_logs)
        assert counts and max(counts.values()) == 1, counts
    finally:
        restarted.close()



def test_a_burst_with_a_black_holed_mesh_keeps_the_network_queue_bounded(
    tmp_path,
) -> None:
    from hyprial.daemon.impl.orgfs.document.model import (
        _FACADE_NETWORK_EFFECT_CAPACITY,
    )

    app = _application(tmp_path)
    session = _ControlledSession()
    runtime = app._require_orgfs_runtime()
    runtime.bind_transport(session, supplier_online=lambda _node: True)
    try:
        space_count = 3 * 64
        assert space_count == 3 * _FACADE_NETWORK_EFFECT_CAPACITY
        session.block_logs = True
        session.published_logs.clear()
        space_ids: list[str] = []
        max_active = 0
        max_in_flight = 0
        facade = runtime.facade
        for index in range(space_count):
            space = facade.create_space(f"team-{index}")
            space_ids.append(space.space_id)
            with facade._lock:  # noqa: SLF001
                active = len(facade._network_active_spaces)  # noqa: SLF001
                in_flight = len(facade._network_effect_spaces)  # noqa: SLF001
            # The pump reserves a space before lane.submit() and rolls the
            # reservation back if the lane refuses it, so an observer may see
            # one in-progress reservation above capacity. The lane itself is
            # the bound that is promised: it never holds more than capacity.
            lane = facade._network_effect_lane  # noqa: SLF001
            if lane is not None:
                assert lane.snapshot().outstanding <= _FACADE_NETWORK_EFFECT_CAPACITY
            assert active <= _FACADE_NETWORK_EFFECT_CAPACITY + 1
            assert in_flight <= _FACADE_NETWORK_EFFECT_CAPACITY + 1
            max_active = max(max_active, active)
            max_in_flight = max(max_in_flight, in_flight)
        assert session.log_entered.wait(2)
        assert max_active >= _FACADE_NETWORK_EFFECT_CAPACITY
        assert max_in_flight >= _FACADE_NETWORK_EFFECT_CAPACITY
        lane = facade._network_effect_lane  # noqa: SLF001
        assert lane is not None
        assert lane.snapshot().outstanding == _FACADE_NETWORK_EFFECT_CAPACITY
        # Quiescent: every refused reservation has been rolled back.
        with facade._lock:  # noqa: SLF001
            assert len(facade._network_active_spaces) == _FACADE_NETWORK_EFFECT_CAPACITY  # noqa: SLF001
            assert len(facade._network_effect_spaces) == _FACADE_NETWORK_EFFECT_CAPACITY  # noqa: SLF001
        expected_logs = sum(
            len(tuple(runtime.stores[space_id].unbroadcast()))
            for space_id in space_ids
        )
        assert expected_logs == space_count

        session.release_logs.set()
        _eventually(
            lambda: all(
                not tuple(runtime.stores[space_id].unbroadcast())
                for space_id in space_ids
            ),
            timeout=20.0,
        )
        counts = Counter(session.published_logs)
        assert len(session.published_logs) == expected_logs
        assert counts and max(counts.values()) == 1, counts
    finally:
        session.release_logs.set()
        app._close()
        app._close_home_lease()


@pytest.mark.parametrize("blocked", [False, True], ids=["unblocked", "blocked"])
def test_forged_protected_alias_rejects_while_broadcast_is_black_holed(
    tmp_path, blocked: bool
) -> None:
    from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
    from hyprial.identity import directory_device_path, protected_directory_doc_id
    from tests.daemon.orgfs.mesh.device_rows.test_round4 import _device, _receive
    from tests.daemon.orgfs.mesh.device_rows.test_round6 import _tree_envelope

    app = _application(tmp_path)
    session = _ControlledSession()
    runtime = app._require_orgfs_runtime()
    runtime.bind_transport(session, supplier_online=lambda _node: True)
    try:
        space = app._handle_orgfs("orgfs.create", {"name": "group-acme"})
        space_id = space["space_id"]
        _eventually(lambda: not tuple(runtime.stores[space_id].unbroadcast()))
        session.block_logs = blocked
        session.release_logs.clear()
        session.log_entered.clear()
        owner = app.owner
        # Resolve the supplier independently of the forged protected-document author.
        runtime._meshes[space_id]._supplier_owner = {runtime.node_id: f"user:{owner}"}.get
        row = _device(owner, "alias-link", address="tcp://10.0.0.7:7447#alias")
        store = OrgFsDirectoryStore(runtime.facade)
        store.ensure_org_directory_roots("group-acme")
        store.put_device("group-acme", row)
        if blocked:
            assert session.log_entered.wait(2)
            assert tuple(runtime.stores[space_id].unbroadcast())
        else:
            _eventually(lambda: not tuple(runtime.stores[space_id].unbroadcast()))
        protected_id = protected_directory_doc_id(
            space_id,
            directory_device_path(owner, row.device_id),
            space_owner=f"user:{owner}",
        )
        assert protected_id is not None
        forged = _tree_envelope(
            runtime,
            space_id,
            author=owner,
            actor="alias-link",
            node_id="protected-alias",
            parent="root",
            name="public-alias.json",
            doc_id=protected_id,
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(
                _receive, runtime, space_id, forged, runtime.node_id
            )
            try:
                result = response.result(timeout=0.75)
            except FutureTimeout:
                session.release_logs.set()
                response.result(timeout=2)
                pytest.fail("owner admission waited for broadcast meta")
        assert (result.status, result.code) == ("rejected", "not-a-member")
        _eventually(lambda: app._ipc_request_owner.projection().active == 0)
    finally:
        session.release_logs.set()
        app._close()
        app._close_home_lease()
