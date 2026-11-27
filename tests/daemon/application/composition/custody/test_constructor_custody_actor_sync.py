"""Isolated acquisition-failure controls for the dev/actor merge seams.

The application function is extracted verbatim because unrelated conflicts in
this isolated worktree prevent importing the full daemon module. Production
imports cover the two actor constructors directly.
"""

from __future__ import annotations

import ast
import os
import textwrap
import threading
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import hyprial.daemon.impl.autoupdate.actor as autoupdate_actor
import hyprial.daemon.impl.transport.session_actor as session_actor
from hyprial.kernel.impl.actor_runtime import ActorSpec
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication
from tests.daemon.startup.test_startup_event_reconciliation import _native_application_source


class _Scheduler:
    def __init__(self) -> None:
        self.shutdowns = []
        self.cancelled = []
        self.drain = True

    def shutdown(self, timeout):  # type: ignore[no-untyped-def]
        self.shutdowns.append(timeout)
        return self.drain

    def cancel(self, name):  # type: ignore[no-untyped-def]
        self.cancelled.append(name)


class _Lane:
    def __init__(self) -> None:
        self.closes = []

    def close(self, timeout=1.0):  # type: ignore[no-untyped-def]
        self.closes.append(timeout)
        return True


class _Cell:
    def __init__(self) -> None:
        self.stops = []

    def tell(self, _command):  # type: ignore[no-untyped-def]
        return object()

    def snapshot(self):  # type: ignore[no-untyped-def]
        return SimpleNamespace(queued=0, in_flight=0)

    def stop(self, timeout=1.0):  # type: ignore[no-untyped-def]
        self.stops.append(timeout)
        return True


class _Host:
    def __init__(self, lane):  # type: ignore[no-untyped-def]
        self.lane = lane
        self.cells = []

    def spawn(self, _spec):  # type: ignore[no-untyped-def]
        cell = _Cell()
        self.cells.append(cell)
        return cell

    def effect_lane(self, **_kwargs):  # type: ignore[no-untyped-def]
        if isinstance(self.lane, BaseException):
            raise self.lane
        return self.lane


def _source_root() -> Path:
    return Path(
        os.environ.get(
            "HYPRIAL_CONSTRUCTOR_SOURCE_ROOT", Path(__file__).resolve().parents[5]
        )
    )


@pytest.mark.parametrize("failed_lane", ["transport-data", "transport-lifetime"])
def test_transport_authority_constructor_rolls_back_each_allocation_stage(
    monkeypatch: pytest.MonkeyPatch, failed_lane: str
) -> None:
    scheduler = _Scheduler()
    allocated: list[tuple[str, _Lane]] = []

    def lane(*, name, **_kwargs):  # type: ignore[no-untyped-def]
        if name == failed_lane:
            raise RuntimeError(f"injected {name} allocation failure")
        result = _Lane()
        allocated.append((name, result))
        return result

    class _Cell:
        def __init__(self) -> None:
            self.drains: list[float] = []

        def tell(self, _command: object) -> object:
            return None

        def drain(self, timeout: float) -> object:
            self.drains.append(timeout)
            return SimpleNamespace(complete=True)

        def stop(self, timeout: float) -> bool:
            raise AssertionError("constructor rollback must drain, not abort")

    class _Host:
        def __init__(self) -> None:
            self.cells: list[_Cell] = []

        def spawn(self, _spec: object) -> _Cell:
            cell = _Cell()
            self.cells.append(cell)
            return cell

        def effect_lane(self, **kwargs):  # type: ignore[no-untyped-def]
            return lane(**kwargs)

    host = _Host()
    # Self-built scheduler: the rollback must shut it down.  The factory lives
    # in the composition root and is imported lazily by the constructor.
    monkeypatch.setattr(
        "hyprial.daemon.impl.composition.actors.create_generation_scheduler",
        lambda: scheduler,
    )
    native = SimpleNamespace(config=object())

    with pytest.raises(
        RuntimeError, match=f"injected {failed_lane} allocation failure"
    ):
        session_actor.TransportSessionAuthority(native, actor_host=host)

    assert len(host.cells) == 1
    assert len(host.cells[0].drains) == 1
    assert scheduler.shutdowns
    assert all(effect.closes == [1.0] for _, effect in allocated)
    assert [name for name, _effect in allocated] == (
        [] if failed_lane == "transport-data" else ["transport-data"]
    )


