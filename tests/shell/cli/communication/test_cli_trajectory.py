from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.commands.message.observe.trajectory as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

STATUS_QUERY = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "hyprial"
    / "daemon"
    / "impl"
    / "application"
    / "messaging"
    / "delivery"
    / "status_queries.py"
)


def _write_log(state_dir: Path, entries: list[dict[str, object]]) -> None:
    path = state_dir / "logs" / "daemon.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries),
        encoding="utf-8",
    )


def _entry(
    ts: str,
    event: str,
    *,
    component: str,
    node: str,
    message_id: str = "msg-trajectory-1",
    **fields: object,
) -> dict[str, object]:
    return {
        "ts": ts,
        "level": "info",
        "component": component,
        "name": "node-a",
        "event": event,
        "messageId": message_id,
        "correlationId": message_id,
        "node": node,
        **fields,
    }


def test_trajectory_merges_logs_status_and_agent_timeline_in_timestamp_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    _write_log(
        state_dir,
        [
            _entry(
                "2026-08-16T01:00:00.000Z",
                "adapter.inbound",
                component="adapter",
                node="adapter-inbound",
                sender="channel:lark:test",
                target="agent:alice:mac:worker",
            ),
            _entry(
                "2026-08-16T01:00:01.000Z",
                "send.received",
                component="daemon",
                node="daemon-send",
                sender="channel:lark:test",
                target="agent:alice:mac:worker",
            ),
            _entry(
                "2026-08-16T01:00:02.000Z",
                "wake.signalled",
                component="claude-worker",
                node="wake",
                actorId="agent:alice:mac:worker",
            ),
            _entry(
                "2026-08-16T01:00:03.000Z",
                "worker.turn.started",
                component="claude-worker",
                node="worker-turn",
                actorId="agent:alice:mac:worker",
            ),
            {
                "ts": "2026-08-16T01:00:03.500Z",
                "level": "info",
                "component": "agents",
                "name": "node-a",
                "event": "agent.harness.handover",
                "actor": "agent:alice:mac:worker",
                "previousHarness": "pi",
                "nextHarness": "claude",
            },
        ],
    )

    def request(method: str, params: dict[str, object], **_kwargs: object):
        assert method == "message.status"
        assert params == {
            "from": "channel:lark:test",
            "messageId": "msg-trajectory-1",
        }
        return {
            "ok": True,
            "messageId": "msg-trajectory-1",
            "state": "pending",
            "records": [],
            "diagnostics": {"noKnownLoss": True},
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    result = _cli_owner_1._trajectory_result("msg-trajectory-1")

    assert result["schemaVersion"] == 1
    assert result["found"] is True
    nodes = result["nodes"]
    assert [node["event"] for node in nodes] == [
        "adapter.inbound",
        "send.received",
        "wake.signalled",
        "worker.turn.started",
        "message.status.pending",
    ]
    assert [node["state"] for node in nodes] == [
        "completed",
        "completed",
        "completed",
        "running",
        "running",
    ]
    assert [node["ts"] for node in nodes[:-1]] == sorted(
        node["ts"] for node in nodes[:-1]
    )
    assert result["agentTimeline"][0]["event"] == "agent.harness.handover"
    assert result["ordering"]["authoritative"] is False
    schema = json.loads(
        (Path(__file__).resolve().parents[4] / 'contract/wire/trajectory.schema.json').read_text()
    )
    Draft202012Validator(schema).validate(result)


def test_trajectory_includes_a_consumed_delivery_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    _write_log(
        state_dir,
        [
            _entry(
                "2026-10-08T01:00:00.000Z",
                "worker.turn.skipped",
                component="claude-worker",
                node="worker-turn",
                actorId="agent:alice:mac:worker",
                deliveryId="msg-trajectory-1",
                conversationId="conversation",
                sender="agent:alice:mac:sender",
                recipient="agent:alice:mac:worker",
                reason="already-consumed",
            )
        ],
    )
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {"ok": True, "state": "unknown", "records": []},
    )

    result = _cli_owner_1._trajectory_result("msg-trajectory-1")

    assert result["nodes"][0]["event"] == "worker.turn.skipped"
    assert result["nodes"][0]["state"] == "completed"
    assert result["nodes"][0]["details"]["reason"] == "already-consumed"


