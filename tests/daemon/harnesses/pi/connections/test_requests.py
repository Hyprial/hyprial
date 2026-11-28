"""Exact requests, tool round trips, unknown cost and sanitized upstream failures."""

from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from .support import options, running, reservation
from .test_binding import binding


@contextmanager
def upstream(*, status=200, tool=False, error_code="model_not_found", invalid=False):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
            self.end_headers()
            if status != 200:
                self.wfile.write(json.dumps({"error": {"message": "private-vendor-secret synthetic-pi-test-key", "code": error_code}}).encode())
                return
            if invalid:
                self.wfile.write(b'data: invalid-response\n\n')
                return
            use_tool = tool and len(requests) == 1
            delta = {"role": "assistant", "content": "fixture reply"}
            if use_tool:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_fixture", "type": "function", "function": {"name": "read", "arguments": '{"path":"fixture.txt"}'}}]}
            for change, finish in [(delta, None), ({}, "tool_calls" if use_tool else "stop")]:
                chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": body["model"], "created": 0,
                         "choices": [{"index": 0, "delta": change, "finish_reason": finish}]}
                self.wfile.write(f'data: {json.dumps(chunk)}\n\n'.encode())
            self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def prompt(process, rows):
    process.stdin.write('{"type":"prompt","id":"prompt","message":"Read fixture.txt if appropriate, then reply."}\n')
    process.stdin.flush()
    events = []
    while True:
        item = rows.get(timeout=20)
        assert item is not None
        events.append(item)
        if item.get("type") in {"agent_end", "agent_settled"}:
            return events


@pytest.mark.parametrize("model", ["hy4-flash", "qwen3.7-flash", "eepseek-v4.1-flash-1017", "gpt-5-custom"])
@pytest.mark.parametrize("tool", [False, True])
def test_real_sdk_precise_request_and_tool_roundtrip(tmp_path, model, tool):
    with upstream(tool=tool) as (endpoint, requests):
        selected = binding()
        selected = replace(selected, connection=replace(selected.connection, endpoint=endpoint), model=model)
        data, env = options(tmp_path, selected=selected)
        (Path(data["cwd"]) / "fixture.txt").write_text("synthetic tool result")
        with running(data, env) as (process, rows):
            events = prompt(process, rows)
            assert len(requests) == (2 if tool else 1)
            for path, authorization, body in requests:
                assert path == "/v1/chat/completions"
                assert authorization == "Bearer synthetic-pi-test-key"
                assert body["model"] == model and body["max_tokens"] == 4096
                assert not {"reasoning_effort", "store", "thinking", "thinking_budget"} & body.keys()
                assert not any(row["role"] == "developer" for row in body["messages"])
            if tool:
                assert any(row["role"] == "tool" and "synthetic tool result" in str(row) for row in requests[-1][2]["messages"])
            process.stdin.write('{"type":"get_session_stats","id":"stats"}\n')
            process.stdin.flush()
            while True:
                stats = rows.get(timeout=10)
                if stats.get("id") == "stats":
                    assert stats["data"]["cost"] is None and stats["data"]["costStatus"] == "unknown"
                    break
            assert "fixture reply" in json.dumps(events)
        assert not reservation(data).exists()
        assert all(b"synthetic-pi-test-key" not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


@pytest.mark.parametrize("status,reason", [(404, "model-not-found"), (401, "authentication-rejected"), (429, "rate-limited"), (500, "request-rejected")])
def test_upstream_error_is_sanitized_and_never_retried(tmp_path, status, reason):
    with upstream(status=status) as (endpoint, requests):
        selected = binding()
        data, env = options(tmp_path, selected=replace(selected, connection=replace(selected.connection, endpoint=endpoint)))
        with running(data, env) as (process, rows):
            events = prompt(process, rows)
            rendered = json.dumps(events)
            assert len(requests) == 1
            assert reason in rendered
            assert "private-vendor-secret" not in rendered and "synthetic-pi-test-key" not in rendered


@pytest.mark.parametrize("status,invalid,reason", [(404, False, "request-rejected"), (200, True, "invalid-response")])
def test_upstream_protocol_failure_does_not_guess_model_missing(tmp_path, status, invalid, reason):
    with upstream(status=status, invalid=invalid, error_code="route_not_found") as (endpoint, requests):
        selected = binding()
        data, env = options(tmp_path, selected=replace(selected, connection=replace(selected.connection, endpoint=endpoint)))
        with running(data, env) as (process, rows):
            rendered = json.dumps(prompt(process, rows))
            assert f"MODEL_UPSTREAM_ERROR: request/{reason}" in rendered
            assert len(requests) == 1


def test_real_resume_retains_snapshot_and_agent_model_store_isolation(tmp_path):
    with upstream() as (endpoint, requests):
        selected = binding()
        selected = replace(selected, connection=replace(selected.connection, endpoint=endpoint))
        first, env = options(tmp_path / "first", selected=selected)
        with running(first, env) as (process, rows):
            prompt(process, rows)
        receipt = Path(first["receiptPath"])
        snapshot = json.loads(receipt.read_text())["modelBinding"]
        sessions = list(Path(first["sessionRoot"]).glob("*.jsonl"))
        assert len(sessions) == 1
        original_session = sessions[0]
        first["requireExistingSession"] = True
        first["modelBinding"]["bindingRevision"] += 1
        with running(first, env) as (process, rows):
            prompt(process, rows)
        assert list(Path(first["sessionRoot"]).glob("*.jsonl")) == [original_session]
        assert json.loads(receipt.read_text())["modelBinding"] == snapshot
        assert len(requests[-1][2]["messages"]) > len(requests[0][2]["messages"])
        store = Path(first["nativeRoot"]) / "models-store.json"
        before = store.read_bytes()
        second, env2 = options(tmp_path / "second", selected=replace(selected, model="another-opaque-model"))
        with running(second, env2) as (process, rows):
            prompt(process, rows)
        other_store = Path(second["nativeRoot"]) / "models-store.json"
        assert other_store.exists() and other_store != store
        assert store.read_bytes() == before
        assert requests[-1][2]["model"] == "another-opaque-model"
