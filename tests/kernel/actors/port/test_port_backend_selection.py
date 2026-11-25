# Decision record (file header; no inline comments by repository rule):
# - P6 pinned the create_engine seam, S moved the shipped default, and P8
#   removed the pykka backend: an unset HYPRIAL_ACTOR_BACKEND selects the
#   native engine over hyprial._core.actors, explicit reference-abi serves the
#   same port from the packaged pure-Python ABI engine, and a missing or
#   foreign native module fails closed. Every case goes through create_engine,
#   not a backend constructor, so the selector and the adapter are exercised
#   together.
# - Failure cases are deterministic without a built extension: sys.modules
#   carries a module or an explicit None entry, so the native path never
#   depends on the host's toolchain. The fake native engine records what the
#   adapter passes down and answers from the handler the adapter wrapped.
# - The reference-abi cases assert the observable port contract (admission,
#   ask, events, expected errors, factory errors, drain and shutdown). The
#   existing port suites are run a second time with the same environment to
#   widen that contract, so this file stays a seam test, not a copy of them.
"""P6/S/P8 backend selection: default native, explicit reference-abi, fail-closed."""

from __future__ import annotations

import sys
import time
import types
from collections.abc import Callable

import pytest

from hyprial.kernel.impl.actor_runtime import (
    ActorAdmissionError,
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorSpec,
    ActorState,
    AdmissionResult,
    ExpectedActorError,
    STATE_AUTHORITY,
    SupervisionPolicy,
)
from hyprial.kernel.impl.actor_runtime.backends.abi import AbiEngine
from hyprial.kernel.impl.actor_runtime.backends.native import (
    NATIVE_MODULE,
    ActorBackendUnavailableError,
)
from hyprial.kernel.impl.actor_runtime.port import (
    ActorEngineConfig,
    create_engine,
)
from hyprial.kernel.impl.actor_runtime.port.config import (
    ACTOR_BACKENDS,
    ACTOR_BACKEND_ENV,
    DEFAULT_ACTOR_BACKEND,
    UnknownActorBackendError,
    selected_actor_backend,
)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _policies() -> dict[str, SupervisionPolicy]:
    return {
        STATE_AUTHORITY: SupervisionPolicy(
            max_restarts=2,
            restart_window=10.0,
            base_backoff=0.0,
            max_backoff=0.0,
            jitter_ratio=0.0,
        )
    }


def _config(
    event_sink: Callable[[ActorEvent], None] | None = None,
) -> ActorEngineConfig:
    return ActorEngineConfig(policies=_policies(), event_sink=event_sink)


def test_unset_environment_selects_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ACTOR_BACKEND_ENV, raising=False)
    assert selected_actor_backend({}) == DEFAULT_ACTOR_BACKEND == "native"
    engine = create_engine(_config())
    assert isinstance(engine, AbiEngine)
    assert engine._module.__name__ == NATIVE_MODULE
    assert engine.shutdown(1.0) is True


def test_pykka_is_no_longer_a_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ACTOR_BACKENDS == ("reference-abi", "native")
    monkeypatch.setenv(ACTOR_BACKEND_ENV, "pykka")
    with pytest.raises(UnknownActorBackendError):
        create_engine(_config())


def test_unknown_or_empty_backend_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value in ("", "pyka", "pykka", "native ", "PYDANTIC"):
        with pytest.raises(UnknownActorBackendError):
            selected_actor_backend({ACTOR_BACKEND_ENV: value})
        monkeypatch.setenv(ACTOR_BACKEND_ENV, value)
        with pytest.raises(UnknownActorBackendError):
            create_engine(_config())
    assert selected_actor_backend({"OTHER": "native"}) == "native"


def test_selection_is_read_at_each_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ACTOR_BACKEND_ENV, raising=False)
    first = create_engine(_config())
    assert isinstance(first, AbiEngine)
    assert first._module.__name__ == NATIVE_MODULE
    assert first.shutdown(1.0) is True
    monkeypatch.setenv(ACTOR_BACKEND_ENV, "reference-abi")
    second = create_engine(_config())
    assert isinstance(second, AbiEngine)
    assert second._module.__name__ != NATIVE_MODULE
    assert second.shutdown(1.0) is True


