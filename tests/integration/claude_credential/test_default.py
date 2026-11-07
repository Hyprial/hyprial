"""Isolated real daemon/PAC/SQLite chain; only Claude's model protocol is fake."""
from pathlib import Path
import base64
import json
import os
import secrets
import shutil
import sys
import tempfile
import time

from tests.integration.claude_credential.bait import prepare_bait_worker
from hyprial.identity import AgentRegistry
from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import IsolatedHarness

REPO = Path(__file__).resolve().parents[3]
OWNER = "claudecred-owner"
FIELD = "CLAUDE_CODE_OAUTH_TOKEN"


def wait_for(check, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.2)
    raise AssertionError("isolated scenario did not reach its expected state")


def test_pac_default_missing_and_resident_native():
    root = Path(tempfile.mkdtemp(prefix="hr-claudecred-", dir="/tmp"))
    node = IsolatedHarness(root)
    work = root / "work"
    work.mkdir()
    uv = shutil.which("uv")
    assert uv
    node.environment = {
        "PATH": os.environ["PATH"], "HOME": str(root / "user"),
        "HYPRIAL_HOME": str(node.home), "HARNESS_STATE_DIR": str(node.state),
        "HYPRIAL_NODE_ID": "claudecred-host", "HYPRIAL_NETWORK_ISOLATED": "1",
        "HYPRIAL_ZENOH_LISTEN": "", "HYPRIAL_ZENOH_CONNECT": "",
        "NO_PROXY": "127.0.0.1,localhost,::1", "UV_CACHE_DIR": str(root / "uv-cache"),
        "UV_TOOL_DIR": str(root / "uv-tools"), "PYTHONPATH": str(REPO / "src"),
        **{f"XDG_{name}_HOME": str(root / name.lower()) for name in ("CONFIG", "CACHE", "DATA", "STATE")},
    }
    Path(node.environment["HOME"]).mkdir(mode=0o700)
    node.write_owner(OWNER)
    # Model a pre-existing resident /login with a private, file-less native root.
    # No Keychain access: this tests only source admission, not vendor auth.
    node.state.mkdir(parents=True, exist_ok=True)
    registry = AgentRegistry(node.state / "agents.sqlite3", owner=OWNER,
                             machine="claudecred-host", hyprial_home=node.home.resolve())
    try:
        registry.create("resident", preferred_harness="claude")
        native = Path(registry.home_receipt("resident").path) / "secrets/native/claude"
        native.mkdir(mode=0o700, parents=True)
        native.parent.chmod(0o700)
        native.parent.parent.chmod(0o700)
    finally:
        registry.close()
    fake, audit = prepare_bait_worker(root, Path(node.environment["HOME"]),
                                     REPO / "tests/fixtures/harnesses/fake_claude_sdk.py")
    node.environment["CLAUDE_CONFIG_DIR"] = str(Path(node.environment["HOME"]) / ".claude")
    bootstrap = root / "daemon.py"
    bootstrap.write_text(audit +
        "from hyprial.daemon.impl.harnesses import agent_sdk\n"
        f"agent_sdk.sdk_worker_command = lambda **kw: {(uv, 'run', '--no-project', '--python', sys.executable, 'python', str(fake))!r}\n"
        # Exercise the real PTY route for the last graph, without invoking Claude.
        "from dataclasses import replace\n"
        "from pathlib import Path\n"
        "from hyprial.daemon.impl.harnesses.runtime.launcher import HarnessLauncher\n"
        "from hyprial.daemon.impl.harnesses.protocol.common import PtyHarnessProcess\n"
        "original_start = HarnessLauncher.start\n"
        "original_spawn = PtyHarnessProcess.spawn\n"
        "def observed_spawn(*args, **kwargs):\n"
        f" Path({str(root / 'pty-spawned')!r}).touch()\n"
        " return original_spawn(*args, **kwargs)\n"
        "PtyHarnessProcess.spawn = observed_spawn\n"
        "def start(self, spec):\n"
        f" if spec.harness == 'claude' and Path({str(root / 'pty-mode')!r}).exists():\n"
        f"  spec = replace(spec, headless=False, command={(sys.executable, str(fake))!r})\n"
        " return original_start(self, spec)\n"
        "HarnessLauncher.start = start\n"
        "from hyprial.shell.impl.cli.runtime.entry import main\nmain()\n"
    )
    identity = {"actor": f"user:{OWNER}"}
    graphs = []
    def graph(label):
        result = node.ipc("workflow.start", {
            **identity, "yaml": f"version: 2\nname: {label}\non_failure: hold\nnodes:\n"
            f"  - id: work\n    task: synthetic credential check\n    launch: {{harness: claude, cwd: '{work}'}}\n",
        })
        graph_id = result.get("graphId") or result["runId"]
        graphs.append(graph_id)
        return graph_id
    def inspect(graph_id):
        return node.ipc("workflow.node.inspect", {**identity, "runId": graph_id, "target": "work"})
    def actor_of(graph_id):
        result = inspect(graph_id)
        return result["node"]["owner"].rsplit(":", 1)[-1]
    def log_events():
        result = []
        for path in (node.state / "logs").rglob("*.jsonl"):
            result.extend(json.loads(line) for line in path.read_text().splitlines() if line)
        return result
    try:
        node.start_daemon(command=[uv, "run", "--no-project", "--python", sys.executable,
                                   "python", str(bootstrap), "daemon", "run"])
        assert node.ipc("ps", {})["zenoh"]["isolated"] is True
        node.ipc("agent.create", {"name": "custodian"})
        token = secrets.token_hex(24)
        node.ipc("agent.provider.login", {"actor": "custodian", "provider": "claude",
            "setupToken": token, "makeDefault": True})
        default = node.ipc("agent.provider.default", {})
        assert default["credentialSource"] == "setup-token"
        assert default["defaultCredential"]["ownerActor"] == "custodian"
        assert not (node.home / "agents/custodian/secrets").exists()
        good = graph("default-grant")
        actor = actor_of(good)
        grants = wait_for(lambda: [grant for grant in node.ipc("agent.secret.list", {})["grants"] if grant["actor"] == actor])
        assert grants[0]["source"] == "agent-shared"
        assert grants[0]["ownerActor"] == "custodian"
        created = next(item for item in node.ipc("agent.list", {})["agents"] if item["actor"] == actor)
        assert created["capabilities"]["creationOrigin"] == "managed-launch"
        assert not (node.home / "agents" / actor / "secrets.sqlite").exists()
        wait_for(lambda: any(event.get("event") == "worker.ready" for event in log_events()))
        agent_home = node.home / "agents" / actor
        own_native = agent_home / "secrets/native/claude"
        transcript = agent_home / "state/sessions/claude/synthetic/transcript.jsonl"
        assert transcript.is_file()
        assert (own_native / ".synthetic-config").is_file()
        assert (own_native / "projects").resolve() == transcript.parent.parent.resolve()
        node.ipc("agent.provider.default", {"clear": True})
        assert node.ipc("agent.provider.default", {})["provider"] is None
        assert node.ipc("agent.secret.list", {"actor": actor})["grants"] == grants
        missing = graph("no-default")
        failed = wait_for(lambda: (result if (result := inspect(missing))["node"]["state"] == "failed" else None))
        assert "CLAUDE_CREDENTIAL_MISSING" in failed["node"]["reasonRef"]
        assert "PROVIDER_AUTHENTICATION_FAILED" not in failed["node"]["reasonRef"]
        node.ipc("agent.create", {"name": "native-owner"})
        node.ipc("agent.provider.login", {"actor": "native-owner", "provider": "claude",
            "files": [{"filename": ".credentials.json", "contentBase64": base64.b64encode(b'{"synthetic":true}').decode()}],
            "makeDefault": True})
        native_graph = graph("native-default")
        native_actor = actor_of(native_graph)
        wait_for(lambda: any(event.get("event") == "worker.ready" and event.get("name") == native_actor
                             for event in log_events()))
        assert (node.home / "agents" / native_actor / "secrets/native/claude/.credentials.json").is_file()
        assert node.ipc("agent.secret.list", {"actor": native_actor})["grants"] == []
        node.ipc("agent.provider.default", {"clear": True})
        assert not (native / ".credentials.json").exists()
        node.ipc("lifecycle.start", {"provider": "claude", "name": "resident", "headless": True,
                                    "cwd": str(work), "command": [uv, "run", "--no-project", "--python", sys.executable, "python", str(fake)]})
        assert node.ipc("agent.secret.list", {"actor": "resident"})["grants"] == []
        ambient_native = Path(node.environment["HOME"]) / ".claude"
        (root / "pty-mode").touch()
        pty_graph = graph("pty-no-default")
        pty_failed = wait_for(lambda: (result if (result := inspect(pty_graph))["node"]["state"] == "failed" else None))
        assert pty_failed["node"]["reasonRef"] == "pac:launch_failed:CLAUDE_CREDENTIAL_MISSING"
        assert not (root / "pty-spawned").exists()
        assert not (root / "host-bait-accessed").exists()
        assert sorted(p.name for p in ambient_native.iterdir()) == [".credentials.json"]
        assert (ambient_native / ".credentials.json").read_text() == '{"synthetic_state":"host-bait"}'
        events = log_events()
        assert all(token not in json.dumps(event) for event in events)
        sources = {event.get("credentialSource") for event in events if event.get("event") == "worker.claude.credential_source"}
        assert {"env-grant", "missing", "agent-native"} <= sources
        (root / "result.json").write_text(json.dumps({"defaultGrant": "PASS", "missingStart": "PASS",
            "pacReason": failed["node"]["reasonRef"], "residentNativeWithoutFile": "PASS",
            "clearPreservesGrants": "PASS", "nativeDefaultPac": "PASS",
            "workflowPtyNoSpawn": "PASS", "ptyPacReason": pty_failed["node"]["reasonRef"],
            "hostBaitNoAccess": "PASS", "agentNativeConfigAndTranscript": "PASS",
            "realProviderAuth": "NOT_RUN", "realKeychain": "NOT_RUN"}))
    finally:
        if node.daemon is not None:
            try:
                for graph_id in graphs:
                    node.ipc("workflow.cancel", {**identity, "runId": graph_id})
            finally:
                node.stop_daemon()
    assert node.daemon is None
    print(f"Isolated evidence: {root}/result.json")
