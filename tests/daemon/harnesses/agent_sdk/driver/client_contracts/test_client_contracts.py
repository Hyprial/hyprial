from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
import pytest
import hyprial.daemon.impl.harnesses.agent_sdk as agent_sdk_module
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient
from hyprial.kernel.impl.log import Logger

from ..support import (
    _configured_claude_launch,
)


@pytest.mark.anyio
async def test_isolated_worker_jsonl_proxy_delivers_a_terminal_result() -> None:
    code = """
import json, sys
print('{"type":"ready"}', flush=True)
for line in sys.stdin:
    command = json.loads(line)
    if command['op'] == 'query':
        print(json.dumps({'type': 'result', 'result': command['prompt'] + ' done', 'isError': False}), flush=True)
    elif command['op'] == 'stop':
        break
"""
    spec = HarnessLaunchSpec(harness="claude", name="claude-worker", headless=True)
    client = IsolatedAgentSdkClient(spec, worker_command=(sys.executable, "-c", code))

    async with client:
        await client.query("work")
        messages = [message async for message in client.receive_response()]

    assert len(messages) == 1
    assert messages[0].result == "work done"
    assert not messages[0].is_error


@pytest.mark.anyio
async def test_isolated_worker_jsonl_proxy_forwards_progress_before_result() -> None:
    code = """
import json, sys
print('{"type":"ready"}', flush=True)
for line in sys.stdin:
    command = json.loads(line)
    if command['op'] == 'query':
        print(json.dumps({'type': 'progress', 'phase': 'tool-call', 'summary': 'calling read', 'toolCallId': 'call-1', 'toolName': 'read'}), flush=True)
        print(json.dumps({'type': 'progress', 'phase': 'thinking', 'summary': 'thinking'}), flush=True)
        print(json.dumps({'type': 'progress', 'phase': 'turn-end', 'summary': 'Claude turn completed', 'terminal': True}), flush=True)
        print(json.dumps({'type': 'result', 'result': command['prompt'] + ' done', 'isError': False}), flush=True)
    elif command['op'] == 'stop':
        break
"""
    spec = HarnessLaunchSpec(harness="claude", name="claude-worker", headless=True)
    client = IsolatedAgentSdkClient(spec, worker_command=(sys.executable, "-c", code))

    async with client:
        await client.query("work")
        messages = [message async for message in client.receive_response()]

    assert [type(message).__name__ for message in messages] == [
        "ProgressObservation",
        "ProgressObservation",
        "ProgressObservation",
        "_WireResult",
    ]
    assert messages[0].phase == "tool-call"
    assert messages[0].tool_call_id == "call-1"
    assert messages[0].tool_name == "read"
    assert messages[1].phase == "thinking"
    assert messages[2].phase == "turn-end"
    assert messages[2].terminal is True
    assert messages[3].result == "work done"


@pytest.mark.anyio
async def test_claude_steer_is_an_honest_advance_not_an_injection(
    tmp_path: Path,
) -> None:
    """Claude has no non-destructive steer; steer must interrupt and say so.

    The Agent SDK worker exposes only ``interrupt`` on a live turn, so the
    timeout path's honest mechanism ends the running turn and lets the queued
    delivery start next.  The message is never injected (disposition
    ``advanced``), which is what the runtime uses to keep it queued.
    """

    log = tmp_path / "ops.log"
    code = """
import json, sys
log = sys.argv[1]
print('{"type":"ready"}', flush=True)
for line in sys.stdin:
    command = json.loads(line)
    with open(log, 'a', encoding='utf-8') as stream:
        stream.write(command['op'] + '\\n')
    if command['op'] == 'stop':
        break
"""
    spec = HarnessLaunchSpec(harness="claude", name="claude-worker", headless=True)
    client = IsolatedAgentSdkClient(
        spec, worker_command=(sys.executable, "-c", code, str(log))
    )

    async with client:
        assert await client.steer("this text must not be injected") == "advanced"

    ops = log.read_text(encoding="utf-8").split()
    assert ops[0] == "interrupt"
    assert ops.count("interrupt") == 1  # one advance, never a query


@pytest.mark.anyio
async def test_claude_fake_driver_writes_redacted_lifecycle_log(tmp_path: Path) -> None:
    code = """
import sys, time
print('{"type":"ready"}', flush=True)
sys.stderr.write('token claude-lifecycle-secret\\n')
sys.stderr.flush()
time.sleep(0.05)
raise SystemExit(23)
"""
    launch = HarnessLaunchSpec(
        harness="claude", name="claude-worker", headless=True
    )
    logger = Logger.worker(
        tmp_path / "state", runtime="claude", name=launch.name
    )
    legacy_log = tmp_path / "state" / "claude-claude-worker.log"
    legacy_log.parent.mkdir(parents=True)
    legacy_log.write_bytes(b"historical-claude-log\n")
    client = IsolatedAgentSdkClient(
        launch,
        worker_command=(sys.executable, "-c", code),
        logger=logger,
    )

    await client.__aenter__()
    assert client._exit_task is not None
    await asyncio.wait_for(client._exit_task, timeout=2.0)
    await client.__aexit__(None, None, None)

    raw = logger.path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    assert [record["event"] for record in records] == [
        "worker.readiness.waiting",
        "worker.started",
        "worker.readiness.waiting",
        "worker.ready",
        "worker.exited",
    ]
    assert all(record["component"] == "claude-worker" for record in records)
    assert all(record["name"] == "claude-worker" for record in records)
    assert records[-1]["returnCode"] == 23
    assert records[-1]["pid"] > 0
    assert "claude-lifecycle-secret" not in raw
    assert "[REDACTED]" in records[-1]["stderrTail"]
    assert legacy_log.read_bytes() == b"historical-claude-log\n"