def _open_transport_from_isolated_source(**dependencies):  # type: ignore[no-untyped-def]
    source = (_source_root() / "src/hyprial/daemon/impl/application/netendpoints/endpoints.py").read_text(
        encoding="utf-8"
    )
    node = next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name == "_open_transport")
    function = "from __future__ import annotations\n" + ast.get_source_segment(source, node)
    ast.parse(function)
    namespace = dict(dependencies)
    exec(compile(function, "application.py:_open_transport", "exec"), namespace)
    return namespace["_open_transport"]


@dataclass(frozen=True)
class _Config:
    listen: tuple[str, ...]


def test_wrapper_failure_closes_native_without_derived_fallback() -> None:
    native_sessions = []
    error = RuntimeError("injected authority construction failure")

    class Native:
        def __init__(self, config, *, event_sink=None, actor_host=None):  # type: ignore[no-untyped-def]
            # The transport is opened with an optional diagnostic sink and the
            # process-wide host; the fake mirrors the real constructor.
            self.config = config
            self.event_sink = event_sink
            self.actor_host = actor_host
            self.closed = False
            native_sessions.append(self)

        def close(self) -> None:
            self.closed = True

    def fail_owner(_native, *, actor_host=None):  # type: ignore[no-untyped-def]
        raise error

    endpoint = "tcp/100.64.20.1:7447"
    open_transport = _open_transport_from_isolated_source(
        ZenohConfig=_Config,
        TransportSessionAuthority=fail_owner,
        ZenohTransport=Native,
        DEFAULT_POLICIES={"external-io": SimpleNamespace(max_restarts=2)},
        EXTERNAL_IO="external-io",
        replace=replace,
        _reserve_loopback_endpoint=lambda: "tcp/127.0.0.1:9000",
    )

    with pytest.raises(RuntimeError) as caught:
        open_transport(_Config((endpoint,)), (), derived_listen=endpoint)
    assert caught.value is error
    assert len(native_sessions) == 1
    assert native_sessions[0].closed


def test_native_bind_failure_still_drops_only_derived_listener() -> None:
    attempts = []
    endpoint = "tcp/100.64.20.1:7447"

    class Native:
        def __init__(self, config, *, event_sink=None, actor_host=None):  # type: ignore[no-untyped-def]
            self.event_sink = event_sink
            self.actor_host = actor_host
            attempts.append(config.listen)
            if endpoint in config.listen:
                raise OSError(f"bind failed: {endpoint}")
            self.config = config

    open_transport = _open_transport_from_isolated_source(
        ZenohConfig=_Config,
        TransportSessionAuthority=lambda native, *, actor_host=None: native,
        ZenohTransport=Native,
        DEFAULT_POLICIES={"external-io": SimpleNamespace(max_restarts=2)},
        EXTERNAL_IO="external-io",
        replace=replace,
        _reserve_loopback_endpoint=lambda: "tcp/127.0.0.1:9000",
    )
    owner, forwarding, detail = open_transport(
        _Config((endpoint,)), (), derived_listen=endpoint
    )
    assert attempts == [(endpoint,), ()]
    assert owner.config.listen == ()
    assert forwarding == ()
    assert endpoint in detail


