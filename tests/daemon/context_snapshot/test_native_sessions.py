from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon import ContextSnapshotExporter

from .support import agent_home, native_session_path, request


def _write_jsonl(path: Path, values: list[object], *, suffix: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            json.dumps(item, separators=(",", ":")).encode() + b"\n"
            for item in values
        )
        + suffix
    )


@pytest.mark.parametrize(
    ("harness", "values", "expected"),
    [
        (
            "claude",
            [
                {"type": "user", "message": {"role": "user", "content": "question"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {"path": "src/a.py"}}]}},
                {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "tool answer"}]}},
            ],
            ("question", "call-1", "tool answer"),
        ),
        (
            "pi",
            [
                {"type": "session", "version": 1, "id": "session-1", "cwd": "/work"},
                {"type": "message", "message": {"role": "user", "content": "question"}},
                {"type": "message", "message": {"role": "assistant", "content": "answer"}},
                {"type": "toolCall", "toolCallId": "call-1", "name": "read_file", "arguments": {}},
                {"type": "toolResult", "toolCallId": "call-1", "content": "tool answer"},
            ],
            ("question", "answer", "tool answer"),
        ),
        (
            "codex",
            [
                {"type": "session_meta", "payload": {"id": "session-1", "version": 1}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "question"}]}},
                {"type": "response_item", "payload": {"type": "function_call", "call_id": "call-1", "name": "read_file", "arguments": "{}"}},
                {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1", "output": "tool answer"}},
            ],
            ("question", "call-1", "tool answer"),
        ),
    ],
)
def test_native_formats_preserve_complete_messages_and_tool_association(
    tmp_path: Path,
    harness: str,
    values: list[object],
    expected: tuple[str, ...],
) -> None:
    with agent_home(tmp_path) as (registry, agent, home):
        source = native_session_path(home, harness, str(agent.cwd), "session-1")
        _write_jsonl(source, values, suffix=b'{"half":"\xe4\xb8')
        original = source.read_bytes()
        result = ContextSnapshotExporter(registry).export(
            request(agent, harness=harness).with_switch_id(f"switch-{harness}")
        )
        body = (result.path / "handoff.md").read_text()
        for fragment in expected:
            assert fragment in body
        manifest = json.loads((result.path / "manifest.json").read_text())
        assert "native-session-torn-record" in {
            gap["code"] for gap in manifest["gaps"]
        }
        native = next(item for item in manifest["sources"] if item["kind"] == "native-session")
        assert native["digest"] and len(native["digest"]) == 64
        assert not source.is_relative_to(result.path)
        assert source.read_bytes() == original


def test_byte_and_token_limits_redaction_and_digest_are_enforced(tmp_path: Path) -> None:
    with agent_home(tmp_path) as (registry, agent, home):
        source = native_session_path(home, "codex", str(agent.cwd), "session-1")
        secret = "sk-" + "a" * 40
        values: list[object] = [
            {"type": "session_meta", "payload": {"id": "session-1", "version": 1}}
        ]
        values.extend(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"row-{index} {secret}"}],
                },
            }
            for index in range(80)
        )
        _write_jsonl(source, values)
        result = ContextSnapshotExporter(registry).export(
            request(agent, harness="codex", read_limit=1024, token_limit=32)
        )
        body = (result.path / "handoff.md").read_text()
        manifest_text = (result.path / "manifest.json").read_text()
        manifest = json.loads(manifest_text)
        assert secret not in body
        assert secret not in manifest_text
        assert "<redacted>" in body or "[REDACTED]" in body
        codes = {gap["code"] for gap in manifest["gaps"]}
        assert "native-session-byte-limit" in codes
        assert "native-session-token-limit" in codes
        native = next(item for item in manifest["sources"] if item["kind"] == "native-session")
        assert native["capturedBytes"] <= 1024
        assert native["sourceBytes"] > native["capturedBytes"]


def test_unknown_native_version_is_unsupported_partial(tmp_path: Path) -> None:
    with agent_home(tmp_path) as (registry, agent, home):
        source = native_session_path(home, "pi", str(agent.cwd), "session-1")
        _write_jsonl(
            source,
            [
                {"type": "session", "version": 99, "id": "session-1"},
                {"type": "message", "message": {"role": "user", "content": "unsafe"}},
            ],
        )
        result = ContextSnapshotExporter(registry).export(
            request(agent, harness="pi").with_switch_id("unknown-version")
        )
        manifest = json.loads((result.path / "manifest.json").read_text())
        assert manifest["coverage"] in {"partial", "unavailable"}
        assert "native-format-unsupported" in {
            gap["code"] for gap in manifest["gaps"]
        }
        assert "unsafe" not in (result.path / "handoff.md").read_text()
