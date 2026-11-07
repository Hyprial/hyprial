"""Opt-in real VM / real daemon IPC; the client is synthetic, not a harness."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from hyprial.kernel.impl.contracts.channels.worker_channel import GUEST_WORKER_SOCKET
from hyprial.daemon.impl.transfer.execution.smolvm import (
    _cleanup,
    _host_canaries,
    _private_environment,
    _regular_read,
    _run,
    _snapshot,
    _succeeded,
    _validate_paths,
)
from tests.daemon.workers.test_worker_relay import ACTOR, OTHER, frame, isolated, message  # noqa: F401


def test_real_smolvm_channel(isolated):  # noqa: F811 - imported pytest fixture
    required = [
        "AT08_SMOLVM",
        "AT08_ROOTFS",
        "AT08_RESIZE2FS",
        "AT08_CLIENT",
        "AT08_OUTPUT",
    ]
    if not all(os.environ.get(key) for key in required):
        pytest.skip(
            "Real VM channel NOT_RUN; use scripts/verify/channel-probe.py under machine lock"
        )
    binary, rootfs, resize2fs, client, output = [
        Path(os.environ[key]) for key in required
    ]
    binary, rootfs, output, resize2fs = _validate_paths(
        binary, rootfs, output, resize2fs
    )
    assert client.is_file() and not client.is_symlink()
    _, app, relay, binding = isolated
    app._inbox.receive(message(ACTOR, "for-alice"))
    app._inbox.receive(message(OTHER, "for-bob"))
    before = {
        "rootfs": _snapshot(rootfs),
        "runtime": _snapshot(binary.parent),
        "host": _host_canaries(),
    }
    output.mkdir(mode=0o700)
    env = _private_environment(output)
    report = {
        "ok": False,
        "kind": "synthetic-smolvm-worker-channel",
        "schema_version": 1,
        "actor": ACTOR,
        "checks": {
            "channel": "NOT_RUN",
            "worker": "NOT_RUN",
            "authentication": "NOT_RUN",
            "sandbox": "NOT_RUN",
            "strict_resume": "NOT_RUN",
            "cross_machine": "NOT_RUN",
        },
        "limitations": "Synthetic Unix client and supervisor projection; real daemon IPC/SQLite only. No model or harness. targets visibility unchanged (AT09).",
    }

    def run(args, label, timeout=120):
        result = _run([str(binary), *args], output, label, env, timeout)
        assert _succeeded(result), (label, result)
        return _regular_read(output / (label + ".stdout"), 1024 * 1024)

    try:
        shutil.copytree(
            binary.parent / "agent-rootfs",
            output / "engine/agent-rootfs",
            symlinks=True,
        )
        env["SMOLVM_AGENT_ROOTFS"] = str(output / "engine/agent-rootfs")
        tools = output / "engine/bin"
        tools.mkdir()
        (tools / "resize2fs").symlink_to(resize2fs)
        env["PATH"] = str(tools) + ":" + env["PATH"]
        assert run(["--version"], "version", 10).strip() == b"smolvm 1.19.0"
        fixture = output / "fixture"
        fixture.mkdir(mode=0o700)
        shutil.copyfile(client, fixture / "client")
        (fixture / "client").chmod(0o700)
        report["client"] = _snapshot(fixture / "client")
        canary = output / "host-secret"
        canary.write_text("SYNTHETIC-UNMOUNTED-SECRET")
        requests = [
            frame(),
            frame("message.pending.list", {"fetched": True}),
            frame("identity.whoami", {"actor": OTHER}),
            frame("identity.whoami", {"sessionRef": "bob-session"}),
            frame(
                "message.send",
                {"to": [OTHER], "message": "x", "resourcePaths": [str(canary)]},
            ),
            frame("lifecycle.start", {}),
            frame("orgfs.read", {"node": str(canary)}),
        ]
        (fixture / "requests.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in requests)
        )
        run(
            [
                "machine",
                "create",
                "--name",
                "channel-probe",
                "--image",
                str(rootfs),
                "--cpus",
                "2",
                "--mem",
                "1024",
                "--storage",
                "1",
                "--overlay",
                "1",
                "--volume",
                f"{fixture}:/probe:ro",
                "--mount-socket",
                f"{relay.socket_path}:{GUEST_WORKER_SOCKET}",
            ],
            "create",
        )
        run(["machine", "start", "--name", "channel-probe"], "start")
        command = [
            "machine",
            "exec",
            "--name",
            "channel-probe",
            "--stream",
            "--timeout",
            "20s",
            "--",
            "/probe/client",
            GUEST_WORKER_SOCKET,
            "/probe/requests.jsonl",
        ]
        raw = run(command, "guest")
        responses = [json.loads(line) for line in raw.splitlines()]
        assert len(responses) == len(requests)
        assert responses[0]["result"]["actor"] == ACTOR
        assert [m["messageId"] for m in responses[1]["result"]["messages"]] == [
            "for-alice"
        ]
        assert [r["error"]["code"] for r in responses[2:]] == [
            "CALLER_NOT_AUTHORIZED",
            "CALLER_NOT_AUTHORIZED",
            "INVALID_ARGUMENT",
            "METHOD_NOT_FOUND",
            "METHOD_NOT_FOUND",
        ]
        assert b"SYNTHETIC-UNMOUNTED-SECRET" not in raw
        assert app._inbox.outbox_count() == 0
        # Change only this isolated daemon's epoch, then exercise the SAME live
        # socket from the SAME VM. A relay must not silently renew its binding.
        app.epoch = "replacement-fixture-epoch"
        expired = [
            json.loads(line) for line in run(command, "guest-expired").splitlines()
        ]
        assert (
            expired[0]["error"]["code"]
            == expired[1]["error"]["code"]
            == "STALE_SESSION"
        )
        report["checks"]["channel"] = "PASS"
    finally:
        report["cleanup"] = _cleanup(binary, output, env)
        after = {
            "rootfs": _snapshot(rootfs),
            "runtime": _snapshot(binary.parent),
            "host": _host_canaries(),
        }
        for key in before:
            report["checks"][key] = "PASS" if before[key] == after[key] else "FAIL"
        (output / "before.json").write_text(json.dumps(before, sort_keys=True))
        (output / "after.json").write_text(json.dumps(after, sort_keys=True))
        report["ok"] = (
            report["cleanup"]["ok"]
            and report["checks"]["channel"] == "PASS"
            and before == after
        )
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    assert report["ok"], report
