"""Synthetic validator tests only; no VM, credential, or model acceptance."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.fixtures.smolvm.smolvm_model_evidence import (
    assert_model_journey,
    install_model_observers,
    scan_evidence,
    model_authorization,
    read_authorized_key,
)


def test_explicit_model_permission_and_private_credential(tmp_path):
    source = tmp_path / "auth.json"
    source.write_text(json.dumps({"deepseek": {"type": "api_key", "key": "synthetic"}}))
    source.chmod(0o600)
    permit = tmp_path / "permission.json"
    data = {
        "actor": "agent:test:isolated:probe",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "endpoint": "https://api.deepseek.com/",
        "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "allowMaterialize": True,
        "approvalRef": "synthetic-operator-approval",
        "credentialFile": str(source),
    }
    permit.write_text(json.dumps(data))
    assert read_authorized_key(model_authorization(permit)) == "synthetic"
    source.chmod(0o644)
    with pytest.raises(ValueError, match="private regular"):
        read_authorized_key(data)
    source.chmod(0o600)
    alias = tmp_path / "link"
    alias.symlink_to(source)
    with pytest.raises(OSError):
        read_authorized_key({**data, "credentialFile": str(alias)})
    data["expiresAt"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    permit.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="expired"):
        model_authorization(permit)


def journey():
    bindings = [
        {
            "actor": "agent:test:isolated:probe",
            "sessionRef": f"worker-{i}",
            "daemonEpoch": f"epoch-{i}",
        }
        for i in (1, 2)
    ]
    identity = {
        **bindings[0],
        "nodeId": "isolated",
        "ok": True,
        "sessionRegistered": True,
    }
    tool = {
        "id": "tool-1",
        "type": "mcpToolCall",
        "server": "harness-bridge",
        "tool": "harness_whoami",
        "status": "completed",
        "result": {"content": [{"type": "text", "text": json.dumps(identity)}]},
    }

    def notification(method, time, turn):
        return {
            "kind": "notification",
            "method": method,
            "time": time,
            "params": {
                "threadId": "native-thread",
                "turn": {"id": turn, "status": "completed"},
            },
        }

    first = [
        {
            "kind": "protocol",
            "method": "thread/start",
            "time": 1,
            "ok": True,
            "returnedThreadId": "native-thread",
        },
        {
            "kind": "protocol",
            "method": "turn/start",
            "time": 2,
            "ok": True,
            "threadId": "native-thread",
            "input": [{"type": "text", "text": "[delivery]\nFirst prompt"}],
        },
        notification("turn/started", 2.1, "turn-1"),
        {
            "kind": "notification",
            "method": "item/started",
            "time": 3,
            "params": {"threadId": "native-thread", "turnId": "turn-1", "item": tool},
        },
        {
            "kind": "worker-whoami",
            "startedAt": 3.1,
            "time": 3.2,
            "binding": bindings[0],
            "result": identity,
        },
        {
            "kind": "notification",
            "method": "item/completed",
            "time": 4,
            "params": {"threadId": "native-thread", "turnId": "turn-1", "item": tool},
        },
        notification("turn/completed", 5, "turn-1"),
    ]
    second = [
        {
            "kind": "protocol",
            "method": "thread/resume",
            "time": 10,
            "ok": True,
            "threadId": "native-thread",
            "returnedThreadId": "native-thread",
        },
        {
            "kind": "protocol",
            "method": "turn/start",
            "time": 11,
            "ok": True,
            "threadId": "native-thread",
            "input": [{"type": "text", "text": "[delivery]\nSecond prompt"}],
        },
        notification("turn/started", 11.1, "turn-2"),
        {
            "method": "item/completed",
            "time": 11.5,
            "params": {
                "threadId": "native-thread",
                "turnId": "turn-2",
                "item": {"type": "agentMessage", "text": "AT08-SAME-THREAD"},
            },
        },
        notification("turn/completed", 12, "turn-2"),
    ]
    return first, second, bindings


def check(first, second, bindings, messages=None):
    return assert_model_journey(
        first,
        second,
        session_ref="native-thread",
        bindings=bindings,
        node_id="isolated",
        messages=messages or [
            {"messageId": "message-1", "operationId": "send-1", "prompt": "First prompt"},
            {"messageId": "message-2", "operationId": "send-2", "prompt": "Second prompt"},
        ],
    )


def test_protocol_and_both_whoami_sources_are_required():
    first, second, bindings = journey()
    result = check(first, second, bindings)
    assert result["turnIds"] == ["turn-1", "turn-2"]
    # Channel session refs are distinct from the native thread being resumed.
    assert bindings[0]["sessionRef"] != result["threadId"]
    for corrupt in (
        "extra-thread",
        "wrong-thread",
        "missing-daemon",
        "late-daemon",
        "wrong-identity",
        "tool-only-in-text",
    ):
        a, b, bound = copy.deepcopy((first, second, bindings))
        if corrupt == "extra-thread":
            b.append(a[0])
        elif corrupt == "wrong-thread":
            b[2]["params"]["threadId"] = "fresh-thread"
        elif corrupt == "missing-daemon":
            a.pop(4)
        elif corrupt == "late-daemon":
            a[4]["time"] = 8
        elif corrupt == "wrong-identity":
            a[4]["result"]["sessionRef"] = "some-other-worker"
        else:
            a[5]["params"]["item"]["type"] = "agentMessage"
        with pytest.raises(AssertionError):
            check(a, b, bound)


def test_second_turn_cannot_pass_by_replaying_first_delivery():
    first, second, bindings = journey()
    messages = [
        {"messageId": "message-1", "operationId": "send-1", "prompt": "First prompt"},
        {"messageId": "message-2", "operationId": "send-2", "prompt": "Second prompt"},
    ]
    for key in ("messageId", "operationId"):
        duplicate = copy.deepcopy(messages)
        duplicate[1][key] = duplicate[0][key]
        with pytest.raises(AssertionError, match="deduplicated"):
            check(first, second, bindings, duplicate)
    replay = copy.deepcopy(second)
    replay[1]["input"] = first[1]["input"]
    with pytest.raises(AssertionError, match="generation's prompt"):
        check(first, replay, bindings)
    second[3]["params"]["item"]["text"] = "unrelated answer"
    with pytest.raises(AssertionError, match="remembered marker"):
        check(first, second, bindings)


def test_scan_detects_split_secret_and_never_reads_link_targets(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    synthetic = b"synthetic-test-value"
    external = tmp_path / "input-secret"
    external.write_bytes(synthetic)
    (evidence / "external-link").symlink_to(external)
    (evidence / "external-directory").symlink_to(tmp_path, target_is_directory=True)
    log = evidence / "stdout.log"
    log.write_bytes(b"clean output")
    assert scan_evidence(evidence, synthetic) == {
        "zeroMatches": True,
        "filesScanned": 1,
        "linksNotFollowed": ["external-directory", "external-link"],
    }
    log.write_bytes(b"x" * 65530 + synthetic + b"tail")
    result = scan_evidence(evidence, synthetic)
    assert result["zeroMatches"] is False and result["filesScanned"] == 1
    assert synthetic.decode() not in json.dumps(result)
    with pytest.raises(ValueError):
        scan_evidence(evidence, b"")
    os.mkfifo(evidence / "not-a-regular-file")
    with pytest.raises(ValueError, match="Non-regular"):
        scan_evidence(evidence, synthetic)


def test_observers_preserve_real_dispatch_and_queue(monkeypatch):
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient

    first, _, _ = journey()
    recorded, calls = [], []

    async def reader(client):
        await client._notifications.put({"method": "turn/started", "params": {}})
        return "reader-result"

    def handler(app, method, params):
        calls.append((method, params))
        return first[4]["result"]

    monkeypatch.setattr(CodexAppServerClient, "_read_loop", reader)
    monkeypatch.setattr(DaemonApplication, "handle", handler)
    install_model_observers(recorded.append)

    async def observe():
        queue = asyncio.Queue()
        await queue.put("preexisting")
        put = queue.put
        assert (
            await CodexAppServerClient._read_loop(SimpleNamespace(_notifications=queue))
            == "reader-result"
        )
        assert queue.put == put
        assert await queue.get() == "preexisting"
        assert (await queue.get())["method"] == "turn/started"

    asyncio.run(observe())
    params = {**first[4]["binding"], "request": {"method": "identity.whoami"}}
    answer = DaemonApplication.handle(None, "worker.channel.request", params)
    assert answer is first[4]["result"] and calls == [
        ("worker.channel.request", params)
    ]
    assert [e["kind"] for e in recorded] == ["notification", "worker-whoami"]
