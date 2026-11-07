"""Composition owners remain reachable until canonical shutdown settles."""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl import application as application_module
from tests.daemon.startup.test_startup_event_reconciliation import _native_application_source


def _candidate_source() -> str:
    explicit = os.environ.get("HYPRIAL_CONSTRUCTOR_SOURCE")
    return Path(explicit).read_text(encoding="utf-8") if explicit else _native_application_source()


def _application_path() -> Path:
    root = Path(
        os.environ.get(
            "HYPRIAL_CONSTRUCTOR_SOURCE_ROOT", Path(__file__).resolve().parents[5]
        )
    )
    return Path(os.environ.get("HYPRIAL_CONSTRUCTOR_SOURCE", root / "src/hyprial/daemon/impl/application/__init__.py"))


def _runtime_path() -> Path:
    root = Path(
        os.environ.get(
            "HYPRIAL_CONSTRUCTOR_SOURCE_ROOT", Path(__file__).resolve().parents[5]
        )
    )
    return Path(os.environ.get("HYPRIAL_RUNTIME_SOURCE", root / "src/hyprial/daemon/impl/runtime/bridge.py"))


def _method(source: str, name: str):
    tree = ast.parse(source)
    owner = next(
        item for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "DaemonApplication"
    )
    node = next(
        item for item in owner.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    body = textwrap.dedent(ast.get_source_segment(source, node))
    namespace = dict(getattr(application_module.DaemonApplication, name).__globals__)
    exec(compile("from __future__ import annotations\n" + body, f"candidate:{name}", "exec"), namespace)
    return namespace[name], node


class Owner:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.close_count = 0

    def close(self, *_args: object, **_kwargs: object) -> bool:
        self.close_count += 1
        self.events.append(f"{self.name}.close")
        return True

    def stop(self, *_args: object, **_kwargs: object) -> bool:
        self.events.append(f"{self.name}.stop")
        return True

    def shutdown(self, *_args: object, **_kwargs: object) -> bool:
        self.events.append(f"{self.name}.shutdown")
        return True

    def detach(self) -> None:
        return None


class Runtime(Owner):
    def __init__(self, events: list[str], stop_results: tuple[object, ...]) -> None:
        super().__init__("runtime", events)
        self.stop_results = list(stop_results)
        self.stop_count = 0
        self.failure = RuntimeError("synthetic runtime start failure")

    def start(self) -> None:
        self.events.append("runtime.start")
        raise self.failure

    def stop(self) -> object:
        self.stop_count += 1
        self.events.append(f"runtime.stop.{self.stop_count}")
        result = self.stop_results.pop(0) if self.stop_results else None
        if isinstance(result, BaseException):
            raise result
        return result


class Harnesses(Owner):
    def __init__(self, events: list[str], drain_results: tuple[bool, ...]) -> None:
        super().__init__("harnesses", events)
        self.drain_results = list(drain_results)
        self.drain_complete = True
        self.stop_count = 0

    def stop(self, *_args: object) -> None:
        self.stop_count += 1
        self.events.append(f"harnesses.stop.{self.stop_count}")
        self.drain_complete = self.drain_results.pop(0) if self.drain_results else True


class Wakes(Owner):
    def __init__(self, events: list[str], close_results: tuple[bool, ...]) -> None:
        super().__init__("wakes", events)
        self.close_results = list(close_results)
        self.submitted = 0

    def submit(self, _transition: object) -> PortAdmission:
        self.submitted += 1
        self.events.append("wakes.accepted")
        return PortAdmission.ACCEPTED

    def close(self, *_args: object) -> bool:
        self.close_count += 1
        self.events.append(f"wakes.close.{self.close_count}")
        return self.close_results.pop(0) if self.close_results else True


class Directory(Owner):
    def __init__(self, events: list[str]) -> None:
        super().__init__("directory", events)
        self.observer = None

    def presence_projection(self) -> object:
        return object()

    def observe_actor_online(self, observer):
        # Independent supplier subscription; do not replace the inbox binding.
        return lambda: None

    def bind_actor_online(self, observer):
        self.observer = observer
        self.events.append("directory.bind")
        assert observer(SimpleNamespace(actor="agent:o:node:online")) is PortAdmission.ACCEPTED
        return lambda: self.events.append("observer.stop")


class Scenario:
    def __init__(
        self,
        source: str,
        *,
        runtime_stop: tuple[object, ...] = (None,),
        harness_drains: tuple[bool, ...] = (True,),
        wake_closes: tuple[bool, ...] = (True,),
    ) -> None:
        self.source = source
        self.events: list[str] = []
        self.runtime = Runtime(self.events, runtime_stop)
        self.harnesses = Harnesses(self.events, harness_drains)
        self.wakes = Wakes(self.events, wake_closes)
        self.directory = Directory(self.events)
        self.inbox_coordinator = object()
        self.presence = object()
        self.owners = {
            name: Owner(name, self.events)
            for name in (
                "delivery_hooks", "adapters", "lark_events",
                "org_endpoint", "user_endpoint", "status_endpoint", "endpoint",
                "lark_client", "user_delivery", "transport", "inbox",
            )
        }
        self.app = SimpleNamespace()
        begin = source.index("        recipient_wakes: RecipientWakeCoordinator | None = None")
        end = source.index("        self._fetch_receipt_publisher = FetchReceiptPublisher", begin)
        block = textwrap.dedent(source[begin:end])
        program = "from __future__ import annotations\ndef exercise():\n" + textwrap.indent(block, "    ")
        ast.parse(program)
        namespace = {
            **self.owners,
            "runtime": self.runtime,
            "harnesses": self.harnesses,
            "directory": self.directory,
            "presence": self.presence,
            "RecipientWakeCoordinator": lambda *_args: self.wakes,
            "inbox_coordinator": self.inbox_coordinator,
            "shared_inbox_events": object(),
            "self": self.app,
            "threading": threading,
            "PortAdmission": PortAdmission,
        }
        namespace["graph"] = SimpleNamespace(
            **self.owners, harnesses=self.harnesses, directory=self.directory,
            presence=self.presence, inbox_coordinator=self.inbox_coordinator,
            shared_inbox_events=namespace["shared_inbox_events"],
        )
        exec(compile(program, "candidate:startup", "exec"), namespace)
        self.exercise = namespace["exercise"]

    def prepare_close(self) -> None:
        close_method, close_node = _method(self.source, "_close")
        self.close = lambda: close_method(self.app)
        for node in ast.walk(close_node):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and not hasattr(self.app, node.attr)
            ):
                setattr(self.app, node.attr, None)
        complete = Owner("complete", self.events)
        for name in (
            "_autoupdate", "_maintenance_scheduler",
            "_restore_policy", "_blocking_failures", "_restore_policy_finalizer",
            "_blocking_failures_finalizer", "user_adapters", "_route_gateway_cache",
            "user_profiles", "_transfer_p2", "_state_persistence", "_maintenance_watchdog",
            "_gateway_logger", "_logger", "_status_refresh",
        ):
            setattr(self.app, name, complete)
        self.app._registry_management_lock = threading.Lock()
        self.app._degraded_workflow_handles = []
        self.app._lifecycle_domain_ports = ()
        self.app._outbound_gateway_owners = set()
        self.app._maintenance_generation = 0
        self.app.socket_path = _application_path().parent / "absent.sock"
        self.app.state_dir = self.app.socket_path.parent
        for name in (
            "_quota_watchdog", "_inbox_watchdog", "_duplicate_watch", "_actor_token",
        ):
            setattr(self.app, name, None)
        for name in (
            "_arm_shutdown_stall_dump_if_owned", "_log_trace", "_join_restore_thread",
            "_close_ipc_clients", "_flush_agent_activity",
        ):
            setattr(self.app, name, lambda *_args, **_kwargs: None)
        harness_close, _ = _method(self.source, "_close_harnesses_before_agent_domains")
        self.app._close_harnesses_before_agent_domains = lambda: harness_close(self.app)
        self.app._provider_auth = None
        self.app._agent_session_domains = None


