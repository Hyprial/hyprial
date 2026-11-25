"""P7 composition wiring: one process-wide host/engine and domain-owned shutdown."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import pytest

from hyprial.daemon.impl.composition.actors import ActorHostRegistry
from hyprial.daemon.impl.composition.domains import AgentSessionDomains
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel.impl.actor_runtime import (
    ActorSpec,
    ActorState,
    AdmissionResult,
)
from hyprial.kernel.impl.actor_runtime.port import ActorCell, ActorHost, ActorSpawner


def _echo(handled: list[object]) -> Callable[[object], object]:
    def handle(command: object) -> object:
        handled.append(command)
        return command

    return handle


def _cell(host: ActorHost, name: str) -> ActorCell:
    return host.spawn(
        ActorSpec(name=name, handler_factory=lambda: _echo([]))
    )


def _domains(tmp_path, *, actor_hosts: object) -> AgentSessionDomains:
    home = tmp_path / "home"
    home.mkdir()
    return AgentSessionDomains(
        database=tmp_path / "state" / "agents.sqlite3",
        desired_state=DesiredStateStore(tmp_path / "state" / "desired-state.json"),
        owner="owner",
        node_id="node",
        daemon_epoch="test-epoch",
        hyprial_home=home,
        worker_running=lambda *_args, **_kwargs: False,
        clock=time.time,
        actor_hosts=cast(ActorHostRegistry, actor_hosts),
    )


class _DrainReport:
    __slots__ = ("complete",)

    def __init__(self, complete: bool) -> None:
        self.complete = complete


class _StubHost:
    def __init__(
        self,
        *,
        stops: tuple[bool | BaseException, ...] = (True,),
        drain: bool | BaseException = True,
    ) -> None:
        self.stops = stops
        self.drain = drain
        self.stop_calls = 0
        self.drain_calls = 0

    def shutdown(self, timeout: float = 5.0) -> bool:
        value = self.stops[min(self.stop_calls, len(self.stops) - 1)]
        self.stop_calls += 1
        if isinstance(value, BaseException):
            raise value
        return value

    def drain_all(self, timeout: float = 5.0) -> _DrainReport:
        self.drain_calls += 1
        if isinstance(self.drain, BaseException):
            raise self.drain
        return _DrainReport(self.drain)


class _ScriptedRegistry:
    def __init__(self, results: tuple[bool, ...]) -> None:
        self.results = results
        self.calls = 0
        self._hosts = ActorHostRegistry()

    def host_for(self, owner: str) -> ActorHost:
        return self._hosts.host_for(owner)

    def shutdown(self, timeout: float = 5.0) -> bool:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        self._hosts.shutdown(timeout)
        return result


def test_registry_shares_one_host_and_engine_across_owners() -> None:
    registry = ActorHostRegistry()
    first = registry.host_for("owner.one")
    second = registry.host_for("owner.two")
    try:
        assert isinstance(first, ActorHost)
        assert isinstance(first, ActorSpawner)
        assert registry.spawner_for("owner.one") is first
        assert second is first
        assert second.engine is first.engine
        assert registry.owners() == ("owner.one", "owner.two")
    finally:
        assert registry.shutdown(5.0) is True


def test_registry_reuses_the_host_for_the_same_owner() -> None:
    registry = ActorHostRegistry()
    try:
        assert registry.host_for("owner.one") is registry.host_for("owner.one")
        assert registry.owners() == ("owner.one",)
    finally:
        assert registry.shutdown(5.0) is True


def test_registry_drain_all_drains_every_owner() -> None:
    registry = ActorHostRegistry()
    first = _cell(registry.host_for("owner.one"), "first")
    second = _cell(registry.host_for("owner.two"), "second")
    try:
        assert registry.drain_all(1.0) == {"owner.one": True, "owner.two": True}
        assert first.snapshot().state is ActorState.STOPPED
        assert second.snapshot().state is ActorState.STOPPED
        assert registry.host_for("owner.one") is not None
    finally:
        assert registry.shutdown(5.0) is True


def test_registry_owner_host_drain_is_engine_wide() -> None:
    registry = ActorHostRegistry()
    first_host = registry.host_for("owner.one")
    second_host = registry.host_for("owner.two")
    first = _cell(first_host, "first")
    second = _cell(second_host, "second")
    try:
        assert first_host is second_host
        assert first_host.drain_all(1.0).complete is True
        assert first.snapshot().state is ActorState.STOPPED
        assert second.snapshot().state is ActorState.STOPPED
    finally:
        assert registry.shutdown(5.0) is True


def test_registry_shutdown_stops_hosts_and_refuses_new_owners() -> None:
    registry = ActorHostRegistry()
    cell = _cell(registry.host_for("owner.one"), "cell")
    assert registry.shutdown(5.0) is True
    assert cell.snapshot().state is ActorState.STOPPED
    assert registry.shutdown(5.0) is True
    with pytest.raises(RuntimeError):
        registry.host_for("owner.two")
    with pytest.raises(RuntimeError):
        registry.drain_all(1.0)


def test_registry_rejects_blank_owners() -> None:
    registry = ActorHostRegistry()
    try:
        with pytest.raises(ValueError):
            registry.host_for("")
    finally:
        assert registry.shutdown(5.0) is True


def test_registry_shutdown_retries_the_failed_host() -> None:
    registry = ActorHostRegistry()
    host = _StubHost(stops=(False, True))
    registry._host = cast(ActorHost, host)
    registry._owners.append("owner.retry")
    assert registry.shutdown(5.0) is False
    assert registry.owners() == ("owner.retry",)
    assert registry.shutdown(5.0) is True
    assert registry.owners() == ()
    assert host.stop_calls == 2


def test_registry_shutdown_retains_the_host_after_base_exception() -> None:
    registry = ActorHostRegistry()
    interrupting = _StubHost(stops=(KeyboardInterrupt(), True))
    registry._host = cast(ActorHost, interrupting)
    registry._owners.append("owner.interrupt")
    with pytest.raises(KeyboardInterrupt):
        registry.shutdown(5.0)
    assert interrupting.stop_calls == 1
    assert registry.owners() == ("owner.interrupt",)
    assert registry.shutdown(5.0) is True
    assert registry.owners() == ()


def test_registry_drain_all_drains_the_shared_engine_once() -> None:
    registry = ActorHostRegistry()
    host = _StubHost(drain=True)
    registry._host = cast(ActorHost, host)
    registry._owners.extend(("owner.one", "owner.two"))
    try:
        assert registry.drain_all(1.0) == {"owner.one": True, "owner.two": True}
        assert host.drain_calls == 1
    finally:
        assert registry.shutdown(5.0) is True


def test_registry_drain_all_reports_a_shared_failure_for_every_owner() -> None:
    registry = ActorHostRegistry()
    host = _StubHost(drain=RuntimeError("drain failed"))
    registry._host = cast(ActorHost, host)
    registry._owners.extend(("owner.one", "owner.two"))
    try:
        assert registry.drain_all(1.0) == {"owner.one": False, "owner.two": False}
        assert host.drain_calls == 1
    finally:
        assert registry.shutdown(5.0) is True


def test_registry_host_for_is_stable_under_concurrency() -> None:
    registry = ActorHostRegistry()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            hosts = list(
                pool.map(lambda index: registry.host_for(f"owner.{index}"), range(8))
            )
        assert len({id(host) for host in hosts}) == 1
        assert set(registry.owners()) == {f"owner.{index}" for index in range(8)}
    finally:
        assert registry.shutdown(5.0) is True


def test_domain_close_shuts_down_injected_hosts(tmp_path) -> None:
    registry = ActorHostRegistry()
    domains = _domains(tmp_path, actor_hosts=registry)
    cell = _cell(domains.actor_hosts.host_for("domain.owner"), "domain-cell")
    assert cell.tell("one") is AdmissionResult.ACCEPTED
    assert domains.close() is True
    assert cell.snapshot().state is ActorState.STOPPED
    with pytest.raises(RuntimeError):
        registry.host_for("domain.owner")


def test_domain_close_reports_a_failed_host_shutdown(tmp_path) -> None:
    domains = _domains(tmp_path, actor_hosts=_ScriptedRegistry((False,)))
    assert domains.close() is False


def test_domain_close_can_leave_borrowed_hosts_to_the_owner(tmp_path) -> None:
    registry = ActorHostRegistry()
    domains = _domains(tmp_path, actor_hosts=registry)
    cell = _cell(domains.actor_hosts.host_for("domain.owner"), "domain-cell")
    assert cell.tell("one") is AdmissionResult.ACCEPTED
    try:
        assert domains.close(shutdown_hosts=False) is True
        assert cell.snapshot().state is ActorState.RUNNING
        assert registry.host_for("domain.owner") is not None
    finally:
        assert registry.shutdown(5.0) is True
    assert cell.snapshot().state is ActorState.STOPPED


def test_domain_close_without_host_ownership_never_calls_shutdown(tmp_path) -> None:
    registry = _ScriptedRegistry((False,))
    domains = _domains(tmp_path, actor_hosts=registry)
    assert domains.close(shutdown_hosts=False) is True
    assert registry.calls == 0
    assert domains.close(shutdown_hosts=False) is True
    assert registry.calls == 0
    assert domains.close() is False
    assert registry.calls == 1


def test_domain_close_retries_a_failed_host_shutdown(tmp_path) -> None:
    registry = _ScriptedRegistry((False, True))
    domains = _domains(tmp_path, actor_hosts=registry)
    assert domains.close() is False
    assert domains.close() is True
    assert registry.calls == 2


def test_domain_close_keeps_reporting_a_terminal_host_failure(tmp_path) -> None:
    registry = _ScriptedRegistry((False, False))
    domains = _domains(tmp_path, actor_hosts=registry)
    assert domains.close() is False
    assert domains.close() is False
    assert registry.calls == 2


def test_agent_directory_facade_close_leaves_the_borrowed_host_running(
    tmp_path,
) -> None:
    """P7 regression: the facade is a borrower view, not a host owner.

    A caller closing ``app.agents`` must not stop the process-wide engine
    under every other daemon consumer; the application's last-resource
    shutdown step is the only owner that stops it.
    """

    registry = ActorHostRegistry()
    domains = _domains(tmp_path, actor_hosts=registry)
    probe = _cell(registry.host_for("p7-probe"), "probe")
    assert probe.tell("one") is AdmissionResult.ACCEPTED
    try:
        domains.agents.close()
        assert probe.snapshot().state is ActorState.RUNNING
        assert registry.host_for("p7-probe") is not None
    finally:
        assert domains.close(shutdown_hosts=False) is True
        assert registry.shutdown(5.0) is True
    assert probe.snapshot().state is ActorState.STOPPED
