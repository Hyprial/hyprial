"""A P2 pi agent can run hyprial's canonical DeepSeek model.

hyprial names the model ``deepseek-flash`` (model_provider.DEEPSEEK_MODEL) and
refuses pi's built-in ``deepseek-v4-flash`` as retired.  pi itself only knows
``deepseek-flash`` from a models config; under agent-home P2 the agent's native
root holds just the linked auth.json, so the controlled loader answered
"configured Pi model is unavailable" (found by the pi manual leg, 2026-10-07).
The launch here is built by the production builder and run on the real SDK.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.model_provider import (
    DEEPSEEK_MODEL,
    DEEPSEEK_PROVIDER,
)
from hyprial.daemon.impl.harnesses.pi.loader import (
    PiProjectTrustReceipt,
    pi_sdk_launch_from_public_projection,
)

NODE = shutil.which("node")
PI = shutil.which("pi")


def _pi_package_root() -> Path | None:
    if PI is None:
        return None
    for parent in (Path(PI).resolve(), *Path(PI).resolve().parents):
        manifest = parent / "package.json"
        if manifest.is_file():
            try:
                if json.loads(manifest.read_text(encoding="utf-8")).get("name") == (
                    "@earendil-works/pi-coding-agent"
                ):
                    return parent
            except (OSError, json.JSONDecodeError):
                continue
    return None


PI_ROOT = _pi_package_root()

if os.environ.get("HYPRIAL_REQUIRE_PI_SDK") and (NODE is None or PI_ROOT is None):
    # CI installs the pinned SDK; a missing one there is a broken guard, not a skip.
    raise RuntimeError("HYPRIAL_REQUIRE_PI_SDK is set but node or the pi SDK is missing")

pytestmark = pytest.mark.skipif(
    NODE is None or PI_ROOT is None,
    reason="the real node and installed Pi SDK are required",
)


def _p2_roots(tmp_path: Path) -> dict[str, Path]:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    roots = {
        "repo": repository,
        "projection": tmp_path / "agent" / "state" / "config" / "native" / "pi",
        "native": tmp_path / "agent" / "secrets" / "native" / "pi",
        "sessions": tmp_path / "agent" / "state" / "pi",
        "home": tmp_path / "agent" / "state" / "home",
    }
    for name in ("projection", "native", "sessions", "home"):
        roots[name].mkdir(parents=True)
    # Exactly what P2 gives the agent: the linked credential and nothing else.
    (roots["native"] / "auth.json").write_text(
        json.dumps({DEEPSEEK_PROVIDER: {"type": "api_key", "key": "sk-test-not-real"}}),
        encoding="utf-8",
    )
    return roots


def test_p2_pi_launch_resolves_hyprials_canonical_deepseek_model(
    tmp_path: Path,
) -> None:
    roots = _p2_roots(tmp_path)
    environment = {
        "HOME": str(roots["home"]),
        "PI_CODING_AGENT_DIR": str(roots["native"]),
    }
    launch = pi_sdk_launch_from_public_projection(
        {
            "mode": "agent-home-p2",
            "harness": "pi",
            "projectionRoot": str(roots["projection"]),
            "nativeRoot": str(roots["native"]),
            "sessionRoot": str(roots["sessions"]),
            "environment": environment,
        },
        trust=PiProjectTrustReceipt(roots["repo"], roots["repo"], True),
        mode="rpc",
        session_id="p2-deepseek-model",
        pi_package_root=PI_ROOT or Path("/missing"),
        node=NODE or "node",
        model_provider=DEEPSEEK_PROVIDER,
        model=DEEPSEEK_MODEL,
    )

    completed = subprocess.run(
        launch.argv,
        input='{"id":"state","type":"get_state"}\n',
        cwd=roots["repo"],
        env={
            **launch.environment,
            "PATH": os.environ.get("PATH", ""),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
        },
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    rows = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    (state,) = [row for row in rows if row.get("id") == "state"]
    model = state["data"]["model"]
    assert (model["provider"], model["id"]) == (DEEPSEEK_PROVIDER, DEEPSEEK_MODEL)
    assert "sk-test-not-real" not in completed.stdout + completed.stderr


class _FakeChatCompletions:
    """A local OpenAI-compatible endpoint that answers one streamed turn."""

    def __init__(self) -> None:
        import http.server
        import threading

        self.models: list[str] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.models.append(str(body.get("model")))
                chunks = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "pong"}}]},
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
                    },
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in chunks:
                    payload = {"id": "c1", "object": "chat.completion.chunk", "created": 0,
                               "model": owner.models[-1], **chunk}
                    self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_p2_pi_canonical_deepseek_model_completes_a_turn(tmp_path: Path) -> None:
    """Review 1028: resolving the model is not enough; a turn must run.

    A registered provider gets no defaults, so a definition without ``cost``
    resolved but failed every turn ("reading 'tiers'").
    """

    endpoint = _FakeChatCompletions()
    try:
        roots = _p2_roots(tmp_path)
        environment = {
            "HOME": str(roots["home"]),
            "PI_CODING_AGENT_DIR": str(roots["native"]),
            "HYPRIAL_OPENAI_BASE_URL": endpoint.base_url,
        }
        launch = pi_sdk_launch_from_public_projection(
            {
                "mode": "agent-home-p2",
                "harness": "pi",
                "projectionRoot": str(roots["projection"]),
                "nativeRoot": str(roots["native"]),
                "sessionRoot": str(roots["sessions"]),
                "environment": environment,
            },
            trust=PiProjectTrustReceipt(roots["repo"], roots["repo"], True),
            mode="rpc",
            session_id="p2-deepseek-turn",
            pi_package_root=PI_ROOT or Path("/missing"),
            node=NODE or "node",
            model_provider=DEEPSEEK_PROVIDER,
            model=DEEPSEEK_MODEL,
        )
        events: list[dict[str, object]] = []
        stderr = ""
        with subprocess.Popen(
            launch.argv,
            cwd=roots["repo"],
            env={
                **launch.environment,
                "PATH": os.environ.get("PATH", ""),
                "PI_OFFLINE": "1",
                "PI_TELEMETRY": "0",
            },
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            assert process.stdin is not None and process.stdout is not None
            # A hung loader must fail this test, not the run: the read loop
            # sits behind a hard deadline that kills the child.
            import threading

            watchdog = threading.Timer(60.0, process.kill)
            watchdog.start()
            try:
                process.stdin.write('{"id":"p","type":"prompt","message":"ping"}\n')
                process.stdin.flush()
                for line in process.stdout:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    events.append(event)
                    if event.get("type") in {"agent_end", "agent_settled"}:
                        break
            finally:
                watchdog.cancel()
                process.stdin.close()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                stderr = process.stderr.read() if process.stderr else ""
        stop_reasons = [
            message.get("stopReason")
            for event in events
            for message in ([event.get("message")] + list(event.get("messages") or []))  # type: ignore[operator]
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]
        assert stop_reasons and stop_reasons[-1] == "stop", (
            stop_reasons,
            stderr[-2000:],
        )
        # pi puts the model id on the wire: the canonical name, never a retired one.
        assert endpoint.models and set(endpoint.models) == {DEEPSEEK_MODEL}
    finally:
        endpoint.close()
