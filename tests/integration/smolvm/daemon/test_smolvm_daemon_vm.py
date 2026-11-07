"""Opt-in managed CLI -> daemon -> real VM crash recovery, without model turns."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
from contextlib import closing
import subprocess
import tempfile
import time

import pytest
import anyio

from hyprial.kernel import SmolvmRuntimeSpec
from hyprial.daemon.impl.harnesses.smolvm_runtime import (
    runtime_directory,
    tree_digest,
)
from hyprial.daemon.impl.transfer.execution.smolvm import _host_canaries, _private_environment
from hyprial.daemon.impl.mcp.unix import UnixDaemonConnection
from hyprial.daemon.impl.mcp.api import DaemonRequestRejected
from tests.fixtures.smolvm.smolvm_daemon import (
    events,
    wait_for,
    live,
    emergency_cleanup,
    private_daemon_environment,
    start_isolated_daemon,
    start_managed_worker,
    stop_isolated_daemons,
    health_snapshot,
    production_identity,
    production_unchanged,
    wait_for_session_ref,
)


def test_session_barrier_requires_commit_for_the_target_harness(tmp_path):
    with closing(sqlite3.connect(tmp_path / "lifecycle-operations.sqlite3")) as db:
        db.execute("CREATE TABLE harnesses(harness TEXT, name TEXT, session_ref TEXT)")
        db.executemany(
            "INSERT INTO harnesses VALUES(?,?,?)",
            [
                ("codex", "probe", None),
                ("codex", "other", "native-thread"),
            ],
        )
        db.commit()
        db.execute(
            "UPDATE harnesses SET session_ref='native-thread' WHERE name='probe'"
        )
        # A native callback (or another actor's ref) is insufficient until the
        # real desired-state writer commits. The observer must not write it.
        with pytest.raises(AssertionError, match="committed session_ref"):
            wait_for_session_ref(tmp_path, "native-thread", timeout=0.01)
        db.commit()
        assert wait_for_session_ref(tmp_path, "native-thread")["name"] == "probe"


@pytest.mark.parametrize("mode", ["missing", "revoked", "materials-revoked"])
def test_daemon_rejects_ungranted_provider_before_worker(mode):
    from tests.daemon.workers.test_worker_relay import request, frame

    real_materials = mode == "materials-revoked"
    if real_materials and not all(
        os.environ.get(k)
        for k in ("AT08_RUNTIME_SPEC", "AT08_REJECT_OUTPUT", "AT08_HEALTH_SOCKET")
    ):
        pytest.skip(
            "Pinned VM rejection NOT_RUN: materials, window and machine lock required"
        )
    # Short roots keep the real IPC usable on Darwin as well as Linux CI.
    with tempfile.TemporaryDirectory(prefix="ag-", dir="/tmp") as temporary:
        root = Path(
            os.environ["AT08_REJECT_OUTPUT"] if real_materials else temporary
        ).resolve()
        if real_materials:
            root.mkdir(mode=0o700)
        repo = Path(__file__).resolve().parents[4]
        env = private_daemon_environment(root, repo)
        env["AT08_SYNTHETIC_GRANT"] = "missing" if mode == "missing" else "present"
        # A host key is present deliberately; P2 must not inherit it. All values
        # in these negatives are synthetic and the shared fixture forbids turns.
        env["DEEPSEEK_API_KEY"] = "HOST-SYNTHETIC-NOT-A-CREDENTIAL"
        spec_path = None
        if real_materials:
            spec = SmolvmRuntimeSpec.from_json(
                json.loads(Path(os.environ["AT08_RUNTIME_SPEC"]).read_text())
            )
            spec_path = root / "runtime.json"
            spec_path.write_text(json.dumps(spec.to_json()))
        # CI exercises the shared P2 authorization path without VM material.
        # The opt-in third case selects the actual pinned smolvm configuration.
        processes, streams = [], []
        socket = root / "home/state/daemon.sock"
        runtime = runtime_directory(root / "home/state", "agent:test:isolated:probe")
        assert not runtime.exists()
        before_host = _host_canaries()
        report = {"ok": False, "mode": mode, "model_turn": "NOT_RUN"}
        if real_materials:
            before = health_snapshot(
                Path(os.environ["AT08_HEALTH_SOCKET"]), root, "production-before"
            )
        try:
            start_isolated_daemon(root, repo, "first", env, processes, streams)
            if mode != "missing":
                result = request(
                    socket,
                    frame(
                        "agent.secret.revoke",
                        {
                            "actor": "probe",
                            "grantId": "synthetic-provider",
                        },
                    ),
                )["result"]
                assert result == {"ok": True, "revoked": True}
                report["revocation"] = result
            assert (
                request(socket, frame("agent.secret.list", {"actor": "probe"}))[
                    "result"
                ]["grants"]
                == []
            )
            started = time.monotonic()
            if real_materials:
                cli = start_managed_worker(root, repo, env, spec_path)
                report["cliReturncode"] = cli.returncode
                assert cli.returncode != 0, (
                    "Missing authorization must fail public start"
                )
            else:
                # Exercise the real public lifecycle handler on CI without
                # requiring a host Codex installation. No launcher is replaced;
                # credential refusal must happen before the absent command.
                try:
                    report["start"] = anyio.run(
                        UnixDaemonConnection(socket, timeout=30).request,
                        "lifecycle.start",
                        {
                            "provider": "codex",
                            "name": "probe",
                            "headless": True,
                            "modelProvider": "deepseek",
                            "model": "deepseek-flash",
                            "command": [str(root / "uninstalled-codex")],
                            "cwd": str(root / "workspace"),
                        },
                    )
                except DaemonRequestRejected as error:
                    report["start"] = {"code": error.code, "error": str(error)}
            report["startElapsedSeconds"] = time.monotonic() - started
            assert report["startElapsedSeconds"] < 30
            with closing(
                sqlite3.connect(
                    f"file:{root / 'home/state/lifecycle-operations.sqlite3'}?mode=ro",
                    uri=True,
                )
            ) as db:
                report["lifecycleStates"] = [
                    row[0]
                    for row in db.execute("SELECT state FROM lifecycle_operations")
                ]
            # Public start failed, and its side effects were successfully undone.
            # The journal names that terminal state compensated, not failed.
            assert report["lifecycleStates"] == ["compensated"]
            refusal = json.loads(cli.stdout) if real_materials else report["start"]
            assert "agent-owned DEEPSEEK_API_KEY" in refusal["error"]
            rejection = wait_for(
                lambda: next(
                    (
                        e
                        for e in events(root, "first")
                        if e.get("kind") == "provider-rejected"
                    ),
                    None,
                ),
                "real P2 provider rejection",
            )
            assert rejection["errorType"] == "AgentProviderCredentialMissing"
            assert not rejection["agentKeyPresent"]
            assert not rejection["legacyFallbackAllowed"]
            report["rejection"] = rejection
            report["ok"] = True
        finally:
            stop_isolated_daemons(processes, streams)
            observed = events(root, "first")
            report["adminCalls"] = [e for e in observed if e["kind"] == "admin"]
            report["protocolCalls"] = [e for e in observed if e["kind"] == "protocol"]
            report["runtimeCreated"] = runtime.exists()
            report["hostCanariesUnchanged"] = before_host == _host_canaries()
            if real_materials:
                after_health = health_snapshot(
                    Path(os.environ["AT08_HEALTH_SOCKET"]),
                    root,
                    "production-after",
                )
                report["productionIdentity"] = production_identity(before, after_health)
                report["productionUnchanged"] = production_unchanged(
                    before, after_health
                )
                report["rootfsUnchanged"] = (
                    tree_digest(Path(spec.rootfs_path)) == spec.rootfs_digest
                )
            (root / "report.json").write_text(json.dumps(report, indent=2))
        assert not report["adminCalls"] and not report["protocolCalls"]
        assert not report["runtimeCreated"] and report["hostCanariesUnchanged"]
        if real_materials:
            assert report["productionUnchanged"] and report["rootfsUnchanged"]


def test_real_managed_smolvm_daemon_crash():
    if not all(
        os.environ.get(k)
        for k in ("AT08_RUNTIME_SPEC", "AT08_DAEMON_OUTPUT", "AT08_HEALTH_SOCKET")
    ):
        pytest.skip(
            "Real managed VM NOT_RUN: explicit pinned materials, monitor window and machine lock required"
        )
    root = Path(os.environ["AT08_DAEMON_OUTPUT"]).resolve()
    root.mkdir(mode=0o700)
    home = root / "home"
    repo = Path(__file__).resolve().parents[4]
    spec_path = Path(os.environ["AT08_RUNTIME_SPEC"]).resolve()
    spec = SmolvmRuntimeSpec.from_json(json.loads(spec_path.read_text()))
    env = private_daemon_environment(root, repo)
    runtime = runtime_directory(home / "state", "agent:test:isolated:probe")
    assert not runtime.exists(), (
        "Use a fresh output; never adopt unrelated runtime state"
    )
    report = {
        "ok": False,
        "model_turn": "NOT_RUN",
        "strict_resume": "NOT_RUN",
        "runtime": str(runtime),
    }
    processes, streams, managers = [], [], []
    before_host = _host_canaries()

    def health(label):
        return health_snapshot(
            Path(os.environ["AT08_HEALTH_SOCKET"]), root, "production-" + label
        )

    before_health = health("before")

    def start(generation):
        process, report[generation] = start_isolated_daemon(
            root, repo, generation, env, processes, streams
        )
        return process

    def collect(generation):
        for event in events(root, generation):
            managers.extend(
                pid for pid in event.get("managerPids", []) if pid not in managers
            )
        return events(root, generation)

    def machines(label):
        private_env = _private_environment(runtime, reuse=True)
        private_env["SMOLVM_AGENT_ROOTFS"] = str(runtime / "engine/agent-rootfs")
        result = subprocess.run(
            [spec.smolvm_path, "machine", "ls", "--quiet"],
            env=private_env,
            capture_output=True,
            timeout=15,
        )
        (root / f"machines-{label}.txt").write_bytes(result.stdout)
        assert result.returncode == 0
        return result.stdout.decode().splitlines()

    try:
        first = start("first")
        cli = start_managed_worker(root, repo, env, spec_path)
        assert cli.returncode == 0, "Public start failed; see CLI evidence"
        native_thread = wait_for(
            lambda: next(
                (
                    e["returnedThreadId"]
                    for e in collect("first")
                    if e.get("returnedThreadId")
                ),
                None,
            ),
            "first native thread",
        )
        report["persistedBeforeCrash"] = wait_for_session_ref(
            home / "state", native_thread
        )
        marker = json.loads((runtime / "owner.json").read_text())
        report["firstOwner"] = marker
        report["firstPointer"] = json.loads(
            (home / "state/smolvm-runtimes" / runtime.name).read_text()
        )
        assert any(e.get("guestCodexProcesses") for e in collect("first"))
        assert managers and all(live(pid) for pid in managers)
        assert machines("before-crash") == ["worker"]
        (root / "previous-managers.json").write_text(json.dumps(managers))
        with closing(
            sqlite3.connect(
                f"file:{home / 'state/lifecycle-operations.sqlite3'}?mode=ro", uri=True
            )
        ) as db:
            tables = {
                row[0]: [dict(zip([c[0] for c in cur.description], row)) for row in cur]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('root_state','harnesses')"
                ).fetchall()
                for cur in [db.execute('SELECT * FROM "' + row[0] + '"')]
            }
        (root / "desired-before-crash.json").write_text(json.dumps(tables, indent=2))
        assert tables["root_state"][0]["schema_version"] == 2
        assert (
            json.loads(tables["harnesses"][0]["execution_runtime_json"])
            == spec.to_json()
        )
        assert report["persistedBeforeCrash"]["session_ref"] == native_thread
        first.send_signal(signal.SIGKILL)
        assert first.wait(timeout=10) == -signal.SIGKILL
        report["managersAfterCrash"] = [pid for pid in managers if live(pid)]
        report["ownerAfterCrash"] = json.loads((runtime / "owner.json").read_text())
        report["pointerAfterCrash"] = json.loads(
            (home / "state/smolvm-runtimes" / runtime.name).read_text()
        )
        assert report["ownerAfterCrash"] == marker
        assert report["pointerAfterCrash"] == report["firstPointer"]
        assert machines("after-crash") == ["worker"]
        assert report["managersAfterCrash"], (
            "Crash did not exercise surviving VM reclamation"
        )
        second = start("second")
        wait_for(
            lambda: any(e.get("kind") == "before-create" for e in collect("second")),
            "replacement VM create after old reclamation",
        )
        wait_for(
            lambda: collect("second") and len(managers) >= 2, "replacement VM manager"
        )
        new_create = next(
            e for e in collect("second") if e.get("kind") == "before-create"
        )
        report["secondOwner"], report["secondPointer"] = (
            new_create["owner"],
            new_create["pointer"],
        )
        assert report["firstOwner"]["generation"] != report["secondOwner"]["generation"]
        assert report["first"]["epoch"] != report["second"]["epoch"]
        assert not any(live(pid) for pid in report["managersAfterCrash"])
        # A no-turn thread has no saved native rollout. Recovery must attempt
        # that ref and reject it, rather than inventing a fresh thread.
        wait_for(
            lambda: any(e.get("method") == "thread/resume" for e in collect("second")),
            "native resume rejection",
        )
        assert not any(e.get("method") == "thread/start" for e in collect("second"))
        assert not any(
            e.get("prohibited") for g in ("first", "second") for e in collect(g)
        )
        report["ok"] = True
        second.terminate()
        assert second.wait(timeout=60) == 0
    finally:
        stop_isolated_daemons(processes, streams)
        for generation in ("first", "second"):
            collect(generation)
        report["emergencyCleanup"] = emergency_cleanup(runtime, home / "state", spec)
        report["managerPids"] = managers
        report["liveManagersAfterShutdown"] = [pid for pid in managers if live(pid)]
        after_health = health("after")
        report["productionIdentity"] = production_identity(before_health, after_health)
        report["productionUnchanged"] = production_unchanged(
            before_health, after_health
        )
        report["hostCanariesUnchanged"] = before_host == _host_canaries()
        report["rootfsUnchanged"] = (
            tree_digest(Path(spec.rootfs_path)) == spec.rootfs_digest
        )
        report["ownerAfterShutdown"] = (runtime / "owner.json").exists()
        report["pointerAfterShutdown"] = (
            home / "state/smolvm-runtimes" / runtime.name
        ).exists()
        report["runtimeLeftovers"] = (
            sorted(str(p.relative_to(runtime)) for p in runtime.rglob("*"))
            if runtime.exists()
            else []
        )
        (root / "report.json").write_text(json.dumps(report, indent=2))
    assert not report["liveManagersAfterShutdown"]
    assert not report["emergencyCleanup"], (
        "Normal daemon cleanup failed; fixture reclaimed owned resources"
    )
    assert not report["ownerAfterShutdown"] and not report["pointerAfterShutdown"]
    assert (
        report["productionUnchanged"]
        and report["hostCanariesUnchanged"]
        and report["rootfsUnchanged"]
    )
    assert report["runtimeLeftovers"] == ["runtime.lock"]
