"""Opt-in real guest Codex startup and MCP relay; no authenticated model turn."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
import re
import shlex
from pathlib import Path
import subprocess

import pytest

from hyprial.identity.impl.agents.home.config import AgentConfig
from hyprial.identity.impl.agents.home.environment import ChildEnvironmentLaunch, CompleteChildEnvironment
from hyprial.identity.impl.agents.runtime.context import (
    resolve_agent_runtime_context,
)
from hyprial.kernel import SmolvmRuntimeSpec
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient
from hyprial.daemon.impl.harnesses.codex.native_env import CodexAgentHomeError
from hyprial.daemon.impl.harnesses.smolvm_runtime import SmolvmWorkerRuntime, mount_roots
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from tests.daemon.workers.test_worker_relay import ACTOR, isolated  # noqa: F401
from tests.fixtures.smolvm.smolvm_daemon import (
    prepare_payload,
    health_snapshot,
    production_identity,
    production_unchanged,
)
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


def test_real_persisted_smolvm_runtime(isolated, monkeypatch):  # noqa: F811
    if not all(
        os.environ.get(k)
        for k in ("AT08_RUNTIME_SPEC", "AT08_RUNTIME_OUTPUT", "AT08_HEALTH_SOCKET")
    ):
        pytest.skip(
            "Real smolvm Codex NOT_RUN: explicit materials, monitor window and machine lock required"
        )
    output = Path(os.environ["AT08_RUNTIME_OUTPUT"]).resolve()
    output.mkdir(mode=0o700)
    root, app, _, _ = isolated
    runtime_spec = SmolvmRuntimeSpec.from_json(
        json.loads(Path(os.environ["AT08_RUNTIME_SPEC"]).read_text())
    )
    source, workspace = prepare_payload(output)
    app._agent_registry.update(
        "alice", config=AgentConfig(str(source)), cwd=str(workspace)
    )
    app._agent_registry.ensure_home("alice")
    context = resolve_agent_runtime_context(
        registry=app._agent_registry,
        agent_name="alice",
        harness="codex",
        cwd=str(workspace),
        tool_profile=TEST_TOOL_PROFILE,
    )
    assert context is not None
    channel = build_worker_channel(
        actor=ACTOR,
        session_ref="alice-session",
        hyprial_home=app.hyprial_home,
        state_dir=root,
        runtime_context=context,
    )
    environment = {
        **context.environment(),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        # Noncredential sentinel, restricted to a guest loopback endpoint. No turn is sent.
        "DEEPSEEK_API_KEY": "SYNTHETIC-NOT-A-CREDENTIAL",
        "HYPRIAL_OPENAI_BASE_URL": "http://127.0.0.1:1/",
    }
    launch = ChildEnvironmentLaunch(
        CompleteChildEnvironment(tuple(sorted(environment.items()))),
        actor=ACTOR,
        runtime_context=context,
    )
    spec = HarnessLaunchSpec(
        "codex",
        "alice",
        True,
        cwd=str(workspace),
        execution_runtime=runtime_spec,
        model_provider="deepseek",
        model="deepseek-flash",
    )
    app.desired_state.upsert_harness(spec)
    assert app.desired_state.load().schema_version == 3
    spec = next(s for s in app.desired_state.load().harnesses if s.name == "alice")
    runtime = SmolvmWorkerRuntime(spec.execution_runtime, channel, app.epoch, spec.cwd)
    # Capture launch inputs before the guest creates its own transient links.
    # The runtime repeats this validation at launch; probing mounted parents
    # later must not reclassify live guest files as incoming host payload.
    selected_mounts = mount_roots(channel, spec.cwd)
    invalid_ref = os.environ.get("AT08_INVALID_SESSION_REF")
    report = dict(
        kind="real-guest-codex-startup",
        model_turn="NOT_RUN",
        strict_resume="NOT_RUN",
        cross_machine="NOT_RUN",
        actor=ACTOR,
        fixture=str(root),
        runtime_output=str(runtime.output),
        ok=False,
        invalid_ref=invalid_ref,
        protocol=[],
    )

    def health(label):
        return health_snapshot(Path(os.environ["AT08_HEALTH_SOCKET"]), output, label)

    before = health("production-before")
    original_run = subprocess.run
    count = 0
    manager_pids = []

    def record_admin(argv, **kw):
        nonlocal count
        result = original_run(argv, **kw)
        if argv[0] == runtime_spec.smolvm_path:
            count += 1
            manager_pids.extend(
                int(pid)
                for pid in re.findall(rb"running \(PID: (\d+)\)", result.stdout)
            )
            (output / f"admin-{count:02}.json").write_text(
                json.dumps(
                    {
                        "argv": list(argv),
                        "exit": result.returncode,
                        "stdout": result.stdout.decode(),
                        "stderr": result.stderr.decode(),
                    },
                    indent=2,
                )
            )
        return result

    monkeypatch.setattr(subprocess, "run", record_admin)
    original_request = CodexAppServerClient.request

    async def record_request(client, method, params=None, **kwargs):
        # Observe the real protocol call, without replacing transport or responses.
        # Never record config/environment or prompts, which may contain secrets.
        event = {"method": method}
        if isinstance(params, dict) and "threadId" in params:
            event["threadId"] = params["threadId"]
        report["protocol"].append(event)
        try:
            result = await original_request(client, method, params, **kwargs)
        except Exception as error:
            event["errorType"] = type(error).__name__
            raise
        event["ok"] = True
        if isinstance(result, dict) and isinstance(result.get("thread"), dict):
            event["returnedThreadId"] = result["thread"].get("id")
        return result

    monkeypatch.setattr(CodexAppServerClient, "request", record_request)

    async def exercise():
        client = CodexAppServerClient(
            spec,
            worker_channel=channel,
            complete_launch=launch,
            command=runtime_spec.guest_command,
            execution_runtime=runtime,
            session_ref=invalid_ref,
        )
        if invalid_ref:
            with pytest.raises(CodexAgentHomeError):
                async with client:
                    pytest.fail("A missing native session unexpectedly resumed")
            assert any(e["method"] == "thread/resume" for e in report["protocol"])
            assert not any(e["method"] == "thread/start" for e in report["protocol"])
            assert not any(e.get("returnedThreadId") for e in report["protocol"])
            report["ok"] = True
            return
        async with client:
            report["native_load"] = asdict(client.native_load_evidence)
            report["thread_id"] = client.session_ref
            # Actual guest CLI/MCP uses the production relay. It cannot reach the daemon directly.
            guest_script = """