def test_trajectory_marks_unknown_and_both_terminal_statuses_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert 'projected["state"] = "unconfirmed"' not in STATUS_QUERY.read_text(
        encoding="utf-8"
    )
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    _write_log(
        state_dir,
        [
            _entry(
                "2026-08-16T02:00:00.000Z",
                "send.received",
                component="daemon",
                node="daemon-send",
                sender="agent:alice:mac:sender",
            )
        ],
    )
    replies = iter(
        [
            {"ok": True, "state": "unknown", "records": []},
            {
                "ok": True,
                "state": "expired",
                "records": [
                    {
                        "messageId": "msg-trajectory-1",
                        "state": "expired",
                        "holder": "node-a",
                        "reason": "TTL_EXPIRED",
                        "recordedAtMs": 1786845600500,
                    }
                ],
            },
            {
                "ok": True,
                "state": "fetched",
                "records": [
                    {
                        "messageId": "msg-trajectory-1",
                        "state": "fetched",
                        "holder": "node-b",
                        "reason": "ACK_RECEIVED",
                        "recordedAtMs": 1786845601000,
                    }
                ],
            },
            {
                "ok": True,
                "state": "unconfirmed",
                "records": [],
            },
        ]
    )
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_args, **_kwargs: next(replies))

    unknown = _cli_owner_1._trajectory_result("msg-trajectory-1")
    expired = _cli_owner_1._trajectory_result("msg-trajectory-1")
    completed = _cli_owner_1._trajectory_result("msg-trajectory-1")
    rejected = _cli_owner_1._trajectory_result("msg-trajectory-1")

    assert unknown["nodes"][-1]["state"] == "unknown"
    assert unknown["nodes"][-1]["event"] == "message.status.unknown"
    assert expired["nodes"][-1]["state"] == "completed"
    assert expired["nodes"][-1]["event"] == "message.status.expired"
    assert "observedState" not in expired["nodes"][-1]["details"]["record"]
    assert "observedReason" not in expired["nodes"][-1]["details"]["record"]
    assert completed["nodes"][-1]["state"] == "completed"
    assert completed["nodes"][-1]["event"] == "message.status.fetched"
    assert completed["nodes"][-1]["source"] == "message-status"
    assert rejected["nodes"][-1]["state"] == "unknown"
    assert rejected["nodes"][-1]["event"] == "message.status.unknown"
    assert rejected["nodes"][-1]["details"]["error"] == (
        "unsupported message.status state: 'unconfirmed'"
    )


def test_trajectory_json_schema_and_local_not_found_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    runner = CliRunner()

    missing = runner.invoke(_cli_root_app, ["message", "trajectory", "msg-missing"], color=False)
    missing_json = runner.invoke(
        _cli_root_app, ["message", "trajectory", "msg-missing", "--json"], color=False
    )

    assert missing.exit_code == 0
    assert "本机日志无此消息" in missing.output
    payload = json.loads(missing_json.stdout)
    assert payload == {
        "schemaVersion": 1,
        "ok": True,
        "found": False,
        "messageId": "msg-missing",
        "message": "本机日志无此消息",
        "nodes": [],
        "agentTimeline": [],
        "skippedLines": 0,
        "ordering": {
            "key": "ts",
            "authoritative": False,
            "detail": "cross-node timestamps are display order only",
        },
    }


def test_trajectory_ignores_correlated_operational_events_outside_vocabulary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    _write_log(
        state_dir,
        [
            _entry(
                "2026-08-16T03:00:00.000Z",
                "send.received",
                component="daemon",
                node="daemon-send",
                sender="agent:alice:mac:sender",
                target="agent:bob:mac:worker",
            ),
            _entry(
                "2026-08-16T03:00:01.000Z",
                "message.status.served",
                component="daemon",
                node="delivery-terminal",
            ),
            _entry(
                "2026-08-16T03:00:02.000Z",
                "outbox.prune.dry-run",
                component="daemon",
                node="delivery-terminal",
            ),
        ],
    )
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {"ok": True, "state": "unknown", "records": []},
    )

    result = _cli_owner_1._trajectory_result("msg-trajectory-1")

    assert [node["event"] for node in result["nodes"]] == [
        "send.received",
        "message.status.unknown",
    ]


