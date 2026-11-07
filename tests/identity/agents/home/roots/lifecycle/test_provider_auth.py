"""agent_home_dirs: provider auth scenarios."""

from __future__ import annotations
import contextlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
import hyprial.identity as provider_auth_module
import hyprial.identity as provider_auth_actor_module
from hyprial.identity.impl.agents.actor._core import AgentActor
from hyprial.identity.impl.agents.actor.ports import (
    AcquireAgentRuntimeLaunchCommand,
    AgentRuntimeLaunchLeaseCompleted,
    AgentMutationCompleted,
    CleanupRevokedAgentHomeCommand,
    CreateAgentCommand,
    DestroyAgentCommand,
    ReleaseAgentRuntimeLaunchCommand,
)
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.startup import _ContextBoundProviderAuthRunner
from hyprial.daemon.impl.composition import (
    AgentSessionDomains,
    CorrelatedDomainEvents,
    DomainCommandError,
)
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.identity.impl.provider_auth import HelperOutcome

from .support import (
    _AgentEvents,
    _configured_launch_actor,
    _isolated_environment as _isolated_environment,
    _registry,
)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def test_agent_settled_call_submits_once_and_retains_its_completion() -> None:
    domain_events = CorrelatedDomainEvents(capacity=1)
    submitted = []

    class AgentPort:
        @staticmethod
        def submit(command):
            submitted.append(command)
            return PortAdmission.ACCEPTED

    domains = object.__new__(AgentSessionDomains)
    domains.agent = AgentPort()
    domains._agent_events = domain_events
    command = AcquireAgentRuntimeLaunchCommand("delayed-acquire", "context")
    results = []

    caller = threading.Thread(
        target=lambda: results.append(
            domains.call_agent_settled(
                command, AgentRuntimeLaunchLeaseCompleted
            )
        )
    )
    caller.start()
    deadline = time.monotonic() + 1.0
    while not submitted:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    domain_events.publish(PortCommandRejected(
        "unrelated", "agent", 1, 1, "UNRELATED", "unrelated"
    ))
    domain_events.publish(AgentRuntimeLaunchLeaseCompleted(
        "delayed-acquire", 1, 1, "worker", "lease", True
    ))
    caller.join(1.0)

    assert not caller.is_alive()
    assert submitted == [command]
    assert len(results) == 1 and results[0].lease_token == "lease"


def test_delayed_acquire_settles_before_body_failure_and_releases() -> None:
    acquire_entered = threading.Event()
    settle_acquire = threading.Event()
    body_entered = threading.Event()
    calls = []

    class Domains:
        @staticmethod
        def call_agent(_command, _expected):
            raise AssertionError("observational Agent call must not be used")

        @staticmethod
        def call_agent_settled(command, _expected):
            calls.append(command)
            if isinstance(command, AcquireAgentRuntimeLaunchCommand):
                acquire_entered.set()
                assert settle_acquire.wait(1.0)
                return AgentRuntimeLaunchLeaseCompleted(
                    command.correlation_id,
                    1,
                    1,
                    "worker",
                    "lease-delayed",
                    True,
                )
            if isinstance(command, ReleaseAgentRuntimeLaunchCommand):
                return AgentRuntimeLaunchLeaseCompleted(
                    command.correlation_id,
                    1,
                    1,
                    "worker",
                    command.lease_token,
                    False,
                )
            raise AssertionError(type(command).__name__)

    application = object.__new__(DaemonApplication)
    application._agent_session_domains = Domains()
    application._log = lambda *_args, **_kwargs: None
    context = SimpleNamespace(authority_prepared=True, launch_token="context")
    failures = []

    def launch() -> None:
        try:
            with application._agent_runtime_launch_custody(context):
                body_entered.set()
                raise RuntimeError("spawn failed before returning a child")
        except RuntimeError as error:
            failures.append(str(error))

    caller = threading.Thread(target=launch)
    caller.start()
    assert acquire_entered.wait(1.0)
    assert not body_entered.is_set()
    settle_acquire.set()
    caller.join(1.0)

    assert not caller.is_alive()
    assert body_entered.is_set()
    assert failures == ["spawn failed before returning a child"]
    assert [type(command) for command in calls] == [
        AcquireAgentRuntimeLaunchCommand,
        ReleaseAgentRuntimeLaunchCommand,
    ]


def test_stale_context_bound_provider_auth_runner_never_calls_helper(
    tmp_path: Path,
) -> None:
    _registry_value, actor, events, context = _configured_launch_actor(tmp_path)

    class Domains:
        agent = actor

        @staticmethod
        def call_agent_settled(command, expected):
            assert actor.submit(command) is PortAdmission.ACCEPTED
            return events.wait_domain(command.correlation_id, expected)

    application = object.__new__(DaemonApplication)
    application._agent_session_domains = Domains()
    application._log = lambda *_args, **_kwargs: None
    helper_calls = []

    def helper(*_args, **_kwargs):
        helper_calls.append(True)
        return HelperOutcome.OK

    runner = _ContextBoundProviderAuthRunner(
        context, helper, application._agent_runtime_launch_custody
    )
    try:
        assert actor.submit(DestroyAgentCommand(
            "destroy-before-auth-helper", "worker"
        )) is PortAdmission.ACCEPTED
        events.wait("destroy-before-auth-helper", AgentMutationCompleted)

        with pytest.raises(DomainCommandError) as caught:
            runner("anthropic", lambda _announcement: None)
        assert caught.value.code == "AGENT_RUNTIME_CONTEXT_STALE"
        assert helper_calls == []
        assert not context.roots.native_root.exists()
    finally:
        assert actor.drain(3.0).complete