def test_wake_false_retains_dependencies_until_later_close_true() -> None:
    scenario = Scenario(_candidate_source(), wake_closes=(False, True))
    with pytest.raises(RuntimeError, match="synthetic runtime start failure") as caught:
        scenario.exercise()
    assert caught.value is scenario.runtime.failure
    assert scenario.runtime.stop_count == 0
    assert scenario.wakes.submitted == 1
    assert scenario.wakes.close_count == 0
    assert scenario.directory.observer(SimpleNamespace(actor="agent:o:node:late")) is PortAdmission.CLOSING
    assert scenario.wakes.submitted == 1
    scenario.prepare_close()
    with pytest.raises(RuntimeError, match="recipient wakes did not settle"):
        scenario.close()
    assert scenario.app._recipient_wakes is scenario.wakes
    assert scenario.app._inbox is scenario.owners["inbox"]
    assert scenario.app._directory is scenario.directory
    assert scenario.app._transport is scenario.owners["transport"]
    assert "directory.close" not in scenario.events
    assert "inbox.shutdown" not in scenario.events
    assert "transport.close" not in scenario.events
    scenario.close()
    assert scenario.wakes.close_count == 2
    assert scenario.events.count("directory.close") == 1
    assert scenario.events.count("inbox.shutdown") == 1
    assert scenario.events.count("transport.close") == 1


