from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.home.environment import (
    ChildEnvironmentLaunch,
    CompleteChildEnvironment,
    compose_worker_child_launch,
)
from hyprial.identity.impl.agents.runtime.context import (
    AgentRuntimeContext,
    resolve_agent_runtime_context,
)
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.agent_sdk import (
    ClaudeAgentSdkProcess,
)
from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel, build_worker_channel
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


@dataclass(frozen=True)
class FakeResultMessage:
    result: str
    is_error: bool = False


class BlockingResponse:
    def __init__(self, output: str) -> None:
        self.output = output
        self.started = threading.Event()
        self.interrupted = threading.Event()


class FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.queries: list[str] = []
        self.entered = 0
        self.exited = 0
        self.interrupts = 0
        self._active: BlockingResponse | None = None

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        self.entered += 1
        return self

    async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
        self.exited += 1
        return False

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncIterator[object]:
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, BlockingResponse):
            self._active = response
            response.started.set()
            await asyncio.to_thread(response.interrupted.wait)
            yield FakeResultMessage(response.output)
            return
        yield FakeResultMessage(str(response))

    async def interrupt(self) -> None:
        self.interrupts += 1
        if self._active is not None:
            self._active.interrupted.set()


class ClientFactory:
    def __init__(self, clients: list[FakeClient]) -> None:
        self.clients = list(clients)
        self.created: list[FakeClient] = []

    def __call__(self):  # type: ignore[no-untyped-def]
        client = self.clients.pop(0)
        self.created.append(client)
        return client


def delivery(delivery_id: str, message: str) -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        sender="requester",
        recipient="claude-worker",
        message=message,
    )


def process(
    factory: ClientFactory, *, on_turn_completed=None  # type: ignore[no-untyped-def]
) -> ClaudeAgentSdkProcess:
    return ClaudeAgentSdkProcess(
        HarnessLaunchSpec(
            harness="claude",
            name="claude-worker",
            headless=True,
            cwd="/work",
        ),
        client_factory=factory,
        reconnect_delay_seconds=0.01,
        on_turn_completed=on_turn_completed,
    )


def wait_until(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("timed out waiting for Agent SDK worker")


def wait_for_results(sdk: ClaudeAgentSdkProcess, count: int, timeout: float = 2.0):
    results = []

    def complete() -> bool:
        results.extend(sdk.drain_results())
        return len(results) >= count

    wait_until(complete, timeout)
    return results






def _configured_claude_launch(
    tmp_path: Path,
) -> tuple[AgentRegistry, AgentRuntimeContext, WorkerChannel, ChildEnvironmentLaunch]:
    personality = tmp_path / "personality"
    work = tmp_path / "work"
    work.mkdir()
    settings = personality / "native" / "claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"model":"p23-unit"}\n', encoding="utf-8")
    instructions = personality / "native" / "claude" / "CLAUDE.md"
    instructions.write_text("P23 unit instructions\n", encoding="utf-8")
    skill = personality / "shared" / "skills" / "p23-unit" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# P23 unit\n", encoding="utf-8")
    hyprial_home = tmp_path / "hyprial"
    hyprial_home.mkdir()
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="alice",
        machine="mac",
        hyprial_home=hyprial_home,
    )
    try:
        registry.create(
            "claude-worker",
            cwd=str(work),
            config={
                "sources": [{"path": str(personality), "required": True}],
                "discovery": "explicit-only",
            },
        )
        registry.ensure_home("claude-worker")
        context = resolve_agent_runtime_context(
            registry=registry,
            agent_name="claude-worker",
            harness="claude",
            cwd=str(work),
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert context is not None
        channel = build_worker_channel(
            actor=registry.require("claude-worker").uri,
            session_ref="session-config",
            hyprial_home=hyprial_home,
            state_dir=tmp_path / "daemon-state",
            runtime_context=context,
        )
        launch = compose_worker_child_launch(
            registry=registry,
            hyprial_home=hyprial_home,
            channel=channel,
            environ={
                "PATH": "/usr/bin:/bin",
                "HOME": "/ambient/home",
            },
            agent_name="claude-worker",
        )
        environment = launch.environment.for_exec()
        environment["ANTHROPIC_API_KEY"] = "P23_AGENT_KEY_NOT_A_SECRET"
        launch = dataclasses.replace(
            launch,
            environment=CompleteChildEnvironment(tuple(sorted(environment.items()))),
        )
        return registry, context, channel, launch
    except BaseException:
        registry.close()
        raise




























































# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'BlockingResponse',
    'ClientFactory',
    'FakeClient',
    'FakeResultMessage',
    '_configured_claude_launch',
    'delivery',
    'process',
    'wait_for_results',
    'wait_until',
]