def test_autoupdate_constructor_rollback_and_stop_stages_are_truthful(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scheduler = _Scheduler()
    failing_host = _Host(RuntimeError("lane failed"))
    monkeypatch.setattr(
        autoupdate_actor, "InProcessAutoUpdateScheduler",
        lambda **_kwargs: SimpleNamespace(_hyprial_home=tmp_path),
    )

    with pytest.raises(RuntimeError, match="lane failed"):
        autoupdate_actor.AutoUpdateAuthority(
            actor_host=failing_host, scheduler=scheduler
        )
    assert len(failing_host.cells) == 1
    assert len(failing_host.cells[0].stops) == 1
    assert scheduler.shutdowns == []

    lane = _Lane()
    host = _Host(lane)
    actor = autoupdate_actor.AutoUpdateAuthority(actor_host=host, scheduler=scheduler)
    assert len(host.cells) == 1
    original_close = actor._effects.close
    monkeypatch.setattr(actor._effects, "close", lambda _timeout: False)
    assert actor.stop(timeout=0.02) is False
    assert actor.stop_incomplete_stage == "effects"
    assert lane.closes == []
    assert host.cells[0].stops == []
    monkeypatch.setattr(actor._effects, "close", original_close)
    assert actor.stop(timeout=0.02) is True
    assert lane.closes
    assert len(host.cells[0].stops) == 1
    assert scheduler.shutdowns == []


def test_session_route_constructor_failure_stops_its_actor() -> None:
    source = (
        _source_root() / "src/hyprial/daemon/impl/network/routing/session_route_coordinator.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    owner = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "SessionRouteCoordinator"
    )
    constructor = next(
        item
        for item in owner.body
        if isinstance(item, ast.FunctionDef) and item.name == "__init__"
    )
    body = ast.get_source_segment(source, constructor)
    assert body is not None
    program = (
        "from __future__ import annotations\n"
        "class Owner:\n"
        "    def _handle_command(self, _command): pass\n"
        "    def _undelivered(self, _command, _reason): pass\n"
        "    def _execute(self, _request): pass\n" + textwrap.indent(body, "    ")
    )
    class _Cell:
        def __init__(self) -> None:
            self.drains: list[float] = []

        def tell(self, _command: object) -> object:
            return None

        def drain(self, timeout: float) -> object:
            self.drains.append(timeout)
            return SimpleNamespace(complete=True)

        def stop(self, timeout: float) -> bool:
            raise AssertionError("constructor rollback must drain, not abort")

    class _Host:
        def __init__(self) -> None:
            self.cells: list[_Cell] = []

        def spawn(self, _spec: object) -> _Cell:
            cell = _Cell()
            self.cells.append(cell)
            return cell

        def effect_lane(self, **_kwargs: object) -> object:
            raise RuntimeError("injected session-route lane failure")

    host = _Host()

    shutdowns: list[float] = []

    class _Custody:
        def __init__(self, _host: object = None, *, owner: str) -> None:
            assert owner == "network"
            self.host = host

        def shutdown(self, timeout: float) -> bool:
            shutdowns.append(timeout)
            return True

    namespace = {
        "ActorHostCustody": _Custody,
        "ActorSpec": ActorSpec,
        "TYPE_CHECKING": False,
        "threading": threading,
        "deque": deque,
    }
    exec(compile(program, "session_route_coordinator.py:Owner", "exec"), namespace)
    with pytest.raises(RuntimeError, match="injected session-route lane failure"):
        namespace["Owner"](lambda _request: None)
    # The invariant is unchanged: the actor the constructor started is stopped
    # during the rollback, and nothing is left running.
    assert len(host.cells) == 1
    assert len(host.cells[0].drains) == 1
    # The rollback also releases the host custody it created: without this the
    # standalone path would leak the private registry.
    assert shutdowns == [5.0]


@pytest.mark.parametrize("fail_at", ["bind", "runtime_start"])
def test_recipient_wake_startup_failure_runs_existing_rollback(fail_at: str) -> None:
    source = _native_application_source()
    begin = source.index(
        "        recipient_wakes: RecipientWakeCoordinator | None = None"
    )
    end = source.index("        self._fetch_receipt_publisher = FetchReceiptPublisher", begin)
    block = textwrap.dedent(source[begin:end])
    program = "from __future__ import annotations\ndef exercise():\n" + textwrap.indent(
        block, "    "
    )
    ast.parse(program)
    events: list[str] = []
    failure = RuntimeError(f"injected {fail_at} failure")

    class Closeable:
        def __init__(self, name: str) -> None:
            self.name = name

        def stop(self) -> None:
            events.append(f"{self.name}.stop")

        def start(self) -> None:
            events.append(f"{self.name}.start")
            raise failure

        def close(self, *_args: object) -> bool:
            events.append(f"{self.name}.close")
            return True

    class Wakes:
        def __init__(self, *_args: object) -> None:
            events.append("wakes.created")

        def submit(self, _transition: object) -> None:
            return None

        def close(self, _timeout: float) -> bool:
            events.append("wakes.close")
            return True

    class Directory(Closeable):
        def presence_projection(self) -> None:
            return None

        def observe_actor_online(self, _observer: object):  # type: ignore[no-untyped-def]
            return lambda: None

        def bind_actor_online(self, _observer: object):  # type: ignore[no-untyped-def]
            events.append("directory.bind")
            if fail_at == "bind":
                raise failure
            return lambda: events.append("observer.stop")

    closeables = {
        name: Closeable(name)
        for name in (
            "runtime",
            "harnesses",
            "turn_hooks",
            "delivery_hooks",
            "adapters",
            "lark_events",
            "org_endpoint",
            "user_endpoint",
            "status_endpoint",
            "endpoint",
            "inbox",
            "transport",
        )
    }
    namespace = {
        **closeables,
        "RecipientWakeCoordinator": Wakes,
        "directory": Directory("directory"),
        "inbox_coordinator": object(),
        "shared_inbox_events": object(),
        "presence": object(),
        "lark_client": object(),
        "user_delivery": object(),
        "self": SimpleNamespace(),
        "threading": threading,
        "PortAdmission": PortAdmission,
    }
    namespace["graph"] = SimpleNamespace(
        **closeables, directory=namespace["directory"],
        inbox_coordinator=namespace["inbox_coordinator"],
        shared_inbox_events=namespace["shared_inbox_events"],
        presence=namespace["presence"], lark_client=namespace["lark_client"],
    )
    exec(compile(program, "application.py:recipient-wake-startup", "exec"), namespace)
    with pytest.raises(RuntimeError) as caught:
        namespace["exercise"]()
    assert caught.value is failure
    assert events[:2] == ["wakes.created", "directory.bind"]
    if fail_at == "runtime_start":
        assert "observer.stop" in events
    # run() finally calls the canonical _close owner order. Construction
    # failure keeps the refs reachable and stops online callback admission.
    assert "wakes.close" not in events
    assert "directory.close" not in events
    assert "transport.close" not in events
    assert "runtime.stop" not in events
    application = namespace["self"]
    assert application._runtime is closeables["runtime"]
    assert application._inbox is closeables["inbox"]
    assert application._directory is namespace["directory"]
    assert application._transport is closeables["transport"]

    tree = ast.parse(source)
    daemon_class = next(
        item for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "DaemonApplication"
    )
    close_node = next(
        item for item in daemon_class.body
        if isinstance(item, ast.FunctionDef) and item.name == "_close"
    )
    close_source = textwrap.dedent(ast.get_source_segment(source, close_node))
    globals_for_close = dict(DaemonApplication._close.__globals__)
    exec(
        compile("from __future__ import annotations\n" + close_source, "candidate:_close", "exec"),
        globals_for_close,
    )
    for node in ast.walk(close_node):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and not hasattr(application, node.attr)
        ):
            setattr(application, node.attr, None)

    class Complete:
        def close(self, *_args: object, **_kwargs: object) -> bool:
            return True

        def stop(self, *_args: object, **_kwargs: object) -> bool:
            return True

        def shutdown(self, *_args: object, **_kwargs: object) -> bool:
            return True

        def detach(self) -> None:
            return None

    complete = Complete()
    for name in (
        "_autoupdate", "_maintenance_scheduler", "_restore_policy",
        "_blocking_failures", "_restore_policy_finalizer",
        "_blocking_failures_finalizer", "user_adapters", "_route_gateway_cache",
        "_state_persistence", "_maintenance_watchdog",
        "_gateway_logger", "_logger", "_status_refresh",
    ):
        setattr(application, name, complete)
    application._registry_management_lock = threading.Lock()
    DaemonApplication._init_adapter_operations(application)
    application._close_adapter_operations = DaemonApplication._close_adapter_operations.__get__(application)
    application._degraded_workflow_handles = []
    application._lifecycle_domain_ports = ()
    application._outbound_gateway_owners = set()
    application._maintenance_generation = 0
    application.socket_path = _source_root() / "absent.sock"
    application.state_dir = _source_root()
    for name in (
        "_quota_watchdog", "_inbox_watchdog", "_actor_token", "_duplicate_watch",
    ):
        setattr(application, name, None)
    for name in (
        "_arm_shutdown_stall_dump_if_owned", "_log_trace", "_join_restore_thread",
        "_close_ipc_clients", "_flush_agent_activity", "_close_home_lease", "_stop_grant_recheck",
        "_close_harnesses_before_agent_domains",
    ):
        setattr(application, name, lambda *_args, **_kwargs: None)
    globals_for_close["_close"](application)
    assert application._lark_authorization_closed is True
    assert "wakes.close" in events
    assert "directory.close" in events
    assert "transport.close" in events
    assert "runtime.stop" in events