def test_runtime_false_and_exception_retain_all_owners_for_retry() -> None:
    for first_failure in (False, RuntimeError("synthetic runtime stop error")):
        scenario = Scenario(_candidate_source(), runtime_stop=(first_failure, None))
        with pytest.raises(RuntimeError, match="synthetic runtime start failure") as caught:
            scenario.exercise()
        assert caught.value is scenario.runtime.failure
        assert scenario.runtime.stop_count == 0
        assert scenario.app._runtime is scenario.runtime
        assert scenario.app._recipient_wakes is scenario.wakes
        for name, owner in scenario.owners.items():
            assert getattr(scenario.app, f"_{name}") is owner
            assert owner.close_count == 0
        scenario.prepare_close()
        with pytest.raises(RuntimeError, match="dispatch runtime did not drain"):
            scenario.close()
        assert scenario.app._runtime is scenario.runtime
        assert scenario.app._recipient_wakes is scenario.wakes
        assert "inbox.shutdown" not in scenario.events
        assert "directory.close" not in scenario.events
        assert "transport.close" not in scenario.events
        scenario.close()
        assert scenario.runtime.stop_count == 2
        assert scenario.events.count("directory.close") == 1
        assert scenario.events.count("inbox.shutdown") == 1
        assert scenario.events.count("transport.close") == 1


def test_harness_incomplete_retains_wake_and_transport_until_retry() -> None:
    scenario = Scenario(_candidate_source(), harness_drains=(False, True))
    with pytest.raises(RuntimeError, match="synthetic runtime start failure"):
        scenario.exercise()
    scenario.prepare_close()
    with pytest.raises(RuntimeError, match="harness domain did not drain"):
        scenario.close()
    assert scenario.app._harnesses is scenario.harnesses
    assert scenario.app._recipient_wakes is scenario.wakes
    assert scenario.app._inbox is scenario.owners["inbox"]
    assert "inbox.shutdown" not in scenario.events
    assert "transport.close" not in scenario.events
    scenario.close()
    assert scenario.harnesses.stop_count == 2
    assert scenario.events.count("directory.close") == 1
    assert scenario.events.count("inbox.shutdown") == 1
    assert scenario.events.count("transport.close") == 1


