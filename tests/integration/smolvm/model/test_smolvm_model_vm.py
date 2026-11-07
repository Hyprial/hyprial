"""Opt-in two-turn model journey on the existing isolated managed VM fixture."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from hyprial.kernel import SmolvmRuntimeSpec
from hyprial.daemon.impl.harnesses.smolvm_runtime import runtime_directory, tree_digest
from hyprial.daemon.impl.transfer.execution.smolvm import _host_canaries
from hyprial.kernel import parse_agent_uri
from tests.fixtures.smolvm.smolvm_daemon import (
    emergency_cleanup,
    events,
    health_snapshot,
    live,
    production_identity,
    production_unchanged,
    private_daemon_environment,
    start_isolated_daemon,
    start_managed_worker,
    stop_isolated_daemons,
    wait_for,
    wait_for_session_ref,
)
from tests.fixtures.smolvm.smolvm_model_evidence import (
    assert_model_journey,
    model_authorization,
    read_authorized_key,
    scan_evidence,
)


def test_authorized_model_and_same_thread_resume():
    required = (
        "AT08_MODEL_AUTHORIZATION",
        "AT08_MODEL_OUTPUT",
        "AT08_RUNTIME_SPEC",
        "AT08_HEALTH_SOCKET",
    )
    if not all(os.environ.get(name) for name in required):
        pytest.skip(
            "Real model NOT_RUN: operator permission, private key, VM window and machine lock required"
        )
    authorization_path = Path(os.environ["AT08_MODEL_AUTHORIZATION"]).resolve(
        strict=True
    )
    authorization = model_authorization(authorization_path)
    actor = authorization["actor"]
    _, node, name = parse_agent_uri(actor)
    root = Path(os.environ["AT08_MODEL_OUTPUT"]).resolve()
    root.mkdir(mode=0o700)  # never resume a partly billed/failed test run
    if Path(authorization["credentialFile"]).resolve().is_relative_to(root):
        raise ValueError("Input credential must be outside the evidence directory")
    spec_path = Path(os.environ["AT08_RUNTIME_SPEC"]).resolve(strict=True)
    spec = SmolvmRuntimeSpec.from_json(json.loads(spec_path.read_text()))
    repo = Path(__file__).resolve().parents[4]
    env = private_daemon_environment(root, repo)
    # Explicit model opt-in removes the synthetic loopback endpoint. All other
    # environment/state/mesh isolation is shared with the no-credential runner.
    (root / "home/settings.json").write_text("{}\n")
    env["AT08_MODEL_AUTHORIZATION"] = str(authorization_path)
    state = root / "home/state"
    runtime = runtime_directory(state, actor)
    assert not runtime.exists(), "Do not adopt another run's private VM"
    processes, streams, bindings, managers = [], [], [], []
    report = {"ok": False, "actor": actor, "approvalRef": authorization["approvalRef"]}
    before_host = _host_canaries()
    before_health = health_snapshot(
        Path(os.environ["AT08_HEALTH_SOCKET"]), root, "production-before"
    )
    secret = read_authorized_key(authorization).encode()

    from tests.daemon.workers.test_worker_relay import request, frame

    def ipc(method, params=None, *, request_id=None):
        response = request(
            state / "daemon.sock",
            frame(method, params, request_id=request_id or str(uuid4())),
        )
        assert "error" not in response, f"Isolated IPC rejected {method}"
        return response["result"]

    def collect(generation):
        observed = events(root, generation)
        managers.extend(
            pid
            for e in observed
            for pid in e.get("managerPids", [])
            if pid not in managers
        )
        return observed

    def native(generation, method):
        found = wait_for(
            lambda: next(
                (e for e in collect(generation) if e.get("method") == method), None
            ),
            f"{generation} {method}",
            timeout=90,
        )
        assert found.get("ok"), f"Native {method} failed; see protocol evidence"
        bindings.append(found["binding"])
        return found["returnedThreadId"]

    def send_turn(generation, prompt):
        operation_id = f"model-{generation}-{uuid4()}"
        result = ipc(
            "message.send",
            {"actor": "model-test-driver", "to": [actor], "message": prompt},
            request_id=operation_id,
        )
        (root / f"message-{generation}.json").write_text(json.dumps(result, indent=2))
        assert result["operationId"] == operation_id
        assert result["messageId"]
        previous = report.setdefault("messages", [])
        assert all(result["messageId"] != item["messageId"] for item in previous)
        previous.append({**result, "prompt": prompt})
        completed = wait_for(
            lambda: next(
                (e for e in collect(generation) if e.get("method") == "turn/completed"),
                None,
            ),
            f"{generation} model turn completion",
            timeout=180,
        )
        assert completed["params"]["turn"]["status"] == "completed"
        # RECIPIENT_COMMIT only proves inbox admission. Explicitly settle this
        # completed turn and verify consumption before stopping its daemon, so
        # the next generation cannot pass by replaying the first message.
        ack = ipc("message.ack", {"actor": actor, "messageId": result["messageId"]})
        assert ack["ok"] and ack["acknowledged"]

        def consumed():
            with closing(sqlite3.connect(f"file:{state / 'inbox.sqlite3'}?mode=ro", uri=True)) as db:
                row = db.execute(
                    "SELECT consumed FROM inbox WHERE message_id=? AND recipient=?",
                    (result["messageId"], actor),
                ).fetchone()
            return row is not None and row[0] == 1

        wait_for(consumed, f"{generation} message consumed before daemon stop")
        report.setdefault("settlements", []).append({**ack, "consumed": True})

    try:
        first, report["first"] = start_isolated_daemon(
            root, repo, "first", env, processes, streams
        )
        cli = start_managed_worker(root, repo, env, spec_path, name=name)
        assert cli.returncode == 0, "Public start failed; see CLI evidence"
        thread = native("first", "thread/start")
        ipc("agent.create", {"name": "model-test-driver"})
        send_turn(
            "first",
            "This is an authorized, minimal integration test. Call the harness-bridge harness_whoami MCP tool exactly once, then reply with the returned actor. Remember the marker AT08-SAME-THREAD. Do not call any other tool, read files, change files, or send messages.",
        )
        report["persistedBeforeStop"] = wait_for_session_ref(state, thread, name=name)
        first.terminate()
        report["firstExitCode"] = first.wait(timeout=60)
        assert report["firstExitCode"] == 0
        collect("first")
        report["firstCleanup"] = {
            "liveManagers": [pid for pid in managers if live(pid)],
            "ownerExists": (runtime / "owner.json").exists(),
            "pointerExists": (state / "smolvm-runtimes" / runtime.name).exists(),
            "leftovers": sorted(
                str(p.relative_to(runtime)) for p in runtime.rglob("*")
            ),
        }
        assert report["firstCleanup"] == {
            "liveManagers": [],
            "ownerExists": False,
            "pointerExists": False,
            "leftovers": ["runtime.lock"],
        }
        second, report["second"] = start_isolated_daemon(
            root, repo, "second", env, processes, streams
        )
        assert report["second"]["epoch"] != report["first"]["epoch"]
        assert native("second", "thread/resume") == thread
        send_turn(
            "second",
            "Continue the previous conversation. Reply with the marker from the previous turn. Do not call tools, read files, change files, or send messages.",
        )
        report["protocol"] = assert_model_journey(
            collect("first"),
            collect("second"),
            session_ref=thread,
            bindings=bindings,
            node_id=node,
            messages=report["messages"],
        )
        report["persistedAfterResume"] = wait_for_session_ref(state, thread, name=name)
        report["grantRevoked"] = ipc(
            "agent.secret.revoke", {"actor": name, "grantId": "authorized-model"}
        )
        second.terminate()
        report["secondExitCode"] = second.wait(timeout=60)
        assert report["secondExitCode"] == 0
        report["ok"] = True
    finally:
        stop_isolated_daemons(processes, streams)
        for generation in ("first", "second"):
            collect(generation)
        try:
            report["emergencyCleanup"] = emergency_cleanup(
                runtime, state, spec, actor=actor
            )
        except Exception as error:
            # Failed engine cleanup must not prevent credential removal/scanning.
            report["emergencyCleanup"] = True
            report["cleanupErrorType"] = type(error).__name__
        # Remove only the explicitly materialized test secret; preserve original
        # operator credentials and the native session used as resume evidence.
        location = root / "credential-location.json"
        if location.exists():
            private_secret = Path(json.loads(location.read_text())["path"])
            assert (
                private_secret.is_relative_to(root / "home")
                and not private_secret.is_symlink()
            )
            private_secret.unlink(missing_ok=True)
            report["materializedCredentialRemoved"] = not private_secret.exists()
        report["liveManagersAfterShutdown"] = [pid for pid in managers if live(pid)]
        report["runtimeLeftovers"] = sorted(
            str(p.relative_to(runtime)) for p in runtime.rglob("*")
        )
        report["pointerExists"] = (state / "smolvm-runtimes" / runtime.name).exists()
        after_health = health_snapshot(
            Path(os.environ["AT08_HEALTH_SOCKET"]), root, "production-after"
        )
        report["productionIdentity"] = production_identity(before_health, after_health)
        report["productionUnchanged"] = production_unchanged(
            before_health, after_health
        )
        report["hostCanariesUnchanged"] = before_host == _host_canaries()
        report["rootfsUnchanged"] = (
            tree_digest(Path(spec.rootfs_path)) == spec.rootfs_digest
        )
        report["bundleUnchanged"] = (
            tree_digest(Path(spec.smolvm_path).parent) == spec.smolvm_bundle_digest
        )
        (root / "report.json").write_text(json.dumps(report, indent=2))
        scan = scan_evidence(root, secret)
        del secret
        (root / "secret-scan.json").write_text(json.dumps(scan, indent=2))
        assert scan["zeroMatches"], (
            "Secret found: keep private evidence sealed; do not publish"
        )
    assert not report["emergencyCleanup"] and not report["liveManagersAfterShutdown"]
    assert (
        report["runtimeLeftovers"] == ["runtime.lock"] and not report["pointerExists"]
    )
    assert all(
        report[key]
        for key in (
            "materializedCredentialRemoved",
            "productionUnchanged",
            "hostCanariesUnchanged",
            "rootfsUnchanged",
            "bundleUnchanged",
        )
    )
