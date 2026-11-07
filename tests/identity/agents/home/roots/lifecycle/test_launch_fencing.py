"""agent_home_dirs: launch fencing scenarios."""

from __future__ import annotations
import threading
import time
from pathlib import Path
import pytest
import hyprial.identity.impl.agents.actor._core as agent_actor_module
from hyprial.identity.impl.agents import (
    Agent,
    AgentHomeError,
    AgentNotFoundError,
)
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt
from hyprial.identity.impl.agents.actor._core import AgentActor
from hyprial.identity.impl.agents.actor.ports import (
    AcquireAgentRuntimeLaunchCommand,
    AgentRuntimeLaunchLeaseCompleted,
    AgentMutationCompleted,
    CreateAgentCommand,
    DestroyAgentCommand,
    ReleaseAgentRuntimeLaunchCommand,
    UpdateAgentCommand,
)
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import LifecycleMutationCompleted
from hyprial.kernel import LifecycleMutationRequest

from .support import (
    _AgentEvents,
    _configured_launch_actor,
    _isolated_environment as _isolated_environment,
    _registry,
)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def test_agent_home_lifecycle_io_does_not_hold_mailbox_or_registry_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    registry.create("steady")
    events = _AgentEvents()
    actor = AgentActor(registry, event_sink=events)
    provisioner = registry._home
    assert provisioner is not None
    provision = provisioner.provision
    entered = threading.Event()
    release = threading.Event()

    def blocking_provision(**kwargs: object):  # type: ignore[no-untyped-def]
        if kwargs.get("actor") == "slow":
            entered.set()
            assert release.wait(3.0)
        return provision(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(provisioner, "provision", blocking_provision)
    mutation_threads: list[str] = []

    def trace(statement: str) -> None:
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            mutation_threads.append(threading.current_thread().name)

    registry._db.set_trace_callback(trace)
    lifecycle = LifecycleMutationRequest(
        "lifecycle-create-slow",
        "lifecycle-create-slow:forward:0",
        "lifecycle-create-slow",
        None,
        CreateAgentCommand("lifecycle-create-slow", "slow"),
    )
    try:
        assert actor.submit(lifecycle) is PortAdmission.ACCEPTED
        assert entered.wait(1.0)
        assert actor.submit(
            UpdateAgentCommand(
                "update-while-home-blocked", "steady", model="responsive"
            )
        ) is PortAdmission.ACCEPTED
        update = events.wait("update-while-home-blocked", AgentMutationCompleted)
        assert update.operation == "update"
        release.set()
        completed = events.wait("lifecycle-create-slow", LifecycleMutationCompleted)
        assert completed.provenance.changed
        assert registry.home_receipt("slow").status == "ready"
        assert mutation_threads
        assert len(set(mutation_threads)) == 1
        assert "agent-home-effect" not in mutation_threads
        assert mutation_threads[0] != threading.current_thread().name
    finally:
        registry._db.set_trace_callback(None)
        release.set()
        assert actor.drain(3.0).complete


def test_workspace_publication_is_fenced_by_destroyed_home_incarnation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    registry.create("worker")
    events = _AgentEvents()
    actor = AgentActor(registry, event_sink=events)
    provisioner = registry.home_provisioner
    original = provisioner.ensure_workspace
    entered = threading.Event()
    release = threading.Event()

    def blocked(receipt: HomeReceipt) -> Path:
        entered.set()
        assert release.wait(3.0)
        return original(receipt)

    monkeypatch.setattr(provisioner, "ensure_workspace", blocked)
    paths: list[str] = []
    errors: list[BaseException] = []

    def prepare() -> None:
        try:
            paths.append(actor.prepare_workspace("worker"))
        except BaseException as error:
            errors.append(error)

    caller = threading.Thread(target=prepare)
    caller.start()
    try:
        assert entered.wait(1.0)
        assert actor.submit(
            DestroyAgentCommand("destroy-during-workspace", "worker")
        ) is PortAdmission.ACCEPTED
        deadline = time.monotonic() + 2.0
        while registry.get("worker") is not None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        release.set()
        caller.join(3.0)
        assert not caller.is_alive()
        assert paths == []
        assert len(errors) == 1
        assert isinstance(errors[0], (AgentHomeError, AgentNotFoundError))
        completed = events.wait(
            "destroy-during-workspace", AgentMutationCompleted
        )
        assert completed.operation == "destroy"
        assert registry.get("worker") is None
    finally:
        release.set()
        caller.join(3.0)
        assert actor.drain(3.0).complete


def test_runtime_launch_lease_blocks_destroy_until_release(tmp_path: Path) -> None:
    registry, actor, events, context = _configured_launch_actor(tmp_path)
    try:
        acquire = AcquireAgentRuntimeLaunchCommand(
            "acquire-launch", context.launch_token or ""
        )
        assert actor.submit(acquire) is PortAdmission.ACCEPTED
        lease = events.wait(
            "acquire-launch", AgentRuntimeLaunchLeaseCompleted
        )
        assert lease.acquired and lease.actor == context.actor
        custody = actor.read_runtime_launch_custody()
        assert len(custody) == 1
        assert custody[0].operation_id == "acquire-launch"
        assert custody[0].lease_token == lease.lease_token

        destroy = DestroyAgentCommand("destroy-during-launch", "worker")
        assert actor.submit(destroy) is PortAdmission.ACCEPTED
        rejected = events.wait("destroy-during-launch", PortCommandRejected)
        assert rejected.code == "AGENT_LAUNCH_IN_PROGRESS"
        assert registry.require("worker").entity_token == context.entity_token
        assert context.roots.native_root.is_dir()

        assert actor.submit(ReleaseAgentRuntimeLaunchCommand(
            "release-launch", lease.lease_token, "acquire-launch"
        )) is PortAdmission.ACCEPTED
        released = events.wait(
            "release-launch", AgentRuntimeLaunchLeaseCompleted
        )
        assert not released.acquired
        assert actor.read_runtime_launch_custody() == ()
        assert actor.submit(DestroyAgentCommand(
            "destroy-after-launch", "worker"
        )) is PortAdmission.ACCEPTED
        completed = events.wait("destroy-after-launch", AgentMutationCompleted)
        assert completed.operation == "destroy"
        assert registry.get("worker") is None
    finally:
        assert actor.drain(3.0).complete


def test_runtime_launch_compensation_requires_exact_operation_and_lease(
    tmp_path: Path,
) -> None:
    _registry_value, actor, events, context = _configured_launch_actor(tmp_path)
    try:
        for operation_id in ("acquire-one", "acquire-two"):
            assert actor.submit(AcquireAgentRuntimeLaunchCommand(
                operation_id, context.launch_token or ""
            )) is PortAdmission.ACCEPTED
        first = events.wait("acquire-one", AgentRuntimeLaunchLeaseCompleted)
        second = events.wait("acquire-two", AgentRuntimeLaunchLeaseCompleted)

        assert actor.submit(ReleaseAgentRuntimeLaunchCommand(
            "release-mismatched", first.lease_token, "acquire-two"
        )) is PortAdmission.ACCEPTED
        rejected = events.wait("release-mismatched", PortCommandRejected)
        assert rejected.code == "AGENT_RUNTIME_LAUNCH_MISMATCH"
        assert {item.lease_token for item in actor.read_runtime_launch_custody()} == {
            first.lease_token,
            second.lease_token,
        }

        assert actor.submit(ReleaseAgentRuntimeLaunchCommand(
            "release-one", first.lease_token, "acquire-one"
        )) is PortAdmission.ACCEPTED
        events.wait("release-one", AgentRuntimeLaunchLeaseCompleted)
        assert actor.submit(ReleaseAgentRuntimeLaunchCommand(
            "release-two", second.lease_token, "acquire-two"
        )) is PortAdmission.ACCEPTED
        events.wait("release-two", AgentRuntimeLaunchLeaseCompleted)
        assert actor.read_runtime_launch_custody() == ()
    finally:
        assert actor.drain(3.0).complete


def test_consumed_runtime_context_reconnect_survives_clock_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registry_value, actor, events, context = _configured_launch_actor(tmp_path)
    monotonic = time.monotonic
    try:
        assert actor.submit(AcquireAgentRuntimeLaunchCommand(
            "acquire-first", context.launch_token or ""
        )) is PortAdmission.ACCEPTED
        first = events.wait("acquire-first", AgentRuntimeLaunchLeaseCompleted)
        assert actor.submit(ReleaseAgentRuntimeLaunchCommand(
            "release-first", first.lease_token, "acquire-first"
        )) is PortAdmission.ACCEPTED
        events.wait("release-first", AgentRuntimeLaunchLeaseCompleted)

        monkeypatch.setattr(
            agent_actor_module.time, "monotonic", lambda: monotonic() + 301.0
        )
        assert actor.submit(AcquireAgentRuntimeLaunchCommand(
            "acquire-reconnect", context.launch_token or ""
        )) is PortAdmission.ACCEPTED
        reconnect = events.wait(
            "acquire-reconnect", AgentRuntimeLaunchLeaseCompleted
        )
        assert reconnect.acquired and reconnect.actor == context.actor
        assert actor.submit(ReleaseAgentRuntimeLaunchCommand(
            "release-reconnect", reconnect.lease_token, "acquire-reconnect"
        )) is PortAdmission.ACCEPTED
        events.wait("release-reconnect", AgentRuntimeLaunchLeaseCompleted)
    finally:
        monkeypatch.setattr(agent_actor_module.time, "monotonic", monotonic)
        assert actor.drain(3.0).complete


def test_consumed_runtime_context_is_pinned_across_offer_eviction(
    tmp_path: Path,
) -> None:
    _registry_value, actor, events, context = _configured_launch_actor(tmp_path)
    actor._runtime_launches.context_capacity = 2
    try:
        assert actor.submit(AcquireAgentRuntimeLaunchCommand(
            "acquire-pinned", context.launch_token or ""
        )) is PortAdmission.ACCEPTED
        lease = events.wait("acquire-pinned", AgentRuntimeLaunchLeaseCompleted)
        assert actor.submit(ReleaseAgentRuntimeLaunchCommand(
            "release-pinned", lease.lease_token, "acquire-pinned"
        )) is PortAdmission.ACCEPTED
        events.wait("release-pinned", AgentRuntimeLaunchLeaseCompleted)

        evicted = actor.prepare_runtime_context(
            "worker", "codex", str(tmp_path / "work"), TEST_TOOL_PROFILE
        )
        current = actor.prepare_runtime_context(
            "worker", "codex", str(tmp_path / "work"), TEST_TOOL_PROFILE
        )
        assert evicted is not None and evicted.launch_token
        assert current is not None and current.launch_token

        assert actor.submit(AcquireAgentRuntimeLaunchCommand(
            "acquire-pinned-again", context.launch_token or ""
        )) is PortAdmission.ACCEPTED
        pinned = events.wait(
            "acquire-pinned-again", AgentRuntimeLaunchLeaseCompleted
        )
        assert actor.submit(AcquireAgentRuntimeLaunchCommand(
            "acquire-evicted", evicted.launch_token
        )) is PortAdmission.ACCEPTED
        rejected = events.wait("acquire-evicted", PortCommandRejected)
        assert rejected.code == "AGENT_RUNTIME_CONTEXT_STALE"
        assert actor.submit(ReleaseAgentRuntimeLaunchCommand(
            "release-pinned-again", pinned.lease_token,
            "acquire-pinned-again",
        )) is PortAdmission.ACCEPTED
        events.wait("release-pinned-again", AgentRuntimeLaunchLeaseCompleted)
    finally:
        assert actor.drain(3.0).complete


def test_replaced_incarnation_rejects_old_runtime_launch_token(
    tmp_path: Path,
) -> None:
    registry, actor, events, context = _configured_launch_actor(tmp_path)
    try:
        assert actor.submit(DestroyAgentCommand(
            "destroy-old-context", "worker"
        )) is PortAdmission.ACCEPTED
        events.wait("destroy-old-context", AgentMutationCompleted)
        assert actor.submit(CreateAgentCommand(
            "create-replacement",
            "worker",
            cwd=str(tmp_path / "work"),
            config={
                "sources": [{
                    "path": str(tmp_path / "personality"), "required": True
                }],
                "discovery": "explicit-only",
            },
        )) is PortAdmission.ACCEPTED
        replacement = events.wait("create-replacement", AgentMutationCompleted)
        assert replacement.agent is not None
        assert replacement.agent.entity_token != context.entity_token

        assert actor.submit(AcquireAgentRuntimeLaunchCommand(
            "acquire-stale", context.launch_token or ""
        )) is PortAdmission.ACCEPTED
        rejected = events.wait("acquire-stale", PortCommandRejected)
        assert rejected.code == "AGENT_RUNTIME_CONTEXT_STALE"
        assert not context.roots.projection_root.exists()
    finally:
        assert actor.drain(3.0).complete


def test_agent_close_waits_for_runtime_launch_release(tmp_path: Path) -> None:
    _registry_value, actor, events, context = _configured_launch_actor(tmp_path)
    acquire = AcquireAgentRuntimeLaunchCommand(
        "acquire-before-close", context.launch_token or ""
    )
    assert actor.submit(acquire) is PortAdmission.ACCEPTED
    lease = events.wait("acquire-before-close", AgentRuntimeLaunchLeaseCompleted)
    assert not actor.drain(0.02).complete
    assert actor.submit(ReleaseAgentRuntimeLaunchCommand(
        "release-before-close", lease.lease_token, "acquire-before-close"
    )) is PortAdmission.ACCEPTED
    events.wait("release-before-close", AgentRuntimeLaunchLeaseCompleted)
    assert actor.drain(3.0).complete


def test_stalled_runtime_spawn_remains_fenced_until_explicit_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, actor, events, context = _configured_launch_actor(tmp_path)
    monotonic = time.monotonic
    try:
        assert actor.submit(AcquireAgentRuntimeLaunchCommand(
            "acquire-stalled", context.launch_token or ""
        )) is PortAdmission.ACCEPTED
        lease = events.wait("acquire-stalled", AgentRuntimeLaunchLeaseCompleted)

        # Move beyond the former five-minute lease deadline. A slow native spawn
        # remains accepted work; time passing cannot prove cancellation.
        monkeypatch.setattr(
            agent_actor_module.time, "monotonic", lambda: monotonic() + 301.0
        )
        assert actor.submit(DestroyAgentCommand(
            "destroy-during-stalled-spawn", "worker"
        )) is PortAdmission.ACCEPTED
        rejected = events.wait("destroy-during-stalled-spawn", PortCommandRejected)
        assert rejected.code == "AGENT_LAUNCH_IN_PROGRESS"
        assert registry.require("worker").entity_token == context.entity_token
        assert context.roots.native_root.is_dir()
        assert not actor.drain(0.02).complete
        assert context.roots.native_root.is_dir()

        assert actor.submit(ReleaseAgentRuntimeLaunchCommand(
            "release-stalled", lease.lease_token, "acquire-stalled"
        )) is PortAdmission.ACCEPTED
        events.wait("release-stalled", AgentRuntimeLaunchLeaseCompleted)
        monkeypatch.setattr(agent_actor_module.time, "monotonic", monotonic)
        assert actor.drain(3.0).complete
    finally:
        monkeypatch.setattr(agent_actor_module.time, "monotonic", monotonic)
        assert actor.drain(3.0).complete


def test_application_destroy_reserves_launch_fence_before_side_effects() -> None:
    effects = []
    agent = Agent(
        uri="agent:owner:node:worker",
        actor="worker",
        owner="owner",
        machine="node",
    )

    class Agents:
        @staticmethod
        def require(_name):
            return agent

    class Domains:
        @staticmethod
        def call_agent_settled(_command, _expected):
            raise RuntimeError("AGENT_LAUNCH_IN_PROGRESS")

    application = object.__new__(DaemonApplication)
    application.agents = Agents()
    application._agent_session_domains = Domains()
    application._stop_agent_runtime = lambda *_args, **_kwargs: effects.append("stop")
    application._drop_persona_route = lambda *_args: effects.append("route")
    application._release_agent_binding = lambda *_args: effects.append("binding")
    application._destroy_agent_messages = lambda *_args: effects.append("messages")
    # Destroy now retires session refs through the per-actor session route;
    # with no coordinator the route applies inline, reservation first.
    application._session_route_coordinator = None

    with pytest.raises(RuntimeError, match="AGENT_LAUNCH_IN_PROGRESS"):
        application._destroy_agent("worker")
    assert effects == []