def test_provider_auth_factory_wraps_only_agent_context_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    class Runner:
        def __init__(self, **options):
            self.options = options

    class Coordinator:
        def __init__(self, **options):
            captured.update(options)

    application = object.__new__(DaemonApplication)
    application.user_profiles = SimpleNamespace(path=tmp_path / "missing-profiles")
    application.owner = "owner"
    application.hyprial_home = tmp_path / "home"
    application.state_dir = tmp_path / "state"
    application.stop_event = threading.Event()
    application._log = lambda *_args, **_kwargs: None
    application._agent_runtime_launch_custody = lambda _context: contextlib.nullcontext()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    monkeypatch.setattr(provider_auth_module, "DeviceLoginRunner", Runner)
    monkeypatch.setattr(provider_auth_actor_module, "ProviderAuthAuthority", Coordinator)

    coordinator = application._build_provider_auth_coordinator()
    assert isinstance(coordinator, Coordinator)
    assert isinstance(captured["helper_runner"], Runner)
    assert not isinstance(
        captured["helper_runner"], _ContextBoundProviderAuthRunner
    )

    context = SimpleNamespace(environment=lambda: {"PI_CODING_AGENT_DIR": "/agent"})
    runtime = captured["runtime_helper_factory"](context)
    assert isinstance(runtime, _ContextBoundProviderAuthRunner)


def test_context_bound_provider_auth_runner_holds_home_until_helper_finishes(
    tmp_path: Path,
) -> None:
    registry, actor, events, context = _configured_launch_actor(tmp_path)

    class Domains:
        agent = actor

        @staticmethod
        def call_agent_settled(command, expected):
            assert actor.submit(command) is PortAdmission.ACCEPTED
            return events.wait_domain(command.correlation_id, expected)

    application = object.__new__(DaemonApplication)
    application._agent_session_domains = Domains()
    application._log = lambda *_args, **_kwargs: None
    helper_entered = threading.Event()
    helper_release = threading.Event()
    outcomes = []

    def helper(*_args, **_kwargs):
        helper_entered.set()
        assert helper_release.wait(2.0)
        return HelperOutcome.OK

    runner = _ContextBoundProviderAuthRunner(
        context, helper, application._agent_runtime_launch_custody
    )
    caller = threading.Thread(
        target=lambda: outcomes.append(
            runner("anthropic", lambda _announcement: None)
        )
    )
    caller.start()
    try:
        assert helper_entered.wait(1.0)
        assert actor.submit(DestroyAgentCommand(
            "destroy-during-auth-helper", "worker"
        )) is PortAdmission.ACCEPTED
        rejected = events.wait(
            "destroy-during-auth-helper", PortCommandRejected
        )
        assert rejected.code == "AGENT_LAUNCH_IN_PROGRESS"
        assert registry.require("worker").entity_token == context.entity_token
        assert context.roots.native_root.is_dir()

        helper_release.set()
        caller.join(2.0)
        assert not caller.is_alive()
        assert outcomes == [HelperOutcome.OK]

        assert actor.submit(DestroyAgentCommand(
            "destroy-after-auth-helper", "worker"
        )) is PortAdmission.ACCEPTED
        events.wait("destroy-after-auth-helper", AgentMutationCompleted)
        assert registry.get("worker") is None
    finally:
        helper_release.set()
        caller.join(2.0)
        assert actor.drain(3.0).complete