def test_trajectory_does_not_project_pre_contract_alarm_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    base = {
        "level": "warn",
        "component": "daemon",
        "name": "node-a",
        "correlationId": "msg-alarm-1",
        "originalMessageId": "msg-alarm-1",
        "conversationId": "conversation-1",
        "sender": "agent:alice:mac:sender",
        "recipient": "agent:bob:mac:worker",
        "reason": "HOLD_TTL_EXPIRED",
        "audience": "agent",
    }
    _write_log(
        state_dir,
        [
            {**base, "ts": "2026-08-16T04:00:00.000Z", "event": "terminal.failure"},
            {**base, "ts": "2026-08-16T04:00:01.000Z", "event": "alarm.raised"},
            {**base, "ts": "2026-08-16T04:00:02.000Z", "event": "alarm.delivered"},
        ],
    )
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {"ok": True, "state": "expired", "records": []},
    )

    result = _cli_owner_1._trajectory_result("msg-alarm-1")

    assert result["found"] is False
    assert result["nodes"] == []
    assert result["skippedLines"] == 0


def test_trajectory_rejects_non_contract_message_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    entry = _entry(
        "2026-08-16T05:00:00.000Z",
        "send.received",
        component="daemon",
        node="daemon-send",
        sender="agent:alice:mac:sender",
    )
    entry["messageId"] = "legacy-wrong-id"
    _write_log(state_dir, [entry])
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {"ok": True, "state": "unknown", "records": []},
    )

    result = _cli_owner_1._trajectory_result("msg-trajectory-1")

    assert result["found"] is False
    assert result["nodes"] == []
    assert result["skippedLines"] == 1


def test_trajectory_skips_malformed_matching_log_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    valid = _entry(
        "2026-08-16T05:30:00.000Z",
        "send.received",
        component="daemon",
        node="daemon-send",
        sender="agent:alice:mac:sender",
    )
    missing_name = {key: value for key, value in valid.items() if key != "name"}
    missing_message_id = {
        key: value for key, value in valid.items() if key != "messageId"
    }
    path = state_dir / "logs" / "daemon.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "".join(
            json.dumps(entry, separators=(",", ":")) + "\n"
            for entry in (valid, missing_name, missing_message_id)
        )
        + "not-json\n"
        + '{"ts":"2026-08-16T05:30:01.000Z"',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {"ok": True, "state": "unknown", "records": []},
    )

    result = _cli_owner_1._trajectory_result("msg-trajectory-1")

    assert result["found"] is True
    assert result["skippedLines"] == 4
    assert [node["event"] for node in result["nodes"]] == [
        "send.received",
        "message.status.unknown",
    ]

    rendered = CliRunner().invoke(
        _cli_root_app, ["message", "trajectory", "msg-trajectory-1"], color=False
    )
    assert rendered.exit_code == 0
    assert "skipped 4 malformed lines" in rendered.output
    json_rendered = CliRunner().invoke(
        _cli_root_app, ["message", "trajectory", "msg-trajectory-1", "--json"], color=False
    )
    assert json_rendered.exit_code == 0
    assert json.loads(json_rendered.stdout)["skippedLines"] == 4


@pytest.mark.parametrize(
    "status",
    [
        {
            "ok": True,
            "messageId": "different-message",
            "state": "fetched",
            "records": [],
        },
        {
            "ok": True,
            "messageId": "msg-trajectory-1",
            "state": "fetched",
            "records": [
                {
                    "messageId": "different-message",
                    "state": "fetched",
                    "holder": "node-b",
                    "recordedAtMs": 1786845601000,
                }
            ],
        },
    ],
)
def test_trajectory_never_joins_status_for_a_different_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: dict[str, object],
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    _write_log(
        state_dir,
        [
            _entry(
                "2026-08-16T06:00:00.000Z",
                "send.received",
                component="daemon",
                node="daemon-send",
                sender="agent:alice:mac:sender",
            )
        ],
    )
    monkeypatch.setattr(_cli_support, "_daemon_request", lambda *_args, **_kwargs: status)

    result = _cli_owner_1._trajectory_result("msg-trajectory-1")

    status_node = result["nodes"][-1]
    assert status_node["event"] == "message.status.unknown"
    assert status_node["state"] == "unknown"
    assert "error" in status_node["details"]
    assert "record" not in status_node["details"]
