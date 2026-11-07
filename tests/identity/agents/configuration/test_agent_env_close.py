"""P1b B2: every remaining spawn site replaces — never merges — the ambient env.

Per-entry env equality (T04 parametrized), the codex provider path via the
resolver, ambient provider keys default-denied, and the M04 broker mutation
contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.home.environment import (
    ChildEnvironmentLaunch,
    build_complete_child_environment,
    whitelist_replacement_environment,
)
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

AMBIENT = "P1BB2_AMBIENT_LEAK"


def _channel(tmp_path: Path, actor: str = "agent:p1b:node:worker"):
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
        owner="new-owner",
    )


def _spec(harness: str = "codex", **kwargs):
    from hyprial.kernel import HarnessLaunchSpec

    return HarnessLaunchSpec(harness, "worker", True, **kwargs)


def _launch(channel, secrets=(), *, agent_home="/opt/hyprial/agents/worker"):
    return ChildEnvironmentLaunch(
        environment=build_complete_child_environment(
            base={"PATH": "/usr/bin:/bin", "HOME": "/Users/who"},
            generated={
                **channel.identity_environment(),
                "HYPRIAL_AGENT_DIR": str(agent_home),
            },
            secrets=secrets,
        ),
        actor=channel.actor,
        grants=tuple(
            (s.grant.grant_id, s.grant.revision) for s in secrets
        ),
    )


# ── the shared filter itself ──────────────────────────────────────────────────


def test_whitelist_passes_only_frozen_names(monkeypatch):
    monkeypatch.setenv(AMBIENT, "leak")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-key")
    monkeypatch.setenv("HYPRIAL_OWNER", "daemon-owner")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    result = whitelist_replacement_environment(dict(__import__("os").environ))
    assert AMBIENT not in result
    assert "DEEPSEEK_API_KEY" not in result  # provider keys are NOT ambient
    assert result["HYPRIAL_OWNER"] == "daemon-owner"  # generated passes
    assert result["PATH"] == "/usr/bin:/bin"  # base passes


def test_whitelist_overlays_deltas_in_order(monkeypatch):
    monkeypatch.setenv(AMBIENT, "leak")
    result = whitelist_replacement_environment(
        {"PATH": "/bin", "HOME": "/Users/who"},
        {"DEEPSEEK_API_KEY": "resolver-key"},
        {"DEEPSEEK_API_KEY": "later-wins"},
    )
    assert result["DEEPSEEK_API_KEY"] == "later-wins"
    assert AMBIENT not in result


def test_claude_context_sizing_rides_the_vocabulary_into_a_managed_worker(monkeypatch):
    """A deployment's own context sizing must reach the child, not be dropped.

    2026-10-17 runtime incident: a session on ``deepseek-flash`` ran to the
    provider's limit with no compact boundary, because Claude Code cannot
    derive a window from that model id.  The provider seam now supplies the
    default numbers, but the layer that decides whether an OPERATOR's value
    survives is this one: the names have to be in the frozen vocabulary, or
    an exported override is stripped from the base and every managed worker
    silently runs on the default.  P2 keeps them too -- they are not
    P2_CONTROLLED names, and nothing here is a root or a capability.
    """

    names = {
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "400000",
    }
    for name, value in names.items():
        monkeypatch.setenv(name, value)

    whitelisted = whitelist_replacement_environment(dict(__import__("os").environ))
    assert whitelisted.items() >= names.items()

    complete = build_complete_child_environment(
        base={"PATH": "/usr/bin:/bin", **names},
        generated={},
        secrets=(),
    )
    assert complete.for_exec().items() >= names.items()

    # A P2 profile must not strip them the way it strips the native roots.
    from hyprial.identity.impl.agents.home.environment import (
        apply_runtime_environment_profile,
        P2_CONTROLLED_ENVIRONMENT_NAMES,
    )

    assert not set(names) & P2_CONTROLLED_ENVIRONMENT_NAMES
    profiled = apply_runtime_environment_profile(
        {**names, "SSH_AUTH_SOCK": "/signed/sock"}, {"HOME": "/agent/home"}
    )
    assert profiled.items() >= names.items()
    assert "SSH_AUTH_SOCK" not in profiled  # the profile still owns the socket


def test_local_provider_endpoints_ride_the_vocabulary_into_a_managed_worker(
    monkeypatch,
):
    endpoints = {
        "HYPRIAL_OPENAI_BASE_URL": "http://127.0.0.1:54321/v1",
        "HYPRIAL_ANTHROPIC_BASE_URL": "http://127.0.0.1:54321",
    }
    for name, value in endpoints.items():
        monkeypatch.setenv(name, value)

    whitelisted = whitelist_replacement_environment(dict(__import__("os").environ))

    assert whitelisted.items() >= endpoints.items()


def test_whitelist_git_triple_only_complete(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://x/.insteadOf")
    # VALUE_0 missing: partial group drops
    result = whitelist_replacement_environment(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.https://x/.insteadOf",
        }
    )
    assert "GIT_CONFIG_COUNT" not in result
    result = whitelist_replacement_environment(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.https://x/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://y",
        }
    )
    assert result["GIT_CONFIG_COUNT"] == "1"


# ── per-site spawn env equality (T04, parametrized) ─────────────────────────


def test_codex_process_forwards_complete_launch(tmp_path, monkeypatch):
    """The launcher's codex factory mints the channel ONCE and hands the
    complete environment to the process, which forwards it to every
    (re)connected app-server client."""

    from hyprial.daemon.impl.harnesses.runtime.launcher import HarnessLauncher

    minted: list[object] = []

    def factory(spec):
        channel = _channel(tmp_path)
        minted.append(channel)
        return channel

    launcher = HarnessLauncher(
        worker_channel_factory=factory,
        child_environment_factory=lambda spec, channel: _launch(channel),
    )
    process = launcher._codex_app_server_factory(_spec())
    try:
        assert len(minted) == 1  # single mint
        launch = getattr(process, "_complete_launch")
        assert launch is not None
        frozen = launch.environment.for_exec()
        assert frozen["HYPRIAL_OWNER"] == "new-owner"
    finally:
        process.stop()  # drains the streaming threads + process group


def test_codex_client_spawn_environment(tmp_path, monkeypatch):
    from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient

    channel = _channel(tmp_path)
    launch = _launch(channel)
    client = CodexAppServerClient(
        _spec(),
        command=("true",),
        complete_launch=launch,
    )
    monkeypatch.setenv(AMBIENT, "leak")
    environment = client._spawn_environment()
    frozen = launch.environment.for_exec()
    assert environment is not None
    assert environment.items() >= frozen.items()
    assert AMBIENT not in environment


def test_agent_sdk_env_is_complete_replacement(tmp_path, monkeypatch):
    from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient

    from types import SimpleNamespace
    from hyprial.identity.impl.agents.runtime.secrets import SecretGrant, SecretSource

    channel = _channel(tmp_path)
    grant = SecretGrant("worker", "incarnation", "claude", SecretSource.AGENT_PRIVATE,
                        "claude", "key", ("CLAUDE_CODE_OAUTH_TOKEN",), 1)
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir(mode=0o700)
    launch = _launch(channel, secrets=[SimpleNamespace(
        grant=grant, environment=lambda: {"CLAUDE_CODE_OAUTH_TOKEN": "synthetic"},
    )], agent_home=agent_home)
    client = IsolatedAgentSdkClient(
        _spec("claude"),
        worker_command=("true",),
        worker_channel=channel,
        complete_launch=launch,
    )
    monkeypatch.setenv(AMBIENT, "leak")
    assert client._env is not None
    assert AMBIENT not in client._env
    assert client._env["HYPRIAL_WORKER_ACTOR"] == channel.actor
    assert "HYPRIAL_AGENT_SDK_OPTIONS" in client._env  # harness payload kept


def test_agent_sdk_legacy_env_is_whitelist_filtered(tmp_path, monkeypatch):
    from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient

    monkeypatch.setenv(AMBIENT, "leak")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    client = IsolatedAgentSdkClient(
        _spec("claude"),
        worker_command=("true",),
        env={"CUSTOM_DELTA": "kept"},
    )
    assert AMBIENT not in client._env
    assert client._env["PATH"] == "/usr/bin:/bin"
    assert client._env["CUSTOM_DELTA"] == "kept"


def test_pty_spawn_env_is_whitelist_filtered(monkeypatch):
    """The PTY seam through the REAL pty pair.

    A child that dumps its environment and exits is deliberately a
    "startup failure" to the probe (it exited) — that verdict is not this
    contract; the dump proves the env the exec consumer received.
    """

    from hyprial.daemon.impl.harnesses.protocol.common import HarnessStartError, PtyHarnessProcess

    monkeypatch.setenv(AMBIENT, "leak")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/signed/sock")
    import sys
    import tempfile

    dump = Path(tempfile.mkdtemp()) / "pty-env.json"
    script = (
        "import json,os;json.dump(dict(os.environ),open("
        f"{str(dump)!r},'w'"
        "))"
    )
    try:
        PtyHarnessProcess.spawn(
            "codex",
            (sys.executable, "-c", script),
            env={"CUSTOM_DELTA": "kept"},
        )
    except HarnessStartError:
        pass  # the child exiting is the probe's verdict, not this contract's
    observed = json.loads(dump.read_text(encoding="utf-8"))
    assert AMBIENT not in observed
    assert observed["TERM"] == "xterm-256color"  # BASE name passes
    assert observed["SSH_AUTH_SOCK"] == "/signed/sock"
    assert observed["CUSTOM_DELTA"] == "kept"


def test_exec_env_socket_replaces_ambient(monkeypatch):
    """The broker exec seam (M04's target): whitelist, never merge."""

    from hyprial.daemon.impl.harnesses.python_worker import _exec_env_socket

    monkeypatch.setenv(AMBIENT, "leak")
    monkeypatch.setenv("HYPRIAL_OWNER", "daemon-owner")
    env_seen: dict[str, str] = {}

    def fake_execvpe(argv0, argv, env):
        env_seen.update(env)
        raise SystemExit(0)

    monkeypatch.setattr(_exec_env_socket.os, "execvpe", fake_execvpe)

    class FakeSocket:
        sent = False

        def connect(self, _path):
            pass

        def close(self):
            pass

        def recv(self, _n):
            if not FakeSocket.sent:
                FakeSocket.sent = True
                return json.dumps(
                    {"HYPRIAL_OWNER": "handoff-owner"}
                ).encode() + b"\n"
            return b""

    monkeypatch.setattr(
        _exec_env_socket.socket, "socket", lambda *a, **k: FakeSocket()
    )
    monkeypatch.setattr(
        _exec_env_socket.sys, "argv", ["broker", "/sock", "--", "true"]
    )
    with pytest.raises(SystemExit):
        _exec_env_socket.main()
    assert AMBIENT not in env_seen
    assert env_seen["HYPRIAL_OWNER"] == "handoff-owner"  # delta wins


# ── codex provider path via the resolver ─────────────────────────────────────


def test_codex_provider_key_comes_from_resolver_not_home(tmp_path):
    """DEEPSEEK_API_KEY arrives via the complete launch (grant), and the
    provider configuration reads it from the environment it is handed —
    the HOME auth.json fallback is only reached when the key is absent
    (that fallback's removal is B3, deliberately not here)."""

    from hyprial.identity.impl.agents.runtime.secrets import SecretGrant, SecretSource
    from hyprial.daemon.impl.harnesses.model_provider import codex_provider_configuration

    class _Granted:
        def __init__(self):
            self.grant = SecretGrant(
                actor="worker",
                entity_token="tok",
                grant_id="deepseek",
                source=SecretSource.USER_PROVIDER,
                entry_id="deepseek.json",
                field_name="key",
                environment_names=("DEEPSEEK_API_KEY",),
                revision=2,
            )

        def environment(self):
            return {"DEEPSEEK_API_KEY": "SENTINEL-RESOLVER-KEY"}

    channel = _channel(tmp_path)
    launch = _launch(channel, secrets=[_Granted()])
    child_env = launch.environment.for_exec()
    assert child_env["DEEPSEEK_API_KEY"] == "SENTINEL-RESOLVER-KEY"
    args, provider_env = codex_provider_configuration(
        _spec(model_provider="deepseek"), child_env
    )
    assert provider_env["DEEPSEEK_API_KEY"] == "SENTINEL-RESOLVER-KEY"
    # the env_key mechanism: argv names the env var, never the value
    assert any("env_key" in arg for arg in args)
    assert "SENTINEL-RESOLVER-KEY" not in " ".join(args)


def test_codex_app_server_spawn_env_carries_the_provider_key(tmp_path):
    """The spawn seam, not just the configuration: a complete launch with
    no grant resolves the key through the HOME fallback (kept until B3),
    argv names ``env_key="DEEPSEEK_API_KEY"``, and the spawned child must
    then actually carry that variable.  Nightly E2E-006/E2E-013 failed
    every codex+deepseek turn with codex's own "Missing environment
    variable: `DEEPSEEK_API_KEY`" while the frozen mapping was returned
    verbatim and the resolved delta was dropped."""

    from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient

    home = tmp_path / "user-home"
    auth = home / ".pi" / "agent" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": "SENTINEL-HOME-KEY"}}),
        encoding="utf-8",
    )
    channel = _channel(tmp_path)
    launch = ChildEnvironmentLaunch(
        environment=build_complete_child_environment(
            base={"PATH": "/usr/bin:/bin", "HOME": str(home)},
            generated=channel.identity_environment(),
        ),
        actor=channel.actor,
        grants=(),
    )
    client = CodexAppServerClient(
        _spec(model_provider="deepseek"),
        command=("codex",),
        complete_launch=launch,
    )
    assert 'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"' in client.command
    environment = client._spawn_environment()
    assert environment is not None
    assert environment["DEEPSEEK_API_KEY"] == "SENTINEL-HOME-KEY"
    assert environment.items() >= launch.environment.for_exec().items()


def test_codex_env_carries_daemon_node_identity(tmp_path):
    """The codex worker's env carries the daemon-authority node id, so
    ``pac/context.py`` reads it instead of the hostname fallback."""

    channel = _channel(tmp_path)
    child_env = _launch(channel).environment.for_exec()
    import socket

    assert child_env["HYPRIAL_NODE_ID"] == "p1b-node"
    assert "p1b-node" != socket.gethostname()  # sentinel distinct