@pytest.mark.anyio
async def test_packaged_worker_does_not_turn_failed_resume_into_fresh_session(
    tmp_path: Path,
) -> None:
    fake_package = tmp_path / "claude_agent_sdk"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text(
        """
class ClaudeAgentOptions:
    def __init__(self, **kwargs): self.kwargs = kwargs

class ResultMessage: pass

class ClaudeSDKClient:
    def __init__(self, options): self.options = options
    async def __aenter__(self):
        if self.options.kwargs.get('resume'):
            raise ValueError('No conversation found with session ID')
        return self
    async def __aexit__(self, *args): return False
""",
        encoding="utf-8",
    )
    worker = Path(agent_sdk_module.__file__).with_name("_agent_sdk_worker.py")
    worker_bootstrap = (
        "import runpy, sys; "
        f"sys.path.insert(0, {str(tmp_path)!r}); "
        f"runpy.run_path({str(worker)!r}, run_name='__main__')"
    )
    registry, context, channel, launch = _configured_claude_launch(tmp_path)
    try:
        client = IsolatedAgentSdkClient(
            HarnessLaunchSpec(
                harness="claude",
                name="claude-worker",
                headless=True,
                cwd=str(tmp_path / "work"),
            ),
            worker_channel=channel,
            worker_command=(sys.executable, "-c", worker_bootstrap),
            complete_launch=launch,
            resume="missing-session",
            startup_timeout_seconds=2,
        )

        assert client._runtime_context is context
        assert (
            client._env["HYPRIAL_AGENT_SDK_RUNTIME_CONTEXT_MODE"]
            == "agent-home-p2"
        )
        with pytest.raises(ConnectionError, match="exited before replying"):
            async with client:
                pytest.fail("a failed P2 resume must not emit a fresh-session ready")
        stderr = bytes(client._stderr_tail).decode(errors="replace")
        assert "No conversation found with session ID" in stderr
        assert "starting a fresh session" not in stderr
    finally:
        registry.close()


@pytest.mark.anyio
async def test_packaged_worker_maps_claude_content_blocks_to_progress(
    tmp_path: Path,
) -> None:
    fake_package = tmp_path / "claude_agent_sdk"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text(
        """
class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

class TextBlock:
    def __init__(self, text): self.text = text
class ThinkingBlock:
    def __init__(self, thinking): self.thinking = thinking
class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id; self.name = name; self.input = input
class ToolResultBlock:
    def __init__(self, tool_use_id, is_error=False):
        self.tool_use_id = tool_use_id; self.is_error = is_error
class StreamEvent:
    pass
class AssistantMessage:
    def __init__(self, content): self.content = content
class ResultMessage:
    def __init__(self, result=None, is_error=False):
        self.result = result; self.is_error = is_error

class ClaudeSDKClient:
    def __init__(self, options): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def query(self, prompt): self.prompt = prompt
    async def receive_response(self):
        yield AssistantMessage([
            TextBlock('I am reading the file.'),
            ThinkingBlock('private reasoning stays local'),
            ToolUseBlock('call-1', 'read', {'path': 'README.md'}),
            ToolResultBlock('call-1', is_error=False),
        ])
        yield StreamEvent()
        yield ResultMessage(self.prompt + ' done')
    async def interrupt(self): pass
""",
        encoding="utf-8",
    )
    worker = Path(agent_sdk_module.__file__).with_name("_agent_sdk_worker.py")
    spec = HarnessLaunchSpec(harness="claude", name="claude-worker", headless=True)
    client = IsolatedAgentSdkClient(
        spec,
        worker_command=(sys.executable, str(worker)),
        env={"PYTHONPATH": str(tmp_path)},
    )

    async with client:
        await client.query("work")
        messages = [message async for message in client.receive_response()]

    assert [type(message).__name__ for message in messages] == [
        "ProgressObservation",
        "ProgressObservation",
        "ProgressObservation",
        "ProgressObservation",
        "ProgressObservation",
        "_WireResult",
    ]
    assert [message.phase for message in messages[:-1]] == [
        "message-segment",
        "thinking",
        "tool-call",
        "tool-result",
        "turn-end",
    ]
    assert messages[0].summary == "I am reading the file."
    # ThinkingBlock content is deliberately not forwarded.
    assert messages[1].summary == "thinking"
    assert messages[2].tool_call_id == "call-1"
    assert messages[2].detail == {"inputKeys": ["path"]}
    assert messages[3].tool_call_id == "call-1"
    assert messages[4].summary == "Claude turn completed"
    assert messages[4].terminal is True
    assert messages[5].result == "work done"