def test_agent_session_domains_close_preserves_lease_release_then_retries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "personality"
    config_file = source / "native" / "codex" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text('cli_auth_credentials_store = "file"\n')
    desired = DesiredStateStore(tmp_path / "state" / "desired-state.json")
    domains = AgentSessionDomains(
        database=tmp_path / "state" / "agents.sqlite3",
        desired_state=desired,
        owner="owner",
        node_id="node",
        daemon_epoch="epoch",
        hyprial_home=tmp_path / "home",
        worker_running=lambda *_args, **_kwargs: False,
        clock=time.time,
    )
    closed = False
    try:
        domains.call_agent(
            CreateAgentCommand(
                "create-worker",
                "worker",
                cwd=str(tmp_path / "work"),
                config={
                    "sources": [{"path": str(source), "required": True}],
                    "discovery": "explicit-only",
                },
            ),
            AgentMutationCompleted,
        )
        context = domains.agent.prepare_runtime_context(
            "worker", "codex", str(tmp_path / "work"), TEST_TOOL_PROFILE
        )
        assert context is not None and context.launch_token
        lease = domains.call_agent_settled(
            AcquireAgentRuntimeLaunchCommand(
                "composition-acquire", context.launch_token
            ),
            AgentRuntimeLaunchLeaseCompleted,
        )

        assert not domains.close(0.05)
        assert not domains._domains_closed
        assert domains._registry.require("worker").entity_token == context.entity_token

        order = []

        class Harnesses:
            drain_complete = True

            @staticmethod
            def stop():
                order.append("harness.stop")
                released = domains.call_agent_settled(
                    ReleaseAgentRuntimeLaunchCommand(
                        "composition-release",
                        lease.lease_token,
                        "composition-acquire",
                    ),
                    AgentRuntimeLaunchLeaseCompleted,
                )
                assert not released.acquired

        class Finalizer:
            @staticmethod
            def detach():
                order.append("finalizer.detach")

        application = object.__new__(DaemonApplication)
        application._harnesses = Harnesses()
        application._agent_session_domains = domains
        application._agent_domains_finalizer = Finalizer()
        application._close_harnesses_before_agent_domains()

        assert order == ["harness.stop", "finalizer.detach"]
        assert application._harnesses is None
        assert domains._domains_closed
        assert domains.close(0.01)
        closed = True
    finally:
        if not closed:
            domains.close(3.0)


def test_agent_session_domains_keep_agent_alive_when_session_drain_is_incomplete() -> None:
    calls = []

    class Session:
        @staticmethod
        def drain(_timeout):
            calls.append("session")
            return SimpleNamespace(complete=False)

    class Agent:
        @staticmethod
        def drain(_timeout):
            calls.append("agent")
            return SimpleNamespace(complete=True)

    class CloseProbe:
        @staticmethod
        def close():
            calls.append("closed")

    class CellsProbe:
        @staticmethod
        def close():
            calls.append("cells")

    domains = object.__new__(AgentSessionDomains)
    domains._domains_closed = False
    domains._close_lock = threading.Lock()
    domains.cells = CellsProbe()
    domains.session = Session()
    domains.agent = Agent()
    domains._session_events = CloseProbe()
    domains._agent_events = CloseProbe()
    domains._registry = CloseProbe()

    assert not domains.close(0.01)
    assert calls == ["cells", "session"]
    assert not domains._domains_closed


def test_application_retains_agent_domains_when_retryable_close_is_incomplete() -> None:
    calls = []

    class Domains:
        @staticmethod
        def close():
            calls.append("domains.close")
            return False

    class Finalizer:
        @staticmethod
        def detach():
            calls.append("finalizer.detach")

    application = object.__new__(DaemonApplication)
    application._harnesses = None
    application._agent_session_domains = Domains()
    application._agent_domains_finalizer = Finalizer()

    with pytest.raises(RuntimeError, match="session/agent domains did not drain"):
        application._close_harnesses_before_agent_domains()
    assert calls == ["domains.close"]
    assert application._agent_session_domains is not None


def test_agent_commands_freeze_nested_json_at_construction() -> None:
    config = {"nested": {"items": ["first"]}}
    capabilities = {"scope": {"values": ["one"]}}
    command = CreateAgentCommand(
        "immutable-create",
        "worker",
        config=config,
        capabilities=tuple(capabilities.items()),
    )
    config["nested"]["items"].append("mutated")
    capabilities["scope"]["values"].append("mutated")
    assert command.config_payload() == {"nested": {"items": ["first"]}}
    assert command.capabilities_payload() == {"scope": {"values": ["one"]}}


def test_interactive_runtime_preparation_reconciles_custom_codex_auth(
    tmp_path: Path,
) -> None:
    registry, actor, _events, context = _configured_launch_actor(
        tmp_path,
        model_provider="deepseek",
        seeded_codex_auth=b'{"OPENAI_API_KEY":"test-only"}\n',
    )
    try:
        assert context.authority_prepared is True
        assert context.model_provider == "deepseek"
        assert not (context.roots.native_root / "auth.json").exists()
        assert len(context.notices) == 1
        notice = context.notices[0]
        assert notice.actor == registry.require("worker").uri
        assert Path(notice.path).read_bytes() == b'{"OPENAI_API_KEY":"test-only"}\n'
        assert 'cli_auth_credentials_store = "ephemeral"' in (
            context.roots.native_root / "config.toml"
        ).read_text()
    finally:
        assert actor.drain(3.0).complete


def test_revoked_home_cleanup_has_a_typed_agent_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    registry.create("worker")
    cleanup = registry.cleanup_home
    monkeypatch.setattr(
        registry,
        "cleanup_home",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        registry.destroy("worker")
    monkeypatch.setattr(registry, "cleanup_home", cleanup)
    events = _AgentEvents()
    actor = AgentActor(registry, event_sink=events)
    try:
        command = CleanupRevokedAgentHomeCommand("cleanup-revoked", "worker")
        assert actor.submit(command) is PortAdmission.ACCEPTED
        completed = events.wait("cleanup-revoked", AgentMutationCompleted)
        assert completed.operation == "cleanup-home"
        assert completed.changed
    finally:
        assert actor.drain(3.0).complete