def test_reference_abi_backend_serves_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ACTOR_BACKEND_ENV, "reference-abi")
    events: list[ActorEvent] = []
    spec_events: list[ActorEvent] = []
    engine = create_engine(_config(events.append))
    assert isinstance(engine, AbiEngine)
    handle = engine.spawn(
        ActorSpec(name="ref-cell", handler_factory=lambda: lambda command: command,
                  event_sink=spec_events.append)
    )
    assert engine.tell(handle, "one") is AdmissionResult.ACCEPTED
    assert engine.ask(handle, "two", 1.0) == "two"
    _wait_until(lambda: len(events) >= 2 and len(spec_events) >= 2)
    assert events[-1].kind is ActorEventKind.COMMAND_COMPLETED
    assert spec_events[-1].handle == handle
    snapshot = engine.snapshot(handle)
    assert snapshot.handle == handle
    assert snapshot.state is ActorState.RUNNING
    assert snapshot.generation == 1
    assert engine.stop(handle, 1.0) is True
    assert engine.snapshot(handle).state is ActorState.STOPPED
    assert engine.tell(handle, "closed") is AdmissionResult.CLOSED
    with pytest.raises(ActorAdmissionError) as closed:
        engine.ask(handle, "closed", 0.1)
    assert closed.value.reason == "closed"
    assert engine.drain(None, 1.0).complete is True
    assert engine.shutdown(1.0) is True


def test_reference_abi_preserves_expected_and_factory_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ACTOR_BACKEND_ENV, "reference-abi")

    class Rejected(ExpectedActorError):
        pass

    def reject(command: object) -> object:
        raise Rejected("E_REJECTED", "detail")

    engine = create_engine(_config())
    handle = engine.spawn(ActorSpec(name="ref-expected", handler_factory=lambda: reject))
    with pytest.raises(Rejected) as rejected:
        engine.ask(handle, "go", 1.0)
    assert rejected.value.code == "E_REJECTED"

    def broken_factory() -> Callable[[object], object]:
        raise ValueError("factory boom")

    with pytest.raises(ValueError, match="factory boom"):
        engine.spawn(ActorSpec(name="ref-broken", handler_factory=broken_factory))
    assert engine.shutdown(1.0) is True


class _FakeNativeEngine:
    instances: list["_FakeNativeEngine"] = []

    def __init__(
        self,
        *,
        policies: dict[str, tuple],
        jitter_seed: int | None,
        event_sink: Callable[[tuple], None],
        expected_error_type: type,
    ) -> None:
        self.policies = policies
        self.jitter_seed = jitter_seed
        self.event_sink = event_sink
        self.expected_error_type = expected_error_type
        self.handlers: dict[str, Callable[[object], object]] = {}
        self.names: dict[str, str] = {}
        self.stopped: set[str] = set()
        self.pools: set[str] = set()
        self.shutdowns = 0
        self.instances.append(self)

    def spawn(
        self,
        name: str,
        handler_factory: Callable[[], Callable[[object], object]],
        capacity: int,
        profile: str,
        dead_letter_sink: Callable[[object, str], None] | None = None,
        event_sink: Callable[[tuple], None] | None = None,
    ) -> str:
        actor_id = f"native-{len(self.handlers)}"
        self.handlers[actor_id] = handler_factory()
        self.names[actor_id] = name
        return actor_id

    def spawn_pool(
        self,
        name: str,
        worker_handler_factory: Callable[[], Callable[[object], object]],
        pool_size: int,
        capacity: int,
        profile: str,
        dead_letter_sink: Callable[[object, str], None] | None = None,
        event_sink: Callable[[tuple], None] | None = None,
        retry_interval: float = 0.01,
    ) -> str:
        actor_id = f"native-{len(self.handlers)}"
        self.handlers[actor_id] = worker_handler_factory()
        self.names[actor_id] = name
        self.pools.add(actor_id)
        return actor_id

    def tell(self, actor_id: str, name: str, command: object) -> int:
        if actor_id in self.stopped or self.names.get(actor_id) != name:
            return 2
        self.handlers[actor_id](command)
        return 0

    def ask(self, actor_id: str, name: str, command: object, timeout: float) -> object:
        if actor_id not in self.handlers or self.names.get(actor_id) != name:
            raise self._errors[0]("closed")
        return self.handlers[actor_id](command)

    def snapshot(self, actor_id: str, name: str) -> tuple[str, int, int, int, int]:
        if self.names.get(actor_id) != name:
            raise KeyError(actor_id)
        state = "stopped" if actor_id in self.stopped else "running"
        return state, 1, 0, 0, 0

    def stop(self, actor_id: str, name: str, timeout: float) -> bool:
        if self.names.get(actor_id) != name:
            return False
        self.stopped.add(actor_id)
        return True

    def reset(self, actor_id: str, name: str) -> bool:
        return False

    def drain(
        self, actor_ids: list[str] | None, timeout: float
    ) -> tuple[bool, float, list[str]]:
        targets = list(self.names) if actor_ids is None else actor_ids
        for actor_id in targets:
            self.stopped.add(actor_id)
        return True, 0.0, []

    def shutdown(self, timeout: float) -> bool:
        self.shutdowns += 1
        return True

    def send_after(self, actor_id: str, name: str, delay: float, command: object) -> str:
        return "native-timer"

    def cancel_after(self, timer_id: str) -> bool:
        return True


