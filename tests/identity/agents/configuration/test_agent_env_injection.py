"""P1b B1: the injection closed loop for the pi + headless carrier.

T03 (complete replacement, L3 daemon-authority variables), T06 (per-agent
minimal mapping), T07 (no secret values in any persisted/logged surface),
T11 (failures are loud — no ambient fallback).  The spawn seam is exercised
for real: a child process is exec'd and observed, not mocked.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import hyprial.kernel.impl.primitives.node_identity as node_identity_module
from hyprial.identity.impl.agents import AgentRegistry, SecretSource
from hyprial.identity.impl.agents.home.environment import (
    BASE_CHILD_ENVIRONMENT_NAMES,
    GENERATED_CHILD_ENVIRONMENT_NAMES,
    ChildEnvironmentLaunch,
    build_complete_child_environment,
    compose_worker_child_launch,
)
from hyprial.identity.impl.agents.runtime.secrets import SecretResolutionError
from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel, build_worker_channel

OLD, NEW = "old-owner", "new-owner"
AMBIENT_SENTINEL = "P1BB1_AMBIENT_LEAK"


def _channel(tmp_path: Path, *, actor: str = "agent:p1b:node:worker") -> WorkerChannel:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    return build_worker_channel(
        actor=actor,
        session_ref="sess-1",
        hyprial_home=home,
        state_dir=state,
        node_id="p1b-node",
        owner=NEW,
    )


def test_pac_local_identity_explicit_empty_node_id_never_returns_empty(
    monkeypatch,
) -> None:
    from hyprial.daemon.impl.pac.views.context import _local_identity

    monkeypatch.setenv("HYPRIAL_NODE_ID", "   ")
    monkeypatch.setattr(node_identity_module.socket, "gethostname", lambda: " pac-host ")

    _owner, machine = _local_identity()

    assert machine == "pac-host"


def _spec(**kwargs):
    from hyprial.kernel import HarnessLaunchSpec

    return HarnessLaunchSpec(harness="pi", name="worker", headless=True, **kwargs)


def _launch(channel: WorkerChannel, secrets=()) -> ChildEnvironmentLaunch:
    environment = build_complete_child_environment(
        base={"PATH": "/usr/bin:/bin", "HOME": "/Users/who"},
        generated={
            **channel.identity_environment(),
            "HYPRIAL_AGENT_DIR": "/opt/hyprial/agents/worker",
        },
        secrets=secrets,
    )
    return ChildEnvironmentLaunch(
        environment=environment,
        actor=channel.actor,
        grants=tuple(
            (secret.grant.grant_id, secret.grant.revision) for secret in secrets
        ),
    )


async def _reap(client) -> None:
    """Close a spawned dummy client's transport so no pipe leaks the test."""

    process = getattr(client, "_process", None)
    if process is None:
        return
    for closer in (
        lambda: process.transport.close(),
        lambda: process.stdout and process.stdout.close(),
        lambda: process.stderr and process.stderr.close(),
        lambda: process.stdin and process.stdin.close(),
    ):
        try:
            closer()
        except Exception:
            pass


# ── L3: the three daemon-authority variables ──────────────────────────────────


def test_l3_names_are_generated_and_channel_mints_daemon_values(tmp_path):
    """The whitelist admits exactly what the channel injects (B1 fix)."""

    assert {"HYPRIAL_NODE_ID", "HYPRIAL_OWNER", "HYPRIAL_MANAGED_WORKER"} <= (
        GENERATED_CHILD_ENVIRONMENT_NAMES
    )
    identity = _channel(tmp_path).identity_environment()
    assert identity["HYPRIAL_NODE_ID"] == "p1b-node"
    assert identity["HYPRIAL_OWNER"] == NEW
    assert identity["HYPRIAL_MANAGED_WORKER"] == "1"


def test_worker_pac_context_reads_daemon_machine_not_hostname(monkeypatch):
    """The L3 consumer: machine comes from the injected value, not the
    hostname fallback — a sentinel distinct from gethostname proves the
    fallback was never taken."""

    from hyprial.daemon.impl.pac.views.context import _local_identity

    monkeypatch.setenv("HYPRIAL_NODE_ID", "sentinel-node-id")
    hostname = socket.gethostname()
    assert "sentinel-node-id" != hostname  # otherwise the test proves nothing
    _owner, machine = _local_identity()
    assert machine == "sentinel-node-id"


# ── T03: complete replacement at the pi spawn seam ───────────────────────────


