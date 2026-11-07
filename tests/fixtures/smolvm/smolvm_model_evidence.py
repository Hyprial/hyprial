"""Evidence checks for the existing managed VM fixture, never a model runner.

Synthetic checks of these helpers do not establish D2/D3/D5 acceptance. The
real journey still requires the separately approved actor and test grant.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import time
from datetime import datetime, timezone


def model_authorization(path: Path):
    """Explicit operator test permission, not a product TTL implementation."""
    from hyprial.kernel import parse_agent_uri

    data = json.loads(path.read_text())
    actor = parse_agent_uri(data["actor"])
    expiry = datetime.fromisoformat(data["expiresAt"])
    if (
        actor is None
        or data.get("provider") != "deepseek"
        or data.get("model") != "deepseek-flash"
        or data.get("endpoint") != "https://api.deepseek.com/"
        or data.get("allowMaterialize") is not True
        or not data.get("approvalRef")
        or expiry.tzinfo is None
        or expiry <= datetime.now(timezone.utc)
    ):
        raise ValueError("Missing, expired or unsupported model test authorization")
    return data


def read_authorized_key(authorization):
    """Read only the explicitly selected DeepSeek API entry; never log a value."""
    path = Path(authorization["credentialFile"])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Test credential must be an owned private regular file")
        document = json.load(stream)
    value = document.get("deepseek", {}).get("key")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Authorized DeepSeek entry is absent")
    return value.strip()


def install_model_observers(emit):
    """Observe real notifications and the real handler without replacing either."""
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient

    original_loop = CodexAppServerClient._read_loop
    original_handle = DaemonApplication.handle

    async def observed_loop(client):
        queue = client._notifications
        original_put = queue.put

        async def observed_put(message):
            if isinstance(message, dict):
                method, params = message.get("method"), message.get("params", {})
                item = params.get("item", {}) if isinstance(params, dict) else {}
                item = item if isinstance(item, dict) else {}
                if method in {"turn/started", "turn/completed"} or (
                    method in {"item/started", "item/completed"}
                    and item.get("type") in {"mcpToolCall", "agentMessage"}
                ):
                    emit({"kind": "notification", "method": method, "params": params})
            await original_put(message)

        # Preserve queued events and waiters, and restore the original method
        # when the reader exits. A reconnect must not stack observation wrappers.
        queue.put = observed_put
        try:
            return await original_loop(client)
        finally:
            queue.put = original_put

    def observed_handle(app, method, params, **kwargs):
        started = time.time()
        result = original_handle(app, method, params, **kwargs)
        if (
            method == "worker.channel.request"
            and params["request"].get("method") == "identity.whoami"
        ):
            emit(
                {
                    "kind": "worker-whoami",
                    "startedAt": started,
                    "binding": {
                        key: params[key]
                        for key in ("actor", "sessionRef", "daemonEpoch")
                    },
                    "result": result,
                }
            )
        return result

    CodexAppServerClient._read_loop = observed_loop
    DaemonApplication.handle = observed_handle


def _identity(result):
    assert isinstance(result, dict) and not result.get("isError"), "MCP result failed"
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    values = []
    for content in result.get("content", []):
        if content.get("type") == "text":
            try:
                value = json.loads(content["text"])
            except (KeyError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and "actor" in value:
                values.append(value)
    assert len(values) == 1, "Expected one structured whoami result"
    return values[0]


def assert_model_journey(first, second, *, session_ref, bindings, node_id, messages):
    """D2/D3: two real turns, one native thread, and two-sided whoami proof."""
    assert session_ref and len(bindings) == 2
    assert bindings[0]["actor"] == bindings[1]["actor"]
    assert bindings[0]["daemonEpoch"] != bindings[1]["daemonEpoch"]
    assert len(messages) == 2
    for key in ("messageId", "operationId"):
        assert all(message.get(key) for message in messages)
        assert messages[0][key] != messages[1][key], "Second send was deduplicated"
    all_events = first + second
    starts = [e for e in all_events if e.get("method") == "thread/start"]
    resumes = [e for e in all_events if e.get("method") == "thread/resume"]
    assert len(starts) == 1 and starts[0] in first, "Extra or missing thread/start"
    assert starts[0].get("ok") and starts[0].get("returnedThreadId") == session_ref
    assert len(resumes) == 1 and resumes[0] in second, "Expected one strict resume"
    assert resumes[0].get("ok") and resumes[0].get("threadId") == session_ref
    assert resumes[0].get("returnedThreadId") == session_ref, "Resume changed thread"
    turn_ids = []
    for sequence, message in zip((first, second), messages, strict=True):
        requests = [e for e in sequence if e.get("method") == "turn/start"]
        begun = [e for e in sequence if e.get("method") == "turn/started"]
        ended = [e for e in sequence if e.get("method") == "turn/completed"]
        assert len(requests) == len(begun) == len(ended) == 1, (
            "Expected one turn per epoch"
        )
        assert requests[0].get("ok") and requests[0].get("threadId") == session_ref
        texts = [item.get("text", "") for item in requests[0].get("input", []) if item.get("type") == "text"]
        assert any(text.endswith("\n" + message["prompt"]) for text in texts), (
            "Native turn did not receive this generation's prompt"
        )
        for event in (begun[0], ended[0]):
            assert event["params"]["threadId"] == session_ref, "Turn changed thread"
        turn_id = begun[0]["params"]["turn"]["id"]
        assert turn_id and ended[0]["params"]["turn"]["id"] == turn_id
        assert ended[0]["params"]["turn"]["status"] == "completed"
        turn_ids.append(turn_id)
    assert turn_ids[0] != turn_ids[1], "Second turn reused first turn evidence"
    assert resumes[0]["time"] < next(
        e["time"] for e in second if e.get("method") == "turn/start"
    ), "Second turn preceded successful resume"
    assert any(
        event.get("method") == "item/completed"
        and event.get("params", {}).get("threadId") == session_ref
        and event["params"].get("turnId") == turn_ids[1]
        and event["params"].get("item", {}).get("type") == "agentMessage"
        and "AT08-SAME-THREAD" in event["params"]["item"].get("text", "")
        for event in second
    ), "Second turn did not answer with the remembered marker"

    matches = []
    for end in first:
        params = end.get("params", {})
        item = params.get("item", {})
        if not (
            end.get("method") == "item/completed"
            and item.get("type") == "mcpToolCall"
            and item.get("server") == "harness-bridge"
            and item.get("tool") == "harness_whoami"
        ):
            continue
        assert item.get("status") == "completed", "Model whoami did not complete"
        assert (
            params.get("threadId") == session_ref
            and params.get("turnId") == turn_ids[0]
        )
        identity = _identity(item.get("result"))
        for key, value in bindings[0].items():
            assert identity.get(key) == value, "Whoami binding mismatch"
        assert (
            identity.get("nodeId") == node_id
            and identity.get("sessionRegistered") is True
        )
        opened = [
            e
            for e in first
            if e.get("method") == "item/started"
            and e.get("params", {}).get("item", {}).get("id") == item.get("id")
        ]
        assert len(opened) == 1 and item.get("id"), "Missing tool start evidence"
        calls = [
            e
            for e in first
            if e.get("kind") == "worker-whoami"
            and e.get("binding") == bindings[0]
            and e.get("result") == identity
            and opened[0]["time"] <= e["startedAt"] <= e["time"] <= end["time"]
        ]
        assert calls, "Missing same-window daemon whoami evidence"
        matches.append(item["id"])
    assert matches, "Missing model-initiated MCP whoami evidence"
    return {"threadId": session_ref, "turnIds": turn_ids, "whoamiItems": matches}


def scan_evidence(root: Path, secret: bytes):
    """D5: scan every regular file, without following links or exposing the key.

    Call only after authorized private credential material has been removed
    from the evidence tree. The input credential belongs outside that tree.
    Any unreadable or non-regular entry refuses a successful scan result.
    """
    if not secret or not stat.S_ISDIR(root.lstat().st_mode):
        raise ValueError(
            "A nonempty in-memory secret and real evidence directory are required"
        )
    count, zero, links = 0, True, []

    def fail(error):
        raise error

    for directory, dirs, files, directory_fd in os.fwalk(
        root, onerror=fail, follow_symlinks=False
    ):
        for name in (*dirs, *files):
            mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                links.append(str((Path(directory) / name).relative_to(root)))
                continue
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError("Non-regular evidence cannot be silently skipped")
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Evidence changed during scan")
                tail = b""
                while chunk := stream.read(65536):
                    window = tail + chunk
                    zero = (secret not in window) and zero
                    tail = window[-(len(secret) - 1) :] if len(secret) > 1 else b""
            count += 1
    return {
        "zeroMatches": zero,
        "filesScanned": count,
        "linksNotFollowed": sorted(links),
    }