def test_partial_runtime_owner_allocation_is_drained_by_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _runtime_path()
    spec = importlib.util.spec_from_file_location("hyprial.daemon.impl.runtime.runtime_candidate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        closed: list[bool] = []

        class DispatchState:
            def close(self, _timeout: float) -> bool:
                closed.append(True)
                return True

        class Desired:
            def load(self):
                return SimpleNamespace(as_mailbox=False, interactive_sessions=())

        monkeypatch.setattr(module, "DispatchStateAuthority", DispatchState)
        monkeypatch.setattr(module, "DispatchOwnedMap", lambda _owner, _table: {})
        monkeypatch.setattr(module, "DispatchOwnedSet", lambda _owner, _table: set())
        def fail_offers(_dispatch):
            raise RuntimeError("synthetic dispatch offer construction failure")
        monkeypatch.setattr(module, "DispatchOfferCoordinator", fail_offers)
        bridge = module.DaemonEventBridge(
            state_dir=tmp_path,
            node_id="node",
            desired_state=Desired(),
            transport=SimpleNamespace(),
            inbox=SimpleNamespace(),
            harnesses=SimpleNamespace(dispatch=lambda *_args: None),
            actor_mode=True,
        )
        with pytest.raises(RuntimeError, match="synthetic dispatch offer construction failure"):
            bridge.start()
        assert bridge._dispatch_state is not None
        bridge.stop()
        assert closed == [True]
        assert bridge._dispatch_state is None
        assert bridge._startup_in_progress is False
    finally:
        sys.modules.pop(spec.name, None)


def test_partial_dispatch_state_failed_close_remains_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _runtime_path()
    spec = importlib.util.spec_from_file_location("hyprial.daemon.impl.runtime.runtime_candidate_retry", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        close_attempts: list[int] = []

        class DispatchState:
            def close(self, _timeout: float) -> bool:
                close_attempts.append(len(close_attempts) + 1)
                return len(close_attempts) > 1

        class Desired:
            def load(self):
                return SimpleNamespace(as_mailbox=False, interactive_sessions=())

        monkeypatch.setattr(module, "DispatchStateAuthority", DispatchState)
        monkeypatch.setattr(module, "DispatchOwnedMap", lambda _owner, _table: {})
        monkeypatch.setattr(module, "DispatchOwnedSet", lambda _owner, _table: set())
        monkeypatch.setattr(
            module,
            "DispatchOfferCoordinator",
            lambda _dispatch: (_ for _ in ()).throw(
                RuntimeError("synthetic dispatch offer construction failure")
            ),
        )
        bridge = module.DaemonEventBridge(
            state_dir=tmp_path,
            node_id="node",
            desired_state=Desired(),
            transport=SimpleNamespace(),
            inbox=SimpleNamespace(),
            harnesses=SimpleNamespace(dispatch=lambda *_args: None),
            actor_mode=True,
        )
        with pytest.raises(RuntimeError, match="synthetic dispatch offer construction failure"):
            bridge.start()
        with pytest.raises(ExceptionGroup, match="daemon shutdown failed"):
            bridge.stop()
        assert close_attempts == [1]
        assert bridge._dispatch_state is not None
        assert bridge._startup_in_progress is True
        bridge.stop()
        assert close_attempts == [1, 2]
        assert bridge._dispatch_state is None
        assert bridge._startup_in_progress is False
    finally:
        sys.modules.pop(spec.name, None)


def test_registration_close_failure_keeps_dispatch_state_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _runtime_path()
    spec = importlib.util.spec_from_file_location("hyprial.daemon.impl.runtime.runtime_candidate_registration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        closed: list[str] = []

        class DispatchState:
            def close(self, _timeout: float) -> bool:
                closed.append("dispatch-state")
                return True

        class Desired:
            def load(self):
                return SimpleNamespace(as_mailbox=False, interactive_sessions=())

        class Registration:
            attempts = 0

            def close(self, *, reason: str, initiator: str) -> None:
                assert reason == "daemon-stop"
                assert initiator == "daemon-runtime"
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("synthetic registration close failure")
                closed.append("registration")

        monkeypatch.setattr(module, "DispatchStateAuthority", DispatchState)
        monkeypatch.setattr(module, "DispatchOwnedMap", lambda _owner, _table: {})
        monkeypatch.setattr(module, "DispatchOwnedSet", lambda _owner, _table: set())

        def fail_offers(_dispatch):
            raise RuntimeError("synthetic dispatch offer construction failure")

        monkeypatch.setattr(module, "DispatchOfferCoordinator", fail_offers)
        bridge = module.DaemonEventBridge(
            state_dir=tmp_path,
            node_id="node",
            desired_state=Desired(),
            transport=SimpleNamespace(),
            inbox=SimpleNamespace(),
            harnesses=SimpleNamespace(dispatch=lambda *_args: None),
            actor_mode=True,
        )
        with pytest.raises(RuntimeError, match="synthetic dispatch offer construction failure"):
            bridge.start()
        registration = Registration()
        bridge._actor_registrations["worker"] = registration
        with pytest.raises(ExceptionGroup, match="daemon shutdown failed") as caught:
            bridge.stop()
        assert any(
            "synthetic registration close failure" in str(error)
            for error in caught.value.exceptions
        )
        assert registration.attempts == 1
        assert bridge._actor_registrations["worker"] is registration
        assert bridge._dispatch_state is not None
        assert bridge._startup_in_progress is True
        assert closed == []
        bridge.stop()
        assert registration.attempts == 2
        assert bridge._actor_registrations == {}
        assert bridge._dispatch_state is None
        assert bridge._startup_in_progress is False
        assert closed == ["registration", "dispatch-state"]
    finally:
        sys.modules.pop(spec.name, None)


def test_directory_non_draining_close_still_closes_later_resources() -> None:
    scenario = Scenario(_candidate_source())
    with pytest.raises(RuntimeError, match="synthetic runtime start failure"):
        scenario.exercise()
    scenario.prepare_close()
    failure = TimeoutError("presence authority did not drain")

    def blocked_directory_close():
        scenario.events.append("directory.close")
        raise failure

    scenario.app._directory = SimpleNamespace(close=blocked_directory_close)
    for attribute, name in (
        ("_transport", "transport"),
        ("_state_persistence", "state-persistence"),
        ("_gateway_logger", "gateway-logger"),
        ("_logger", "logger"),
    ):
        setattr(scenario.app, attribute, Owner(name, scenario.events))
    with pytest.raises(TimeoutError, match="presence authority did not drain") as caught:
        scenario.close()
    assert caught.value is failure
    assert [event for event in scenario.events if event in {
        "directory.close", "transport.close", "state-persistence.close",
        "gateway-logger.close", "logger.close",
    }] == [
        "directory.close", "transport.close", "state-persistence.close",
        "gateway-logger.close", "logger.close",
    ]


def test_source_effects_drain_before_lifecycle_and_journal_teardown() -> None:
    scenario = Scenario(_candidate_source())
    with pytest.raises(RuntimeError, match="synthetic runtime start failure"):
        scenario.exercise()
    scenario.prepare_close()
    closes = iter((False, True))

    def close_source(*, timeout):
        assert timeout > 0
        scenario.events.append("source.close")
        return next(closes)

    def drain_lifecycle(timeout):
        assert timeout > 0
        scenario.events.append("lifecycle.drain")
        return True

    source = SimpleNamespace(close=close_source)
    lifecycle = SimpleNamespace(drain=drain_lifecycle)
    journal = Owner("journal", scenario.events)
    scenario.app._transfer_source = source
    scenario.app._lifecycle_manager = lifecycle
    scenario.app._transfer_p2 = journal
    with pytest.raises(RuntimeError, match="P2 source effects did not drain"):
        scenario.close()
    assert scenario.app._transfer_source is source
    assert scenario.app._lifecycle_manager is lifecycle
    assert scenario.app._transfer_p2 is journal
    assert "lifecycle.drain" not in scenario.events
    assert "harnesses.stop.1" not in scenario.events
    assert journal.close_count == 0
    scenario.close()
    assert scenario.app._transfer_source is None
    assert scenario.events.index("lifecycle.drain") > scenario.events.index("source.close")
    assert scenario.events.index("journal.close") > scenario.events.index("lifecycle.drain")
