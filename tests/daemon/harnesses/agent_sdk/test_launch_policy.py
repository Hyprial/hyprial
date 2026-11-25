"""Member launch policy carriage, the worker's acknowledgement, and its invariants.

Migrated in I6-5b from ``tests/daemon/authority_set/test_launch_handshake.py``
and ``tests/daemon/authority_service/test_invariants.py`` when the standalone
AuthoritySet packages were deleted.  A cell member's policy reaches a Claude
headless worker only through the launch description: the Agent SDK reads
``allowedTools`` / ``disallowedTools`` once at process start.  The worker
recomputes the policy digest over the lists it actually received, and the
reconcile check (``cells/reconcile/policy.require_policy_ack``) refuses an
absent or different acknowledgement.  The policy path reaches no IM, Lark or
squire module and installs no live ``can_use_tool`` approval hook.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import hyprial.daemon.impl.harnesses.agent_sdk as agent_sdk_module
from hyprial.daemon.impl.cells.reconcile.policy import (
    PolicyHandshakeError,
    policy_digest,
    protocol_version,
    require_policy_ack,
    resolve_member_policy,
)
from hyprial.daemon.impl.harnesses.agent_sdk import (
    ClaudeAgentSdkProcess,
    IsolatedAgentSdkClient,
)
from hyprial.daemon.impl.harnesses.worker_channel import (
    WorkerAuthorityPolicy,
    build_worker_channel,
)
from hyprial.kernel import HarnessLaunchSpec

WORKER = Path(agent_sdk_module.__file__).with_name("_agent_sdk_worker.py")
AUTHORITY_PROTOCOL_VERSION = protocol_version()
_FAKE_SDK = '''
"""Minimal claude-agent-sdk double for the ready-frame handshake."""

import asyncio


class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class ResultMessage:
    def __init__(self, result=None, is_error=False):
        self.result = result
        self.is_error = is_error


class ClaudeSDKClient:
    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def query(self, prompt):
        return None

    async def receive_response(self):
        yield ResultMessage(result="ok", is_error=False)
'''


def _channel(tmp_path: Path, authority: WorkerAuthorityPolicy | None = None):
    return build_worker_channel(
        actor="agent:worker:node:test",
        session_ref="session-1",
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "state",
        authority=authority,
    )


def _policy(
    *,
    allow: tuple[str, ...] = ("Read",),
    deny: tuple[str, ...] = (),
    version: int = 1,
) -> WorkerAuthorityPolicy:
    rules = resolve_member_policy("claude", tuple(allow), tuple(deny))
    assert rules is not None
    return WorkerAuthorityPolicy(
        launch_options=dict(rules.launch_options()),
        version=version,
    )


def _client(tmp_path: Path, channel) -> IsolatedAgentSdkClient:
    return IsolatedAgentSdkClient(
        HarnessLaunchSpec(
            harness="claude",
            name="claude-worker",
            headless=True,
            cwd=str(tmp_path),
        ),
        worker_channel=channel,
        worker_command=(sys.executable, "-c", "pass"),
    )


def _run_worker(tmp_path: Path, options: dict[str, object]):
    package = tmp_path / "claude_agent_sdk"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text(_FAKE_SDK, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(tmp_path),
        "HYPRIAL_AGENT_SDK_OPTIONS": json.dumps(options, separators=(",", ":")),
    }
    result = subprocess.run(
        [sys.executable, str(WORKER)],
        input='{"op":"stop"}\n',
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    frames = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result, frames


def test_explicit_policy_replaces_the_channel_lists(tmp_path: Path) -> None:
    policy = _policy(
        allow=("Read", "Bash(hyprial:*)"),
        deny=("Bash(hyprial daemon:*)",),
        version=3,
    )
    channel = _channel(tmp_path, policy)

    assert channel.allowed_tools == tuple(policy.launch_options["allowedTools"])
    assert channel.disallowed_tools == tuple(
        policy.launch_options["disallowedTools"]
    )
    assert channel.authority == policy


@pytest.mark.parametrize("version", [0, -1, True, "1"])
def test_policy_version_must_be_a_positive_int(version: object) -> None:
    with pytest.raises(ValueError):
        WorkerAuthorityPolicy(
            launch_options={"allowedTools": ["Read"]}, version=version
        )


def test_launch_options_without_policy_have_no_authority_key(tmp_path: Path) -> None:
    channel = _channel(tmp_path)
    options = json.loads(_client(tmp_path, channel)._env["HYPRIAL_AGENT_SDK_OPTIONS"])

    assert options["allowedTools"] == list(channel.allowed_tools)
    assert all(tool.startswith("mcp__harness-bridge__") for tool in options["allowedTools"])
    assert "disallowedTools" not in options
    assert "authorityVersion" not in options


def test_launch_options_carry_the_policy_and_version(tmp_path: Path) -> None:
    policy = _policy(
        allow=("Read", "Bash(hyprial:*)"),
        deny=("Bash(hyprial daemon:*)",),
        version=3,
    )
    options = json.loads(
        _client(tmp_path, _channel(tmp_path, policy))._env["HYPRIAL_AGENT_SDK_OPTIONS"]
    )

    assert options["allowedTools"] == policy.launch_options["allowedTools"]
    assert options["disallowedTools"] == policy.launch_options["disallowedTools"]
    assert options["authorityVersion"] == 3


def test_process_builds_a_client_with_the_policy_options(tmp_path: Path) -> None:
    policy = _policy(
        allow=("Read",),
        deny=("Bash(rm:*)",),
        version=9,
    )
    process = ClaudeAgentSdkProcess(
        HarnessLaunchSpec(
            harness="claude",
            name="claude-worker",
            headless=True,
            cwd=str(tmp_path),
        ),
        worker_channel=_channel(tmp_path, policy),
        client_factory=lambda: None,
    )

    try:
        client = process._build_client()
        options = json.loads(client._env["HYPRIAL_AGENT_SDK_OPTIONS"])

        assert options["allowedTools"] == ["Read"]
        assert options["disallowedTools"] == ["Bash(rm:*)"]
        assert options["authorityVersion"] == 9
    finally:
        process.stop()


def test_real_worker_ready_frame_is_accepted_by_the_reconcile_check(tmp_path: Path) -> None:
    allow = ["Read", "mcp__harness-bridge__harness_read"]
    deny = ["Bash(rm:*)", "Bash(hyprial daemon:*)"]
    result, frames = _run_worker(
        tmp_path,
        {"allowedTools": allow, "disallowedTools": deny, "authorityVersion": 7},
    )

    assert result.returncode == 0, result.stderr
    ready = frames[0]
    assert ready["type"] == "ready"
    expected = policy_digest(
        harness="claude", headless=True, allow=allow, deny=deny
    )
    assert ready["authority"] == {
        "enabled": True,
        "protocol": AUTHORITY_PROTOCOL_VERSION,
        "policyDigest": expected,
        "version": 7,
    }
    require_policy_ack(ready, digest=expected, version=7)


def test_client_exposes_the_real_worker_ready_frame(tmp_path: Path) -> None:
    package = tmp_path / "claude_agent_sdk"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text(_FAKE_SDK, encoding="utf-8")
    allow = ["Read"]
    deny: list[str] = []
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(tmp_path),
    }
    channel = build_worker_channel(
        actor="agent:worker:node:test",
        session_ref="session-1",
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "state",
        authority=_policy(
            allow=tuple(allow), deny=tuple(deny), version=6
        ),
    )

    async def start() -> dict[str, object] | None:
        client = IsolatedAgentSdkClient(
            HarnessLaunchSpec(
                harness="claude",
                name="claude-worker",
                headless=True,
                cwd=str(tmp_path),
            ),
            env=env,
            worker_channel=channel,
            worker_command=(sys.executable, str(WORKER)),
            startup_timeout_seconds=10.0,
        )
        async with client:
            return client.last_ready

    ready = asyncio.run(start())

    assert ready is not None
    assert ready["authority"] == {
        "enabled": True,
        "protocol": AUTHORITY_PROTOCOL_VERSION,
        "policyDigest": policy_digest(
            harness="claude", headless=True, allow=allow, deny=deny
        ),
        "version": 6,
    }


def test_real_worker_without_policy_emits_no_authority_key(tmp_path: Path) -> None:
    result, frames = _run_worker(
        tmp_path, {"allowedTools": ["Read"], "disallowedTools": ["Write"]}
    )

    assert result.returncode == 0, result.stderr
    ready = frames[0]
    assert ready["type"] == "ready"
    assert "authority" not in ready


def test_tampered_ready_frame_is_rejected(tmp_path: Path) -> None:
    allow = ["Read"]
    deny: list[str] = []
    result, frames = _run_worker(
        tmp_path,
        {"allowedTools": allow, "disallowedTools": deny, "authorityVersion": 4},
    )

    assert result.returncode == 0, result.stderr
    ready = frames[0]
    expected = policy_digest(
        harness="claude", headless=True, allow=allow, deny=deny
    )

    for field, value in (
        ("policyDigest", "0" * 64),
        ("version", 5),
        ("enabled", False),
        ("protocol", 2),
    ):
        tampered = copy.deepcopy(ready)
        tampered["authority"][field] = value
        with pytest.raises(PolicyHandshakeError):
            require_policy_ack(tampered, digest=expected, version=4)


REPO = Path(__file__).resolve().parents[4]
SRC = REPO / "src"
ROOTS = (
    "hyprial.daemon.impl.cells.reconcile.policy",
    "hyprial.daemon.impl.harnesses.agent_sdk.authority_model",
    "hyprial.daemon.impl.harnesses.turn_delivery.channel.worker_channel",
)
KERNEL_CHANNEL_CONTRACT = "hyprial.kernel.impl.contracts.channels.lark"
FORBIDDEN_PREFIXES = (
    "hyprial.daemon.impl.adapters",
    "hyprial.shell.impl.cli.commands.adapter",
)


def _module_path(name: str) -> Path | None:
    parts = name.split(".")
    file = SRC.joinpath(*parts).with_suffix(".py")
    if file.is_file():
        return file
    init = SRC.joinpath(*parts) / "__init__.py"
    return init if init.is_file() else None


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _relative(current: str, level: int, module: str | None) -> str:
    parts = current.split(".")
    path = _module_path(current)
    if path is None or path.name != "__init__.py":
        parts = parts[:-1]
    if level > 1:
        parts = parts[: -(level - 1)]
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    current = _module_name(path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (
                _relative(current, node.level, node.module)
                if node.level
                else (node.module or "")
            )
            if base:
                found.add(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidate = f"{base}.{alias.name}" if base else alias.name
                if _module_path(candidate) is not None:
                    found.add(candidate)
    return found


def _reachable() -> dict[str, Path]:
    seen: dict[str, Path] = {}
    queue = list(ROOTS)
    while queue:
        name = queue.pop()
        if name in seen or not name.startswith("hyprial"):
            continue
        path = _module_path(name)
        if path is None:
            continue
        seen[name] = path
        queue.extend(
            dependency
            for dependency in _imports(path)
            if dependency.startswith("hyprial") and dependency not in seen
        )
    return seen


def test_policy_call_chain_has_no_im_lark_or_squire_import() -> None:
    reachable = _reachable()

    offenders = sorted(
        name
        for name in reachable
        if name != KERNEL_CHANNEL_CONTRACT
        and (
            name.startswith(FORBIDDEN_PREFIXES)
            or "lark" in name.lower()
            or "squire" in name.lower()
        )
    )

    assert offenders == []
    assert "hyprial.daemon.impl.harnesses.turn_delivery.channel.worker_channel" in (
        reachable
    )


def test_policy_call_chain_has_no_can_use_tool_hook() -> None:
    reachable = _reachable()

    offenders = sorted(
        name
        for name, path in reachable.items()
        if "can_use_tool" in path.read_text(encoding="utf-8")
    )

    assert offenders == []