import asyncio,json,os,subprocess,sys
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
 env=dict(os.environ,HYPRIAL_HOME='/run/hyprial-worker',HARNESS_STATE_DIR='/run/hyprial-worker')
 params=StdioServerParameters(command='/usr/bin/python3',args=['-m','hyprial.daemon.impl.mcp.entry','agent-channel','--actor','agent:test:isolated:alice','--session-ref','alice-session'],env=env)
 async with stdio_client(params) as streams:
  async with ClientSession(*streams) as session:
   await session.initialize()
   result=await session.call_tool('harness_whoami',{})
   print(json.dumps(result.model_dump(mode='json')),flush=True)
asyncio.run(main())
"""
            raw = await asyncio.to_thread(
                runtime._admin,
                "machine",
                "exec",
                "--name",
                "worker",
                "--timeout",
                "20s",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--",
                runtime_spec.guest_python,
                "-c",
                guest_script,
                timeout=25,
            )
            report["mcp_whoami"] = json.loads(raw)
            identity = report["mcp_whoami"]["structured_content"]
            assert (
                identity["actor"] == ACTOR
                and identity["sessionRef"] == channel.session_ref
            )
            assert (
                identity["daemonEpoch"] == app.epoch and identity["sessionRegistered"]
            )
            assert report["mcp_whoami"]["is_error"] is False
            raw = await asyncio.to_thread(
                runtime._admin,
                "machine",
                "exec",
                "--name",
                "worker",
                "--timeout",
                "10s",
                "--",
                "/bin/sh",
                "-c",
                _mount_probe(Path.home(), selected_mounts),
                timeout=15,
            )
            (output / "guest-mounts.txt").write_bytes(raw)
            # All installed APKs must verify against keys in the pinned upstream rootfs.
            await asyncio.to_thread(
                runtime._admin,
                "machine",
                "exec",
                "--name",
                "worker",
                "--timeout",
                "10s",
                "--",
                "/bin/sh",
                "-c",
                "apk verify /opt/verify-apks/*.apk",
                timeout=15,
            )
            report["apk_signatures"] = "PASS"
            report["ok"] = True

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()
        report["cleanup_complete"] = runtime.cleanup_complete
        report["owned_manager_pids"] = manager_pids
        report["live_managers_after_close"] = []
        for pid in manager_pids:
            try:
                os.kill(pid, 0)
                report["live_managers_after_close"].append(pid)
            except ProcessLookupError:
                pass
        after = health("production-after")
        report["productionIdentity"] = production_identity(before, after)
        report["production_unchanged"] = production_unchanged(before, after)
        (output / "report.json").write_text(json.dumps(report, indent=2))
    assert report["production_unchanged"] and report["cleanup_complete"]
    assert manager_pids and not report["live_managers_after_close"]


def _mount_probe(host_home: Path, mounts: tuple[Path, ...]) -> str:
    """List only explicit mount parent chains, even when /Users does not exist."""
    parents = sorted({p for mount in mounts for p in mount.parents if p != Path("/")})
    checks = [
        f"test ! -e {shlex.quote(str(host_home / name))}"
        for name in (".codex", ".hyprial")
    ]
    listing = "ls -ld -- " + " ".join(shlex.quote(str(p)) for p in parents)
    return "; ".join(
        ["set -eu", *checks, "cat /proc/mounts", listing, "cat /proc/self/status"]
    )
