"""The three harness carriers emit the SAME daemon-bound worker identity.

hyprial-developer acceptance 3 (2026-09-14): pi, claude, and codex workers must
inject ``HYPRIAL_WORKER_ACTOR`` / ``HYPRIAL_WORKER_SESSION_REF`` isomorphically
-- a codex worker shelling out to ``hyprial workflow`` must not be silently
denied (or worse, silently re-attributed to the human) because its carrier
forgot the binding.  The names are explicit entries of P1a's child-env
whitelist (PR #513); acceptance 5's combination check runs against the real
whitelist when the module exists in this tree and is annotated with the
cross-branch dependency otherwise.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient
from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

CANONICAL = "agent:acme:hyprial-node:worker-1"
IDENTITY_KEYS = ("HYPRIAL_WORKER_ACTOR", "HYPRIAL_WORKER_SESSION_REF")


def _channel() -> object:
    return build_worker_channel(
        actor=CANONICAL,
        session_ref="sess-1",
        hyprial_home=Path("/h/home"),
        state_dir=Path("/h/state"),
    )


def _identity(env: dict[str, str] | None) -> dict[str, str]:
    assert env is not None
    return {key: env[key] for key in IDENTITY_KEYS}


def test_channel_identity_environment_is_the_two_names_plus_pinning() -> None:
    env = _channel().identity_environment()
    assert env["HYPRIAL_WORKER_ACTOR"] == CANONICAL
    assert env["HYPRIAL_WORKER_SESSION_REF"] == "sess-1"
    # daemon pinning rides along: a shell-out reaches THIS daemon
    assert env["HYPRIAL_HOME"] == "/h/home"
    assert env["HARNESS_STATE_DIR"] == "/h/state"


def test_pi_carrier_keeps_the_identity() -> None:
    from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient

    spec = HarnessLaunchSpec(harness="pi", name="pi-worker", headless=True)
    client = PiRpcClient(spec, "stable-session", worker_channel=_channel())
    assert _identity(client._env) == {
        "HYPRIAL_WORKER_ACTOR": CANONICAL,
        "HYPRIAL_WORKER_SESSION_REF": "sess-1",
    }


def test_claude_carrier_injects_the_identity() -> None:
    spec = HarnessLaunchSpec(harness="claude", name="claude-worker", headless=True)
    client = IsolatedAgentSdkClient(spec, worker_channel=_channel())
    assert _identity(client._env) == {
        "HYPRIAL_WORKER_ACTOR": CANONICAL,
        "HYPRIAL_WORKER_SESSION_REF": "sess-1",
    }
    # and the identity survives a caller-provided env delta (caller loses)
    overridden = IsolatedAgentSdkClient(
        spec,
        env={"HYPRIAL_WORKER_ACTOR": "must-not-win"},
        worker_channel=_channel(),
    )
    assert overridden._env["HYPRIAL_WORKER_ACTOR"] == CANONICAL


def test_codex_carrier_injects_the_identity() -> None:
    spec = HarnessLaunchSpec(harness="codex", name="codex-worker", headless=True)
    client = CodexAppServerClient(spec, worker_channel=_channel())
    assert _identity(client._env) == {
        "HYPRIAL_WORKER_ACTOR": CANONICAL,
        "HYPRIAL_WORKER_SESSION_REF": "sess-1",
    }


def test_codex_containerized_carrier_injects_the_identity() -> None:
    spec = HarnessLaunchSpec(
        harness="codex", name="codex-boxed", headless=True, containerized=True
    )
    client = CodexAppServerClient(spec, worker_channel=_channel())
    assert _identity(client._env) == {
        "HYPRIAL_WORKER_ACTOR": CANONICAL,
        "HYPRIAL_WORKER_SESSION_REF": "sess-1",
    }


def test_without_a_channel_no_identity_is_injected() -> None:
    spec = HarnessLaunchSpec(harness="claude", name="claude-worker", headless=True)
    client = IsolatedAgentSdkClient(spec)
    assert not any(key in client._env for key in IDENTITY_KEYS)


def test_all_three_carriers_emit_identical_identity_env() -> None:
    """The coverage table as an executable: same channel, byte-identical
    identity on every carrier -- isomorphism, not three implementations."""
    from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient

    channel = _channel()
    pi = PiRpcClient(
        HarnessLaunchSpec(harness="pi", name="pi-worker", headless=True),
        "stable-session",
        worker_channel=channel,
    )
    claude = IsolatedAgentSdkClient(
        HarnessLaunchSpec(harness="claude", name="claude-worker", headless=True),
        worker_channel=_channel(),
    )
    codex = CodexAppServerClient(
        HarnessLaunchSpec(harness="codex", name="codex-worker", headless=True),
        worker_channel=_channel(),
    )
    expected = {
        "HYPRIAL_WORKER_ACTOR": CANONICAL,
        "HYPRIAL_WORKER_SESSION_REF": "sess-1",
    }
    assert _identity(pi._env) == _identity(claude._env) == _identity(codex._env)
    assert _identity(pi._env) == expected


def test_child_env_whitelist_carries_both_names_when_present() -> None:
    """hyprial-developer acceptance 5: with P1a's whitelist active the two names
    must survive -- otherwise the P1b cutover strips them and every fenced
    PAC write would fail closed.  This tree does not carry the whitelist
    module yet (PR #513, feat/agent-home-p1a-data); when it merges, this
    same test turns into the combination check instead of skipping."""
    if importlib.util.find_spec("hyprial.identity.impl.agents.home.environment") is None:
        pytest.skip(
            "agents.environment lands with PR #513 (feat/agent-home-p1a-data); "
            "cross-point: this branch's two names are verified against that "
            "branch's GENERATED_CHILD_ENVIRONMENT_NAMES and must be re-run "
            "when #513 merges"
        )
    from hyprial.identity.impl.agents.home.environment import GENERATED_CHILD_ENVIRONMENT_NAMES

    assert "HYPRIAL_WORKER_ACTOR" in GENERATED_CHILD_ENVIRONMENT_NAMES
    assert "HYPRIAL_WORKER_SESSION_REF" in GENERATED_CHILD_ENVIRONMENT_NAMES
