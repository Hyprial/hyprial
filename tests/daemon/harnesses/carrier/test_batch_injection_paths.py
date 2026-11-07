"""RED architecture tests for daemon-rendered batches at every model edge."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl import DesiredState, InteractiveSession
from hyprial.daemon.impl.harnesses.dsh_tui.carrier import DshPluginWakeDriver
from hyprial.daemon.impl.mcp import (
    ClaudeChannelAdapter,
    ClaudeChannelDriver,
    StatelessDaemonProxy,
    WakeCommand,
    WakeCoordinator,
)
from hyprial.kernel.impl.contracts.channels.worker_channel import (
    WorkerBinding,
    bound_request,
)


ROOT = Path(__file__).resolve().parents[4]
BATCH_TEXT = "[Harness Network inbox batch cursor=sentinel]\nBYTE-EXACT-秘密"
BATCH_RESPONSE: dict[str, Any] = {
    "ok": True,
    "schemaVersion": 1,
    "format": "inbox-batch-v1",
    "cursor": "cursor-sentinel",
    "daemonEpoch": "epoch-1",
    "actor": "alice",
    "itemCount": 1,
    "totalBodyBytes": 4,
    "inlineLimitBytes": 16384,
    "items": [{"messageId": "m1", "deliveryId": "m1", "body": "body"}],
    "delivery": {
        "mode": "inline",
        "text": BATCH_TEXT,
        "spillPath": None,
        "spillBytes": None,
        "spillSha256": None,
    },
}


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HYPRIAL_HOME", str(home / ".hyprial"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "isolated.sock"))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")


@dataclass
class _Store:
    state: DesiredState

    def load(self) -> DesiredState:
        return self.state


class _Factory:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def connect(self):  # type: ignore[no-untyped-def]
        owner = self

        class _Connection:
            async def request(
                self,
                method: str,
                params: dict[str, Any],
                *,
                request_id: str | None = None,
            ) -> dict[str, Any]:
                del request_id
                owner.calls.append((method, params))
                return owner.responses.pop(0)

        connection = _Connection()

        class _Context:
            async def __aenter__(self):  # type: ignore[no-untyped-def]
                return connection

            async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
                return None

        return _Context()


class _Channel:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))


def _session() -> InteractiveSession:
    return InteractiveSession(
        actor="alice",
        cwd="/work/alice",
        command=("claude",),
        source="claude-channel",
    )


def _adapter(
    factory: _Factory, channel: _Channel | None = None
) -> tuple[ClaudeChannelAdapter, _Channel]:
    edge = channel or _Channel()
    return (
        ClaudeChannelAdapter(
            StatelessDaemonProxy(factory),
            WakeCoordinator(
                _Store(DesiredState(interactive_sessions=(_session(),))),
                ClaudeChannelDriver(edge),
            ),
            actor="alice",
            session_ref="session-1",
            cwd="/work/alice",
            command=("claude",),
        ),
        edge,
    )


def _source(relative: str) -> str:
    return (ROOT / relative).read_text()


@pytest.mark.anyio
async def test_claude_channel_injects_daemon_delivery_text_byte_exactly() -> None:
    factory = _Factory(
        [
            {"ok": True, "daemonEpoch": "epoch-1"},
            {"ok": True},
            {
                "ok": True,
                "daemonEpoch": "epoch-1",
                "messages": [{"messageId": "m1", "deliveryId": "m1"}],
            },
            BATCH_RESPONSE,
        ]
    )
    adapter, channel = _adapter(factory)

    await adapter.start()

    assert [method for method, _params in factory.calls] == [
        "session.register",
        "session.turn.refresh",
        "message.pending.list",
        "message.pending.batch",
    ]
    assert len(channel.notifications) == 1
    assert channel.notifications[0][1]["content"] == BATCH_TEXT


@pytest.mark.anyio
async def test_channel_adapter_harness_read_returns_daemon_batch_without_rendering() -> None:
    factory = _Factory([{"ok": True}, BATCH_RESPONSE])
    adapter, _channel = _adapter(factory)

    result = await adapter.harness_read()

    assert result == BATCH_RESPONSE
    assert [method for method, _params in factory.calls] == [
        "session.turn.started",
        "message.pending.batch",
    ]


def test_worker_channel_forwards_successor_cursor_without_item_exclusions() -> None:
    binding = WorkerBinding(
        actor="agent:op:node-1:worker",
        session_ref="session-1",
        daemon_epoch="epoch-1",
    )
    method, params = bound_request(
        {
            "version": 1,
            "id": "batch-1",
            "method": "message.pending.batch",
            "params": {"afterCursor": "cursor-previous"},
        },
        binding,
    )

    assert method == "message.pending.batch"
    assert params == {
        "actor": binding.actor,
        "sessionRef": binding.session_ref,
        "afterCursor": "cursor-previous",
    }
    assert "excludeMessageIds" not in params


@pytest.mark.anyio
async def test_dsh_plugin_injects_daemon_delivery_text_byte_exactly(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    captured: list[dict[str, object]] = []
    driver = DshPluginWakeDriver(tmp_path / "dsh.sock")

    def roundtrip(frame, _timeout=None):  # type: ignore[no-untyped-def]
        captured.append(frame)
        return {"ok": True, "state": "accepted"}

    monkeypatch.setattr(driver, "_roundtrip", roundtrip)
    outcome = await driver.wake(
        WakeCommand(
            kind="formal",
            actor="alice",
            delivery_id="cursor-sentinel",
            cwd="/work/alice",
            command=("dsh",),
            prompt=BATCH_TEXT,
        )
    )

    assert outcome.status.value == "accepted"
    assert captured == [
        {
            "type": "wake",
            "prompt": BATCH_TEXT,
            "outcomeTimeoutMs": 8000,
        }
    ]


def test_shared_renderer_belongs_to_inbox_batch_module() -> None:
    renderer = _source("src/hyprial/daemon/impl/inbox/batch/render.py")
    assert "inbox-batch-v1" in renderer
    assert "BEGIN UNTRUSTED MESSAGE DATA" in renderer

    old_carrier = _source(
        "src/hyprial/daemon/impl/harnesses/carrier/contract.py"
    )
    assert "def render_injection" not in old_carrier


def test_wire_contract_and_workflow_skill_publish_the_batch_shape() -> None:
    wire = _source("contract/wire/inbox-batch-v1.md")
    assert "message.pending.batch" in wire
    assert "inbox-batch-v1" in wire
    assert all(
        key in wire
        for key in ("cursor", "afterCursor", "items", "delivery", "spillPath")
    )
    assert "excludeMessageIds" not in wire
    assert "system_notices" in wire and "drain" in wire

    skill = _source(".agents/skills/hyprial-workflow/SKILL.md")
    assert "message.pending.batch" in skill
    assert "每个" in skill and ("reply" in skill or "ack" in skill)