def _client(tmp_path: Path, launch: ChildEnvironmentLaunch, *, actor="agent:p1b:node:worker"):
    from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient

    return PiRpcClient(
        _spec(),
        "sess-1",
        command=(sys.executable, "-c", "pass"),
        worker_channel=_channel(tmp_path, actor=actor),
        complete_launch=launch,
    )


def test_spawn_environment_is_the_frozen_mapping_not_ambient(tmp_path, monkeypatch):
    client = _client(tmp_path, _launch(_channel(tmp_path)))
    monkeypatch.setenv(AMBIENT_SENTINEL, "leak")
    monkeypatch.setenv("HYPRIAL_OWNER", "spoofed-from-parent")
    environment = client._spawn_environment()
    assert AMBIENT_SENTINEL not in environment
    assert environment["HYPRIAL_OWNER"] == NEW  # daemon value, not the spoof
    assert environment["HYPRIAL_NODE_ID"] == "p1b-node"
    assert environment["HOME"] == "/Users/who"
    assert environment["HYPRIAL_AGENT_DIR"] == "/opt/hyprial/agents/worker"


def test_legacy_env_without_complete_launch_still_merges_ambient(tmp_path, monkeypatch):
    """The B2-remaining sites keep today's behaviour — pinned, not silently
    changed by this slice."""

    from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient

    channel = _channel(tmp_path)
    client = PiRpcClient(
        _spec(), "sess-1", command=(sys.executable, "-c", "pass"),
        worker_channel=channel,
    )
    monkeypatch.setenv(AMBIENT_SENTINEL, "leak")
    environment = client._spawn_environment()
    assert AMBIENT_SENTINEL in environment  # legacy mode, collected in B2


def test_real_child_process_sees_exactly_the_complete_environment(
    tmp_path, monkeypatch
):
    """The exec consumer, for real: a spawned child dumps its environment;
    the dump equals the frozen mapping and contains no ambient sentinel."""

    dump = tmp_path / "child-env.json"
    channel = _channel(tmp_path)
    launch = _launch(channel)
    script = (
        "import json,os;json.dump(dict(os.environ),open("
        f"{str(dump)!r},'w'"
        "))"
    )
    from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient

    client = PiRpcClient(
        _spec(), "sess-1",
        command=(sys.executable, "-c", script),
        worker_channel=channel,
        complete_launch=launch,
        startup_timeout_seconds=2.0,
    )
    monkeypatch.setenv(AMBIENT_SENTINEL, "leak")
    monkeypatch.setenv("HYPRIAL_OWNER", "spoofed-from-parent")

    async def spawn():
        try:
            async with client:
                await asyncio.sleep(0)
        except Exception:
            # The dummy command never answers the RPC handshake; the child
            # exiting (or the probe failing) after the dump is expected.
            pass
        finally:
            await _reap(client)

    asyncio.run(asyncio.wait_for(spawn(), timeout=20))
    observed = json.loads(dump.read_text(encoding="utf-8"))
    # The frozen mapping arrives verbatim.  macOS posix_spawn additionally
    # forces __CF_USER_TEXT_ENCODING and may set LC_CTYPE — platform facts
    # of every spawn on this OS, not an ambient merge; they are the only
    # keys allowed beyond the frozen set.
    frozen = launch.environment.for_exec()
    assert frozen.items() <= observed.items()
    extras = set(observed) - set(frozen)
    assert extras <= {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert AMBIENT_SENTINEL not in observed
    assert observed["HYPRIAL_OWNER"] == NEW


def test_completion_receipt_names_identity_and_revisions_only(tmp_path):
    """§3.1.5: the receipt fires at spawn, naming actor + grant revisions;
    secret VALUES never appear (T07)."""

    from hyprial.identity.impl.agents.runtime.secrets import SecretGrant

    class _FakeSecrets:
        def __init__(self) -> None:
            self.grant = SecretGrant(
                actor="worker",
                entity_token="tok-1",
                grant_id="deepseek",
                source=SecretSource.USER_PROVIDER,
                entry_id="deepseek.json",
                field_name="key",
                environment_names=("DEEPSEEK_API_KEY",),
                revision=3,
            )

        def environment(self):
            return {"DEEPSEEK_API_KEY": "SENTINEL-VALUE-X"}

    launch = ChildEnvironmentLaunch(
        environment=build_complete_child_environment(
            base={"PATH": "/bin"},
            generated=_channel(tmp_path).identity_environment(),
            secrets=[_FakeSecrets()],  # type: ignore[list-item]
        ),
        actor="agent:p1b:node:worker",
        grants=(("deepseek", 3),),
    )
    client = _client(tmp_path, launch)
    events: list[tuple[str, dict]] = []
    client._write_log = lambda event, **fields: events.append((event, dict(fields)))  # type: ignore[method-assign]

    async def spawn():
        try:
            async with client:
                await asyncio.sleep(0)
        except Exception:
            pass
        finally:
            await _reap(client)

    asyncio.run(asyncio.wait_for(spawn(), timeout=20))
    receipts = [fields for event, fields in events if event == "worker.environment.receipt"]
    assert receipts, "receipt must fire once the process exists"
    (receipt,) = receipts
    assert receipt["actor"] == "agent:p1b:node:worker"
    assert receipt["grants"] == [{"grantId": "deepseek", "revision": 3}]
    assert "SENTINEL-VALUE-X" not in json.dumps(receipt)


def test_resolver_success_alone_emits_no_receipt(tmp_path):
    """The receipt is a spawn artifact: constructing the client (resolution
    done, no process yet) emits nothing."""

    client = _client(tmp_path, _launch(_channel(tmp_path)))
    events: list[str] = []
    client._write_log = lambda event, **fields: events.append(event)  # type: ignore[method-assign]
    assert "worker.environment.receipt" not in events


# ── T06 / T11: per-agent mapping and loud failures at the daemon seam ────────


_OPEN_REGISTRIES: list[AgentRegistry] = []


def _seeded_registry(tmp_path: Path, *, owner: str = NEW) -> AgentRegistry:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner=owner,
        machine="p1b-node",
        hyprial_home=home,
    )
    registry.create("alpha")
    registry.create("beta")
    # Existing file-only entries remain readable, but have no product writer.
    for actor, entry, value in (
        ("alpha", "deepseek.json", "SENTINEL-ALPHA"),
        ("beta", "openai.json", "SENTINEL-BETA"),
    ):
        path = Path(registry.home_receipt(actor).path) / "secrets" / entry
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text(json.dumps({"key": value}), encoding="utf-8")
        path.chmod(0o600)
    registry.grant_secret(
        "alpha",
        grant_id="deepseek",
        source=SecretSource.AGENT_PRIVATE,
        entry_id="deepseek.json",
        field_name="key",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )
    registry.grant_secret(
        "beta",
        grant_id="openai",
        source=SecretSource.AGENT_PRIVATE,
        entry_id="openai.json",
        field_name="key",
        environment_names=("OPENAI_API_KEY",),
        revision=1,
    )
    _OPEN_REGISTRIES.append(registry)
    return registry


