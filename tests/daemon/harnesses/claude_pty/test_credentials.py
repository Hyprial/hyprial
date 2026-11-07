"""PTY source policy with real registry/composition and a synthetic child only."""
from pathlib import Path
import json
import os
import secrets
import sys
import time

import pytest

from hyprial.identity import AgentRegistry, AgentSecretStore, compose_worker_child_launch
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.claude.runtime import ClaudeRuntimeError
from hyprial.daemon.impl.harnesses.protocol.common import PtyHarnessProcess
from hyprial.daemon.impl.harnesses.runtime.launcher import HarnessLauncher
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel


@pytest.fixture
def scenario(tmp_path):
    ambient = tmp_path / "ambient"
    native = ambient / ".claude"
    native.mkdir(parents=True, mode=0o700)
    marker = native / ".credentials.json"
    marker.write_text('{"synthetic_state":"ambient"}')
    marker.chmod(0o600)
    home = tmp_path / "hyprial"
    home.mkdir(mode=0o700)
    fake = tmp_path / "fake.py"
    fake.write_text(
        "from pathlib import Path\nimport os, sys\n"
        "for line in sys.stdin:\n"
        " root = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home()/'.claude')))\n"
        " print('AMBIENT_TURN' if (root/'.credentials.json').is_file() else 'OWN_ROOT', flush=True)\n"
    )
    registry = AgentRegistry(tmp_path / "registry.sqlite3", owner="test", machine="test", hyprial_home=home)
    processes = []

    def launch_case(*, managed=False, nickname=None, headless=False, own_native=False, env_grant=False):
        if env_grant:
            registry.create("custodian")
            store = AgentSecretStore(Path(registry.home_receipt("custodian").path))
            store.set(service="claude", field="CLAUDE_CODE_OAUTH_TOKEN", value=secrets.token_hex(24),
                      actor="user:test", via="cli", at_ms=1)
            registry.set_default_claude_credential("custodian", "claude")
        # A misleading name must never confer workflow ownership.
        agent = registry.create("wf-resident", preferred_harness="claude",
                                capabilities={"creationOrigin": "managed-launch"} if managed else {})
        if own_native:
            own = Path(registry.home_receipt(agent.actor).path) / "secrets/native/claude"
            own.mkdir(parents=True, mode=0o700)
            own.parent.chmod(0o700)
            own.parent.parent.chmod(0o700)
        channel = build_worker_channel(actor=agent.uri, session_ref="pty", hyprial_home=home,
            state_dir=tmp_path / "state", owner="test", node_id="test")
        complete = compose_worker_child_launch(registry=registry, hyprial_home=home,
            channel=channel, environ={"HOME": str(ambient), "PATH": os.environ["PATH"]}, agent_name=agent.actor)
        spec = HarnessLaunchSpec(harness="claude", name=agent.actor, headless=headless,
            nickname=nickname, cwd=str(tmp_path))
        launcher = HarnessLauncher(commands={"claude": (sys.executable, str(fake))},
            worker_channel_factory=lambda spec: channel,
            child_environment_factory=lambda spec, channel: complete)
        process = launcher.start(spec)
        if process is not None:
            processes.append(process)
        return process

    yield launch_case, tmp_path
    for process in processes:
        process.stop()
        if process._child.stderr is not None:
            process._child.stderr.close()
        assert not process.running
    registry.close()


def events(root):
    return [json.loads(line) for path in (root / "state/logs").rglob("*.jsonl")
            for line in path.read_text().splitlines()
            if json.loads(line).get("event") == "worker.claude.credential_source"]


@pytest.mark.parametrize("headless", [False, True])
@pytest.mark.parametrize("ownership", ["creation", "pac-marker"])
def test_workflow_pty_missing_never_spawns(scenario, monkeypatch, ownership, headless):
    launch, root = scenario
    spawned = []
    monkeypatch.setattr(PtyHarnessProcess, "spawn", lambda *a, **kw: spawned.append(kw))
    with pytest.raises(ClaudeRuntimeError) as caught:
        launch(managed=ownership == "creation", headless=headless,
               nickname="pac:graph:actor:1:effect" if ownership == "pac-marker" else None)
    assert caught.value.code == "CLAUDE_CREDENTIAL_MISSING"
    assert caught.value.permanent_start_failure is True
    assert spawned == []
    assert events(root)[-1]["credentialSource"] == "missing"


def test_resident_pty_still_runs_in_own_root_and_reports_missing_source(scenario):
    launch, root = scenario
    process = launch(nickname="pac:not-an-ownership-marker")
    operation = process.inject("synthetic turn")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = process.retrieve(operation)
        if "OWN_ROOT" in result.output:
            break
        time.sleep(.05)
    assert "OWN_ROOT" in result.output
    assert "AMBIENT_TURN" not in result.output
    own = root / "hyprial/agents/wf-resident"
    assert (own / "secrets/native/claude/projects").resolve() == (own / "state/sessions/claude").resolve()
    assert events(root)[-1]["credentialSource"] == "ambient"


def test_workflow_pty_env_grant_does_not_expose_ambient_native(scenario):
    launch, root = scenario
    process = launch(managed=True, env_grant=True)
    operation = process.inject("synthetic turn")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = process.retrieve(operation)
        if "OWN_ROOT" in result.output:
            break
        time.sleep(.05)
    assert "OWN_ROOT" in result.output
    assert "AMBIENT_TURN" not in result.output
    assert events(root)[-1]["credentialSource"] == "env-grant"


def test_workflow_pty_fileless_own_native_still_runs(scenario):
    launch, root = scenario
    process = launch(managed=True, own_native=True)
    assert process.running
    assert events(root)[-1]["credentialSource"] == "agent-native"
