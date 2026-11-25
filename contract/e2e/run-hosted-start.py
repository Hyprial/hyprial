"""AT03: real CLI/daemon/Pi hosted identity and restart; loopback model only.

The old transfer-receive row is a seeded fixture, not a transfer E2E claim.
AT09 grants exactly the driver principal in the worker ledger so harness_send
settles through the intended send-to path rather than TARGET_NOT_AUTHORIZED.
"""
import importlib.util
import json
import re
from pathlib import Path
import socket
import sqlite3
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hosted_registry", ROOT / "contract/e2e/run-hosted-registry.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def request(path, method, params, *, ok=True):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(45)
        client.connect(str(path))
        client.sendall(json.dumps({"version": 1, "id": uuid.uuid4().hex, "method": method, "params": params}).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            part = client.recv(65536)
            assert part, "closed before response"
            data += part
        result = json.loads(data.split(b"\n")[0])
        assert ("error" not in result) == ok, f"{method}: {result}"
        return result["result"] if ok else result["error"]


LOOPBACK_PROVIDER = "deepseek"
LOOPBACK_MODEL = "deepseek-flash"
LOOPBACK_KEY = "e2e-local-provider-not-a-real-key"
AT03_NONCE = re.compile(r"AT03_[a-f0-9]{32}")
INBOX_MESSAGE_ID = re.compile(
    r"(?m)^messageId: ([^\r\n]+)$"
    r"|\(intent: [^,\r\n]+, messageId: ([^,)\r\n]+)"
)


def _message_text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    return json.dumps(message)


def _round_batch(messages, round_nonce):
    """Return the newest user turn carrying this round and its inbox items."""

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        text = _message_text(message)
        if round_nonce not in text:
            continue
        headers = list(INBOX_MESSAGE_ID.finditer(text))
        entries = []
        for offset, header in enumerate(headers):
            end = headers[offset + 1].start() if offset + 1 < len(headers) else len(text)
            nonce = AT03_NONCE.search(text, header.end(), end)
            if nonce is not None:
                message_id = (header.group(1) or header.group(2)).strip()
                entries.append((message_id, nonce.group()))
        return index, entries
    return None, []


def _next_model_action(messages, *, round_nonce, expected, driver):
    """Answer only the current nonce, then settle every AT03 item in the batch."""

    latest_user, batch = _round_batch(messages, round_nonce)
    if latest_user is None or not any(nonce == round_nonce for _, nonce in batch):
        return None, None
    tool_messages = [
        message for message in messages[latest_user + 1:]
        if message.get("role") == "tool"
    ]
    if not tool_messages:
        return "harness_whoami", {}
    if len(tool_messages) == 1:
        assert expected in json.dumps(tool_messages), tool_messages
        return "harness_send", {
            "to": driver, "summary": round_nonce, "message": round_nonce,
        }
    ack_index = len(tool_messages) - 2
    if ack_index < len(batch):
        return "harness_ack", {"messageId": batch[ack_index][0]}
    return None, None


class HostedPiScenario(base.Scenario):
    scope = "local-owner real Pi worker, identity and restart; synthetic loopback model, L0 only"

    def run(self):
        self.env["PATH"] = "/opt/homebrew/bin:" + self.env["PATH"]
        expected = driver = None
        round_nonce = None
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                base.write_json(self.server.root / "model-requests.json", requests)
                name, arguments = _next_model_action(
                    body["messages"], round_nonce=round_nonce,
                    expected=expected, driver=driver,
                )
                tool_messages = [
                    message for message in body["messages"]
                    if message.get("role") == "tool"
                ]
                delta = {"role": "assistant"}
                if name:
                    delta["tool_calls"] = [{"index": 0, "id": "fixture-" + str(len(tool_messages)), "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(arguments)}}]
                else:
                    delta["content"] = "completed"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for payload, finish in ((delta, None), ({}, "tool_calls" if name else "stop")):
                    chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                             "model": LOOPBACK_MODEL, "choices": [{"index": 0, "delta": payload, "finish_reason": finish}]}
                    if finish:
                        chunk["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.root = self.root
        threading.Thread(target=server.serve_forever, daemon=True).start()
        pi = shutil.which("pi", path=self.env["PATH"])
        version = subprocess.run([pi, "--version"], cwd=self.root, env=self.env,
                                 capture_output=True, text=True, check=True)
        provider_url = f"http://127.0.0.1:{server.server_port}/v1"
        self.env.update({"HYPRIAL_OPENAI_BASE_URL": provider_url, "DEEPSEEK_API_KEY": LOOPBACK_KEY})
        workspace = self.root / "worker-cwd"
        subprocess.run(["git", "init", "-q", str(workspace)], env=self.env,
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)
        base.write_json(self.root / "harness.json", {
            "binary": pi, "resolvedBinary": str(Path(pi).resolve()), "version": version.stdout.strip(),
            "providerUrl": provider_url, "model": f"{LOOPBACK_PROVIDER}/{LOOPBACK_MODEL}",
            "cwd": str(workspace), "credentials": "synthetic only",
        })
        try:
            # This immutable old fixture includes a transfer-receive row. Its
            # public lifecycle.start remains forbidden; no Docker is involved.
            base.seed(self.root)
            first = self.start()
            # The migration fixture's dummy grant has no backing credential.
            # Remove it through CLI so M2 can reach actual Pi startup instead
            # of failing incidentally in secret resolution.
            self.cli("agent", "secret", "revoke", "hosted", "synthetic-grant")
            refused = request(self.socket, "lifecycle.start", {
                "name": "hosted", "provider": "pi", "headless": True,
                "pinnedOwner": "visitor@example.test", "cwd": str(self.root),
                "command": [pi], "modelProvider": LOOPBACK_PROVIDER, "model": LOOPBACK_MODEL,
            }, ok=False)
            assert refused["code"] == "INVALID_ARGUMENT", refused
            assert "belongs to foreign owner 'visitor@example.test'" in refused["message"], refused
            assert "use transfer receive" in refused["message"], refused
            base.write_json(self.root / "decision-a-refusal.json", refused)
            driver = self.cli("agent", "create", "--name", "driver")["agent"]["uri"]
            assert driver == f"agent:host-owner:host-node:{base.agent_id_for(base.token_of(self.root, 'driver'))}", driver
            # Hard cut (send links): driver -> hosted worker is agent-to-agent,
            # so this node needs a writable org space before the rounds.
            self.ensure_org_space("driver", prefix="hosted-start")
            created = self.cli("agent", "create", "--name", "worker")["agent"]
            worker_uri = created["uri"]
            expected = worker_uri
            assert created["agentId"] == base.agent_id_for(base.token_of(self.root, "worker")), created
            assert worker_uri == f"agent:host-owner:host-node:{created['agentId']}", created
            self.cli("agent", "grant", "worker", "--capability", "isolation", "--scope", "directory")
            self.cli("agent", "grant", "worker", "--capability", "send-to",
                     "--scope", json.dumps([driver]))
            request(self.socket, "agent.secret.provider-write", {
                "entryId": "at03-loopback", "fieldName": "apiKey", "value": LOOPBACK_KEY,
            })
            request(self.socket, "agent.secret.grant", {
                "actor": "worker", "grantId": "at03-loopback", "source": "user-provider",
                "entryId": "at03-loopback", "fieldName": "apiKey",
                "environmentNames": ["DEEPSEEK_API_KEY"], "revision": 1,
            })
            # An explicit L0 record is an audit fact, not enforced isolation.
            # entity_token is deliberately absent from public CLI responses.
            with sqlite3.connect(self.root / "state/agents.sqlite3") as db:
                token = db.execute("SELECT entity_token FROM agents WHERE actor='worker'").fetchone()[0]
            assert token
            identity = (created["owner"], worker_uri, token, None)

            def check_identity(turn):
                with sqlite3.connect(self.root / "state/agents.sqlite3") as db:
                    row = db.execute("SELECT owner,uri,entity_token,hosted_by FROM agents WHERE actor='worker'").fetchone()
                assert row == identity, (row, identity)
                ps = request(self.root / "state/daemon.sock", "ps", {})
                target = next(x for x in self.cli("message", "targets")["targets"] if x["targetUri"] == expected)
                assert target["hostedBy"] is None and target["status"] != "offline", target
                actor = next(x for x in ps["agents"] if x["uri"] == expected)
                assert actor["hostedBy"] is None and actor["status"] != "offline", actor
                base.write_json(self.root / f"identity-{turn}.json", {"row": row, "target": target, "psAgent": actor})
            answer = self.cli("agent", "start", "pi", "--name", "worker", "--headless", "--cwd", str(workspace),
                              "--provider", LOOPBACK_PROVIDER, "--model", LOOPBACK_MODEL, "--approve")
            assert answer["actor"] == expected, f"start actor differs from created worker URI: {answer}"
            for turn in range(2):
                if turn:
                    self.stop()
                    second = self.start()
                    assert second["epoch"] != first["epoch"], (first, second)
                    base.write_json(self.root / "isolated-restart.json", {"before": first, "after": second})
                check_identity(turn)
                round_nonce = "AT03_" + uuid.uuid4().hex
                nonce = round_nonce
                previous_requests = len(requests)
                self.cli(
                    "message", "send", "--from", "driver", "--to", expected,
                    "--summary", nonce, nonce,
                )
                deadline = time.monotonic() + 45
                messages = []
                while time.monotonic() < deadline:
                    result = request(self.socket, "message.pending.list", {"actor": driver})
                    messages = result.get("messages", [])
                    if any(nonce in json.dumps(m) for m in messages):
                        break
                    time.sleep(0.2)
                base.write_json(self.root / f"received-{turn}.json", messages)
                assert nonce in json.dumps(requests[previous_requests:]), "round did not reach loopback model"
                assert any(nonce in json.dumps(m) and expected in json.dumps(m) for m in messages), messages
            self.after_rounds()
        finally:
            self.stop()
            server.shutdown()
            server.server_close()

    def after_rounds(self):
        self.cli("agent", "stop", "worker")


if __name__ == "__main__":
    raise SystemExit(base.main(HostedPiScenario))