@pytest.fixture(autouse=True)
def _close_registries():
    yield
    while _OPEN_REGISTRIES:
        _OPEN_REGISTRIES.pop().close()


def _compose_launch(registry: AgentRegistry, home: Path, channel: WorkerChannel):
    """The daemon factory, via the SAME shared implementation it calls."""

    from hyprial.identity.impl.agents.home.environment import compose_worker_child_launch

    return compose_worker_child_launch(
        registry=registry,
        hyprial_home=home,
        channel=channel,
        environ={"PATH": "/bin"},
        agent_name=channel.actor.split(":")[-1],
    )


def test_t06_each_agent_receives_only_its_own_grants(tmp_path):
    registry = _seeded_registry(tmp_path)
    home = tmp_path / "home"
    alpha = _compose_launch(
        registry, home, _channel(tmp_path, actor="agent:p1b:node:alpha")
    )
    beta = _compose_launch(
        registry, home, _channel(tmp_path, actor="agent:p1b:node:beta")
    )
    alpha_env = alpha.environment.for_exec()
    beta_env = beta.environment.for_exec()
    assert alpha_env.get("DEEPSEEK_API_KEY") == "SENTINEL-ALPHA"
    assert "OPENAI_API_KEY" not in alpha_env
    assert beta_env.get("OPENAI_API_KEY") == "SENTINEL-BETA"
    assert "DEEPSEEK_API_KEY" not in beta_env
    assert alpha.grants == (("deepseek", 1),)
    assert beta.grants == (("openai", 1),)


