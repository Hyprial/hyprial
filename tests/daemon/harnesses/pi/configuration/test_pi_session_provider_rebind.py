"""§8.1: a resumed Pi session must not silently keep a stale provider.

Production evidence (docs/notes/resident-pi-provider-credentials-2026-10-16.md
§8.1): an agent whose configuration was deepseek resumed a session whose file
recorded ``provider=zai-coding-cn`` and walked GLM into a 429.  Pi restores the
model recorded in the session file when the launch names none, so the session's
old vendor wins -- without a word.  The controlled loader is the single
authority at the session-open boundary: it rebinds to the agent's current
configuration and writes one auditable warning.

The tests drive the real installed Pi SDK through the shipped loader, so they
exercise the real restore path rather than a mock of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.pi.loader import PI_CONTROLLED_LOADER
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE

NODE = shutil.which("node")
PI = shutil.which("pi")

FIXTURE = Path(__file__).resolve().parents[5] / "tests/fixtures/agent_home_p20/pi"


def _pi_package_root() -> Path | None:
    if PI is None:
        return None
    candidate = Path(PI).resolve()
    for parent in (candidate, *candidate.parents):
        manifest = parent / "package.json"
        if not manifest.is_file():
            continue
        try:
            body = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if body.get("name") == "@earendil-works/pi-coding-agent":
            return parent
    return None


PI_ROOT = _pi_package_root()

pytestmark = pytest.mark.skipif(
    NODE is None or PI_ROOT is None,
    reason="the real node and installed Pi SDK are required",
)


def _model_definition(model_id: str) -> dict[str, object]:
    return {
        "api": "openai-completions",
        "baseUrl": "http://127.0.0.1:9/v1",
        "models": [
            {
                "id": model_id,
                "name": model_id,
                "reasoning": False,
                "input": ["text"],
                "cost": {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                },
                "contextWindow": 100_000,
                "maxTokens": 1_000,
            }
        ],
    }


def _selection_entries(provider: str, model: str) -> list[dict[str, object]]:
    return [
        {
            "type": "model_change",
            "id": "e1",
            "parentId": None,
            "timestamp": "2026-10-16T00:00:01.000Z",
            "provider": provider,
            "modelId": model,
        },
        {
            "type": "message",
            "id": "e2",
            "parentId": "e1",
            "timestamp": "2026-10-16T00:00:02.000Z",
            "message": {
                "role": "assistant",
                "provider": provider,
                "model": model,
                "content": [{"type": "text", "text": "recorded reply"}],
                "stopReason": "stop",
            },
        },
        {
            "type": "thinking_level_change",
            "id": "e3",
            "parentId": "e2",
            "timestamp": "2026-10-16T00:00:03.000Z",
            "thinkingLevel": "off",
        },
    ]


def _session_state(tmp_path: Path, recorded: tuple[str, str] | None):
    """Run the real loader over a session recording ``recorded``.

    The agent configuration is deepseek; ``recorded`` is what the session file
    claims.  Returns the ``get_state`` row, stderr, and the session file.
    """

    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    repository = fixture / "workspace" / "repo"
    subprocess.run(
        ["git", "init", "-q", str(repository)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cwd = (repository / "nested").resolve()
    (cwd / ".pi").mkdir(exist_ok=True)
    shutil.copy2(repository / ".pi" / "settings.json", cwd / ".pi" / "settings.json")

    source = fixture / "native-root"
    projection_root = fixture / "projection-root"
    shutil.copytree(source, projection_root)
    (projection_root / "auth.json").unlink()
    native_root = fixture / "native-auth-root"
    native_root.mkdir()
    (native_root / "auth.json").write_text(
        json.dumps(
            {
                "deepseek": {"type": "api_key", "key": "sk-fake-deepseek"},
                "zai-coding-cn": {"type": "api_key", "key": "sk-fake-zai"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (projection_root / "settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": "deepseek",
                "defaultModel": "deepseek-flash",
                "defaultProjectTrust": "ask",
                "enableInstallTelemetry": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    isolated_home = (tmp_path / "home").resolve()
    isolated_home.mkdir()
    session_root = (tmp_path / "sessions").resolve()
    session_root.mkdir()
    session_id = "rebind-session"
    session_file = session_root / f"2026-10-16T00-00-00-000Z_{session_id}.jsonl"
    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-10-16T00:00:00.000Z",
        "cwd": str(cwd),
    }
    entries: list[dict[str, object]] = [header]
    if recorded is not None:
        entries.extend(_selection_entries(*recorded))
    session_file.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )

    payload = {
        "piPackageRoot": str(PI_ROOT),
        "cwd": str(cwd),
        "repositoryRoot": str(repository.resolve()),
        "projectionRoot": str(projection_root.resolve()),
        "nativeRoot": str(native_root.resolve()),
        "homeRoot": str(isolated_home),
        "projectTrusted": True,
        "sessionRoot": str(session_root),
        "sessionId": session_id,
        "receiptPath": str((session_root / "receipts" / "rpc.json").resolve()),
        "modelDefinitions": {
            "deepseek": _model_definition("deepseek-flash"),
            "zai-coding-cn": _model_definition("glm-4.6"),
        },
    }
    completed = subprocess.run(
        [
            NODE or "node",
            str(PI_CONTROLLED_LOADER),
            "run-rpc",
            json.dumps(payload, separators=(",", ":")),
        ],
        input='{"id":"state","type":"get_state"}\n',
        check=False,
        cwd=cwd,
        env={
            "HOME": str(isolated_home),
            "PATH": os.environ.get("PATH", ""),
            "PI_CODING_AGENT_DIR": str(native_root.resolve()),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    state = next((row for row in rows if row.get("id") == "state"), None)
    return state, completed.stderr, session_file


def _rebind_warnings(stderr: str) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    for line in stderr.splitlines():
        prefix = "PI_CONTROLLED_LOADER_WARNING "
        if line.startswith(prefix):
            warnings.append(json.loads(line[len(prefix):]))
    return warnings


def test_a_stale_session_provider_is_rebound_to_the_agent_configuration(
    tmp_path: Path,
) -> None:
    """The session says zai; the agent's configuration is deepseek.

    Reusing zai would walk another vendor's quota silently, so the loader must
    rebind to deepseek and say so once, with no credential in the log.
    """

    state, stderr, session_file = _session_state(
        tmp_path, ("zai-coding-cn", "glm-4.6")
    )

    assert state is not None and state["success"] is True, stderr
    assert state["data"]["model"]["provider"] == "deepseek"
    assert state["data"]["model"]["id"] == "deepseek-flash"

    warnings = _rebind_warnings(stderr)
    assert len(warnings) == 1, stderr
    warning = warnings[0]
    assert warning["event"] == "session.provider.rebound"
    assert warning["sessionId"] == "rebind-session"
    assert warning["recordedProvider"] == "zai-coding-cn"
    assert warning["recordedModel"] == "glm-4.6"
    assert warning["configuredProvider"] == "deepseek"
    assert warning["configuredModel"] == "deepseek-flash"
    assert "sk-fake" not in stderr

    # Durable: the next resume must read deepseek, not the stale vendor.
    selections = [
        json.loads(line)
        for line in session_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        entry.get("type") == "model_change"
        and entry.get("provider") == "deepseek"
        and entry.get("modelId") == "deepseek-flash"
        for entry in selections
    )


def test_a_live_worker_logs_the_rebind_warning_while_it_runs(tmp_path: Path) -> None:
    """R2: the operator sees the rebind while the headless worker is alive.

    Acceptance scenario (2026-10-16): the agent configuration is deepseek, the
    resumed session records zai-coding-cn/glm-4.6, and the launch names no
    provider.  The loader warned on stderr, but PiRpcClient only buffered that
    into ``stderrTail`` until exit, so the live log was silent.  The warning
    must reach the shared worker log immediately, with no credential in it.
    """

    from tests.daemon.harnesses.pi.configuration.test_pi_provider_credentials import (  # noqa: E501
        _complete_launch,
        _deepseek_spec,
        _grant_deepseek,
        _p2_pi_context,
    )
    from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess
    from hyprial.identity.impl.agents.runtime import (
        resolve_agent_runtime_context,
    )

    registry, hyprial_home, _repository, cwd, _stale = _p2_pi_context(tmp_path)
    settings = tmp_path / "personality" / "native" / "pi" / "settings.json"
    settings.write_text(
        json.dumps(
            {"defaultProvider": "deepseek", "defaultModel": "deepseek-flash"}
        )
        + "\n",
        encoding="utf-8",
    )
    context = resolve_agent_runtime_context(
        registry=registry,
        agent_name="worker",
        harness="pi",
        cwd=str(cwd),
        tool_profile=TEST_TOOL_PROFILE,
    )
    assert context is not None
    _grant_deepseek(registry, hyprial_home)
    channel, complete = _complete_launch(registry, hyprial_home, context, tmp_path)
    session_file = (
        context.roots.session_root
        / "2026-10-16T00-00-00-000Z_live-rebind.jsonl"
    )
    header = {
        "type": "session",
        "version": 3,
        "id": "live-rebind",
        "timestamp": "2026-10-16T00:00:00.000Z",
        "cwd": str(cwd),
    }
    session_file.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [header, *_selection_entries("zai-coding-cn", "glm-4.6")]
        )
        + "\n",
        encoding="utf-8",
    )
    spec = replace(
        _deepseek_spec(cwd),
        model_provider=None,
        model=None,
        session_ref="live-rebind",
    )
    log = channel.state_dir / "logs" / "workers" / "pi-worker.jsonl"

    async def scenario() -> None:
        carrier = PiRpcProcess(spec, worker_channel=channel, complete_launch=complete)
        try:
            assert await asyncio.to_thread(
                carrier.wait_ready, timeout=60
            ), carrier.last_error
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if log.exists() and "session.provider.rebound" in log.read_text(
                    encoding="utf-8"
                ):
                    break
                await asyncio.sleep(0.05)
            # The assertion is only meaningful while the worker still runs: a
            # post-exit stderrTail dump must not be able to satisfy it.
            assert carrier.running is True
            body = log.read_text(encoding="utf-8")
            assert "session.provider.rebound" in body
            assert "sk-fake" not in body and "sk-resident" not in body
        finally:
            await asyncio.to_thread(carrier.stop)

    try:
        asyncio.run(scenario())
    finally:
        registry.close()


def test_the_stderr_tail_forwards_a_warning_split_across_chunks() -> None:
    """A 4 KiB read boundary must not drop a live rebind warning."""

    from hyprial.daemon.impl.harnesses.pi.stderr_warnings import PiStderrTail

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []

        def log(self, level: str, event: str, **fields: object) -> None:
            self.calls.append((level, event, fields))

    recorder = Recorder()
    tail = PiStderrTail(recorder, limit=1024)
    line = (
        b'PI_CONTROLLED_LOADER_WARNING {"event":"session.provider.rebound",'
        b'"sessionId":"s","recordedProvider":"zai-coding-cn",'
        b'"recordedModel":"glm-4.6","configuredProvider":"deepseek",'
        b'"configuredModel":"deepseek-flash"}\n'
    )
    for index in range(0, len(line), 7):
        tail.extend(line[index : index + 7])
    assert recorder.calls == [
        (
            "warn",
            "session.provider.rebound",
            {
                "sessionId": "s",
                "recordedProvider": "zai-coding-cn",
                "recordedModel": "glm-4.6",
                "configuredProvider": "deepseek",
                "configuredModel": "deepseek-flash",
            },
        )
    ]
    # Unrelated stderr still only fills the post-mortem tail.
    tail.extend(b"pi: unrelated diagnostic\n")
    assert len(recorder.calls) == 1
    assert b"unrelated diagnostic" in bytes(tail)


def test_a_matching_session_provider_is_left_alone(tmp_path: Path) -> None:
    """No drift, no rewrite: the guard must not fire on every resume."""

    state, stderr, session_file = _session_state(
        tmp_path, ("deepseek", "deepseek-flash")
    )

    assert state is not None and state["success"] is True, stderr
    assert state["data"]["model"]["provider"] == "deepseek"
    assert _rebind_warnings(stderr) == []

    selections = [
        json.loads(line)
        for line in session_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert (
        sum(1 for entry in selections if entry.get("type") == "model_change") == 1
    )
