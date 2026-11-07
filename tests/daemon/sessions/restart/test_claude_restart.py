"""Native Claude sessions survive a restart before the next reconcile tick.

The model protocol is the repository's JSONL double. The daemon, lifecycle
transactions, registry, worker wrapper and restart are real. Suppressing the
periodic ref sync freezes the crash window after ready and before that tick;
startup must commit its established ref without depending on a later timer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import (
    IsolatedHarness,
)
from tests.support.runtime._tmp import hyprial_test_tmp_root
import hyprial.daemon.impl.harnesses.agent_sdk as agent_sdk


ROOT = Path(__file__).resolve().parents[4]


def _wait_ready(home: Path, count: int = 1) -> dict[str, object]:
    log = home / "state/logs/workers/claude-restart-worker.jsonl"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if log.exists():
            records = [json.loads(line) for line in log.read_text().splitlines()]
            ready = [row for row in records if row["event"] == "worker.ready"]
            if len(ready) >= count:
                return ready[-1]
        time.sleep(0.02)
    raise AssertionError(f"no worker.ready in {log}")


@pytest.fixture
def restart_home():
    with tempfile.TemporaryDirectory(prefix="cc-resume-", dir=hyprial_test_tmp_root()) as root:
        yield Path(root)


def test_restart_before_periodic_sync_resumes_established_native_ref(
    restart_home: Path,
) -> None:
    tmp_path = restart_home
    harness = IsolatedHarness(tmp_path)
    harness.write_owner("owner-a")
    os_home = tmp_path / "os-home"
    os_home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    capture = tmp_path / "options.jsonl"
    bootstrap = tmp_path / "worker.py"
    sdk_site = os.environ.get("HYPRIAL_TEST_CLAUDE_SDK_SITE")
    setup = ""
    if sdk_site:
        cli = tmp_path / "claude-test"
        cli.write_text(
            f"#!{sys.executable}\nimport runpy\n"
            f"runpy.run_path({str(Path(__file__).with_name('fake_claude_cli.py'))!r}, run_name='__main__')\n"
        )
        cli.chmod(0o700)
        setup = (
            f"sys.path.insert(0, {sdk_site!r})\n"
            f"os.environ['HYPRIAL_CLAUDE_EXECUTABLE'] = {str(cli)!r}\n"
            f"os.environ['HYPRIAL_TEST_CLAUDE_ARGV_LOG'] = {str(tmp_path / 'argv.jsonl')!r}\n"
            f"os.environ['HYPRIAL_TEST_CLAUDE_SESSIONS'] = {str(tmp_path / 'native-sessions')!r}\n"
        )
    target = (
        Path(agent_sdk.__file__).with_name("_agent_sdk_worker.py")
        if sdk_site else ROOT / "tests/fixtures/harnesses/fake_claude_sdk.py"
    )
    bootstrap.write_text(
        "import json, os, runpy, sys\n"
        f"with open({str(capture)!r}, 'a') as stream:\n"
        "    stream.write(os.environ['HYPRIAL_AGENT_SDK_OPTIONS'] + '\\n')\n"
        + setup
        + f"runpy.run_path({str(target)!r}, run_name='__main__')\n"
    )
    harness.environment.update(
        HOME=str(os_home),
        HYPRIAL_BUNDLED_SDK_BOOTSTRAP=str(bootstrap),
        UV_CACHE_DIR=str(tmp_path / "uv-cache"),
    )
    command = [
        sys.executable,
        "-c",
        "from hyprial.daemon.impl.runtime.reconcile import _BridgeReconcileMixin; "
        "_BridgeReconcileMixin._sync_harness_session_refs = lambda self: None; "
        "from hyprial.cli import main; main()",
        "daemon",
        "run",
    ]
    try:
        harness.start_daemon(command=command)
        harness.ipc("agent.create", {"name": "restart-worker", "cwd": str(work)})
        credential = harness.home / "agents/restart-worker/secrets/native/claude/.credentials.json"
        credential.parent.mkdir(parents=True, exist_ok=True)
        for directory in (credential.parent, credential.parent.parent, credential.parent.parent.parent):
            directory.chmod(0o700)
        credential.write_text("TEST_CLAUDE_CREDENTIAL_NOT_A_SECRET\n")
        credential.chmod(0o600)
        harness.ipc(
            "lifecycle.start",
            {
                "provider": "claude",
                "name": "restart-worker",
                "headless": True,
                "cwd": str(work),
            },
        )
        deadline = time.monotonic() + 10
        while not capture.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        first = json.loads(capture.read_text().splitlines()[0])
        old_ref = first["sessionId"]
        assert "resume" not in first
        first_ready = _wait_ready(tmp_path)
        snapshot = harness.ipc("agent.get", {"name": "restart-worker"})["agent"]
        print(f"AGENT_SESSION last_session_id={snapshot.get('lastSessionId')}")
        harness.stop_daemon()
        harness.start_daemon(command=command)
        deadline = time.monotonic() + 10
        while len(capture.read_text().splitlines()) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        second = json.loads(capture.read_text().splitlines()[1])
        ready = _wait_ready(tmp_path, count=2)
        print(f"RESTART old_ref={old_ref} worker_options={json.dumps(second, sort_keys=True)}")
        if sdk_site:
            argv = [json.loads(line) for line in (tmp_path / "argv.jsonl").read_text().splitlines()]
            print(f"SDK_ARGV old_ref={old_ref} argv={json.dumps(argv[-1])}")
            assert f"--resume={old_ref}" in argv[-1]
        assert second.get("resume") == old_ref, second
        assert "sessionId" not in second
        assert snapshot["lastSessionId"] == old_ref
        assert first_ready["last_session_id"] is None
        assert first_ready["spec.session_ref"] is None
        assert first_ready["session_decision"] == "fresh-mint"
        print(f"READY_FRESH {json.dumps(first_ready, sort_keys=True)}")
        print(f"READY_RESUME {json.dumps(ready, sort_keys=True)}")
        assert "last_session_id" in ready
        assert ready["last_session_id"] == old_ref
        assert ready["spec.session_ref"] == old_ref
        assert ready["session_decision"] == "resume"
    finally:
        if harness.daemon is not None:
            harness.stop_daemon()


@pytest.mark.parametrize("p2", [True, False])
def test_missing_resume_target_exits_nonzero_with_target_id(tmp_path: Path, p2: bool) -> None:
    missing = "missing-native-session-1729"
    package = tmp_path / "claude_agent_sdk"
    package.mkdir()
    (package / "__init__.py").write_text(
        "class ClaudeAgentOptions:\n"
        "    def __init__(self, **kwargs): self.kwargs = kwargs\n"
        "class ResultMessage: pass\n"
        "class ClaudeSDKClient:\n"
        "    def __init__(self, options): self.options = options\n"
        "    async def __aenter__(self):\n"
        "        if self.options.kwargs.get('resume'):\n"
        "            raise ValueError('No conversation found')\n"
        "        return self\n"
        "    async def __aexit__(self, *args): return False\n"
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(tmp_path),
        "HYPRIAL_AGENT_SDK_OPTIONS": json.dumps({"resume": missing}),
        **({"HYPRIAL_AGENT_SDK_RUNTIME_CONTEXT_MODE": "agent-home-p2"} if p2 else {}),
    }
    worker = Path(agent_sdk.__file__).with_name("_agent_sdk_worker.py")
    command = [sys.executable, str(worker)]
    sdk_site = os.environ.get("HYPRIAL_TEST_CLAUDE_SDK_SITE")
    if sdk_site:
        cli = tmp_path / "claude-test"
        cli.write_text(
            f"#!{sys.executable}\nimport runpy\n"
            f"runpy.run_path({str(Path(__file__).with_name('fake_claude_cli.py'))!r}, run_name='__main__')\n"
        )
        cli.chmod(0o700)
        environment.update(
            HYPRIAL_CLAUDE_EXECUTABLE=str(cli),
            HYPRIAL_TEST_CLAUDE_ARGV_LOG=str(tmp_path / "argv.jsonl"),
            HYPRIAL_TEST_CLAUDE_SESSIONS=str(tmp_path / "native-sessions"),
        )
        command = [
            sys.executable, "-I", "-S", "-c",
            f"import runpy, sys; sys.path.insert(0, {sdk_site!r}); "
            f"runpy.run_path({str(worker)!r}, run_name='__main__')",
        ]
    result = subprocess.run(
        command, env=environment, input="", capture_output=True,
        text=True, timeout=10, check=False,
    )
    print(f"NEGATIVE_CONTROL p2={p2} rc={result.returncode} stderr={result.stderr.strip()}")
    assert result.returncode != 0, result.stdout
    assert missing in result.stderr
    assert '"type":"ready"' not in result.stdout.replace(" ", "")
    if sdk_site:
        argv = [json.loads(line) for line in (tmp_path / "argv.jsonl").read_text().splitlines()]
        assert len(argv) == 1
        assert f"--resume={missing}" in argv[0]