def test_t11_stale_grant_fails_loud_no_ambient_fallback(tmp_path):
    registry = _seeded_registry(tmp_path)
    # A stale grant: the incarnation moved on (the mixed-version shape the
    # resolver fences).  The compose must raise — there is no path that
    # silently drops the secret and continues with an ambient environment.
    with registry._lock, registry._db:  # noqa: SLF001 - staleness fixture
        registry._db.execute("PRAGMA foreign_keys=OFF")
        registry._db.execute(
            "UPDATE agent_secret_grants SET entity_token = 'stale-token' "
            "WHERE agent = 'alpha'"
        )
        registry._db.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(SecretResolutionError):
        _compose_launch(
            registry, tmp_path / "home", _channel(tmp_path, actor="agent:p1b:node:alpha")
        )


def test_t07_launch_spec_projection_carries_no_private_env():
    """The complete environment never enters the persisted launch spec."""

    spec = _spec(model_provider="deepseek")
    projected = spec.to_json()
    rendered = json.dumps(projected)
    for needle in ("DEEPSEEK_API_KEY", "environment", "SENTINEL"):
        assert needle not in rendered


def _host_claude_environment(registry, tmp_path):
    return compose_worker_child_launch(
        registry=registry,
        hyprial_home=tmp_path / "home",
        channel=_channel(tmp_path, actor="agent:p1b:node:alpha"),
        environ={
            "PATH": "/bin",
            "HYPRIAL_CLAUDE_EXECUTABLE": "/untrusted/parent/claude",
        },
        agent_name="alpha",
        harness="claude",
    ).environment.for_exec()


def test_host_claude_executable_is_generated_only():
    name = "HYPRIAL_CLAUDE_EXECUTABLE"
    assert name in GENERATED_CHILD_ENVIRONMENT_NAMES
    assert name not in BASE_CHILD_ENVIRONMENT_NAMES
    with pytest.raises(ValueError, match="base child environment"):
        build_complete_child_environment(base={name: "/untrusted/parent/claude"}, generated={})


def test_host_claude_executable_resolves_again_for_each_launch(tmp_path, monkeypatch):
    registry = _seeded_registry(tmp_path)
    which = Mock(side_effect=["/tmp/fake-claude-A", "/tmp/fake-claude-B"])
    monkeypatch.setattr("shutil.which", which)

    first = _host_claude_environment(registry, tmp_path)
    second = _host_claude_environment(registry, tmp_path)

    assert first["HYPRIAL_CLAUDE_EXECUTABLE"] == "/tmp/fake-claude-A"
    assert second["HYPRIAL_CLAUDE_EXECUTABLE"] == "/tmp/fake-claude-B"
    assert which.call_args_list == [(('claude',), {}), (('claude',), {})]


def test_host_claude_executable_fixed_lookup_control(tmp_path, monkeypatch):
    registry = _seeded_registry(tmp_path)
    which = Mock(return_value="/tmp/fake-claude-A")
    monkeypatch.setattr("shutil.which", which)

    first = _host_claude_environment(registry, tmp_path)
    second = _host_claude_environment(registry, tmp_path)

    assert first["HYPRIAL_CLAUDE_EXECUTABLE"] == "/tmp/fake-claude-A"
    assert second["HYPRIAL_CLAUDE_EXECUTABLE"] == "/tmp/fake-claude-A"
    assert which.call_count == 2


@pytest.mark.parametrize("discovered", ["/tmp/fake-claude-A", None, ""])
def test_host_claude_executable_ignores_parent_and_logs_once(
    tmp_path, monkeypatch, caplog, discovered
):
    registry = _seeded_registry(tmp_path)
    which = Mock(return_value=discovered)
    monkeypatch.setattr("shutil.which", which)
    monkeypatch.setenv("HYPRIAL_CLAUDE_EXECUTABLE", "/untrusted/parent/claude")

    with caplog.at_level("INFO", logger="hyprial.identity.impl.agents.home.environment"):
        environment = _host_claude_environment(registry, tmp_path)

    which.assert_called_once_with("claude")
    assert environment.get("HYPRIAL_CLAUDE_EXECUTABLE") != "/untrusted/parent/claude"
    if discovered:
        assert environment["HYPRIAL_CLAUDE_EXECUTABLE"] == discovered
        expected = f"系统 claude 路径: {discovered}"
    else:
        assert "HYPRIAL_CLAUDE_EXECUTABLE" not in environment
        expected = "系统 claude 未找到,回退 SDK bundled"
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "hyprial.identity.impl.agents.home.environment"
    ]
    assert messages == [expected]
    assert "/untrusted/parent/claude" not in caplog.text
    assert "SENTINEL-ALPHA" not in caplog.text