def _fake_native_module(version: object = 1) -> types.ModuleType:
    module = types.ModuleType(NATIVE_MODULE)
    module.ENGINE_ABI_VERSION = version
    module.Engine = _FakeNativeEngine
    module.EffectCompleted = type("EffectCompleted", (), {})
    module.NativeHandlerFactory = type("NativeHandlerFactory", (), {})
    module.ActorAdmissionError = type("ActorAdmissionError", (Exception,), {})
    module.ActorFailedError = type("ActorFailedError", (Exception,), {})
    module.ActorSpawnError = type("ActorSpawnError", (Exception,), {})
    _FakeNativeEngine._errors = (
        module.ActorAdmissionError,
        module.ActorFailedError,
        module.ActorSpawnError,
    )
    return module


def _install_native(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    package = types.ModuleType("hyprial._core")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "hyprial._core", package)
    monkeypatch.setitem(sys.modules, NATIVE_MODULE, module)


def test_native_backend_uses_the_extension_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _fake_native_module()
    _install_native(monkeypatch, module)
    monkeypatch.setenv(ACTOR_BACKEND_ENV, "native")
    events: list[ActorEvent] = []
    engine = create_engine(_config(events.append))
    assert isinstance(engine, AbiEngine)
    handle = engine.spawn(
        ActorSpec(name="native-cell", handler_factory=lambda: lambda command: command)
    )
    assert handle == ActorHandle("native-0", "native-cell")
    assert engine.tell(handle, "one") is AdmissionResult.ACCEPTED
    assert engine.ask(handle, "two", 1.0) == "two"
    engine.snapshot(handle)
    assert _FakeNativeEngine.instances[-1].expected_error_type is ExpectedActorError
    assert _FakeNativeEngine.instances[-1].policies == {
        STATE_AUTHORITY: (2, 10.0, 0.0, 0.0, 0.0)
    }
    assert engine.stop(handle, 1.0) is True
    assert engine.snapshot(handle).state is ActorState.STOPPED
    assert engine.shutdown(1.0) is True
    assert _FakeNativeEngine.instances[-1].shutdowns == 1


def test_native_backend_missing_extension_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, NATIVE_MODULE, None)
    monkeypatch.setenv(ACTOR_BACKEND_ENV, "native")
    with pytest.raises(ActorBackendUnavailableError) as missing:
        create_engine(_config())
    assert NATIVE_MODULE in str(missing.value)
    assert "fail closed" in str(missing.value)
    assert "HYPRIAL_BUILD_NATIVE=1 uv sync --reinstall-package hyprial" in str(missing.value)


def test_native_backend_rejects_a_foreign_abi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_native(monkeypatch, _fake_native_module(version=2))
    monkeypatch.setenv(ACTOR_BACKEND_ENV, "native")
    with pytest.raises(ActorBackendUnavailableError) as foreign:
        create_engine(_config())
    assert "ENGINE_ABI_VERSION=2" in str(foreign.value)


def test_native_backend_rejects_missing_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _fake_native_module()
    del module.Engine
    _install_native(monkeypatch, module)
    monkeypatch.setenv(ACTOR_BACKEND_ENV, "native")
    with pytest.raises(ActorBackendUnavailableError) as incomplete:
        create_engine(_config())
    assert "Engine" in str(incomplete.value)
