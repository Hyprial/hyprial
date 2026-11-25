#!/usr/bin/env python3
"""Two real isolated daemons, native fixture, explicit delegated return custody."""

from __future__ import annotations

import importlib.util
import json
import os
from contextlib import closing
from pathlib import Path
import shlex
import sqlite3
import sys
import tempfile
import time
from uuid import uuid4

import yaml

from hyprial.kernel import CHANNEL_PROTOCOL_VERSION

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _daemon_runner import IsolatedDaemon, free_tcp_endpoint

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "pac_gate", Path(__file__).with_name("run-pac-workflow-unified.py")
)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)
wait, pid_gone, worker_pid = gate.wait, gate.pid_gone, gate.worker_pid
agent_uri = gate.agent_uri


def main():
    root = Path(tempfile.mkdtemp(prefix="pac-cross-")).resolve()
    executable = Path(
        os.environ.get("HYPRIAL_BIN", str(ROOT / ".venv/bin/hyprial"))
    ).resolve()
    endpoint = free_tcp_endpoint()
    origin = IsolatedDaemon(executable, root / "origin", "pac-origin", listen=endpoint)
    executor = IsolatedDaemon(
        executable, root / "executor", "pac-executor", connect=endpoint
    )
    executor.env["HYPRIAL_OWNER"] = "e2e-other"
    for daemon in (origin, executor):
        binary = daemon.root / "bin"
        binary.mkdir(parents=True)
        wrapper = binary / "codex"
        wrapper.write_text(
            "#!/bin/sh\nexport FAKE_CODEX_MODE=pac-complete\nexport FAKE_CODEX_LOG="
            + shlex.quote(str(daemon.root / "protocol.jsonl"))
            + "\nexec "
            + shlex.quote(sys.executable)
            + " "
            # Hard cut C07: the carrier injects the summary only; the shim
            # reads the body back as the worker before the fixture acts.
            + shlex.quote(str(ROOT / "contract/e2e/_pac_complete_codex.py"))
            + ' "$@"\n'
        )
        wrapper.chmod(0o700)
        daemon.env.update(
            PATH=str(binary) + os.pathsep + daemon.env.get("PATH", ""),
            CODEX_HOME=str(daemon.root / "codex-home"),
            UV_CACHE_DIR=str(daemon.root / "uv-cache"),
        )
    session = "cross-node-manual"
    lease = uuid4().hex
    manual_registered = False
    started = []
    owned_pids = []
    try:
        for daemon in (origin, executor):
            started.append(daemon)
            daemon.start()
            wait(
                "daemon restore",
                lambda: (
                    daemon.request("ping", {}).get("restore", {}).get("complete", True)
                ),
            )
        # Project an isolated native config for the borrowed worker: the Codex
        # app-server reads <config>/native/codex/config.toml at ``config/read``
        # and fails its launch without it (same shape as the unified runner).
        personality = root / "borrowed-personality"
        config = personality / "native/codex/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('cli_auth_credentials_store = "ephemeral"\nmodel = "fixture"\n')
        config.chmod(0o600)
        workspace = executor.root / "borrowed-workspace"
        workspace.mkdir(mode=0o700, exist_ok=True)
        executor.command(
            "agent", "create", "--name", "borrowed", "--config", str(personality),
            "--cwd", str(workspace), "--json",
        )
        executor.command(
            "agent", "start", "codex", "--name", "borrowed", "--headless", "--json"
        )
        actor = agent_uri(executor, "borrowed")
        manual_workspace = root / "manual-workspace"
        manual_workspace.mkdir(mode=0o700)
        executor.command("agent", "create", "--name", "manual", "--cwd", str(manual_workspace), "--json")
        manual = agent_uri(executor, "manual")
        worker = worker_pid(executor.root, "borrowed")
        owned_pids.append(worker)

        def dispatch(
            name, task="Do the requested work.", owner=actor, role="execute", **extra
        ):
            path = root / f"{name}.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "version": 2,
                        "name": name,
                        "defaults": {"timeout": "5m"},
                        "nodes": [
                            {"id": "work", "owner": owner, "role": role, "task": task}
                        ],
                        **extra,
                    }
                )
            )
            return origin.command("workflow", "run", str(path), "--yes", "--json")[
                "graphId"
            ]

        def status(graph):
            if manual_registered:
                executor.request("session.heartbeat", {
                    "actor": manual, "sessionRef": session, "channelLeaseToken": lease,
                })
            return origin.command("workflow", "inspect", graph, "--json")

        graph = dispatch("native")
        wait("remote native completion", lambda: status(graph)["state"] == "completed")
        os.kill(worker, 0)
        with closing(sqlite3.connect(origin.root / "state/pac-graph.sqlite3")) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM flag_events WHERE graph_id=?", (graph,)
                ).fetchone()[0]
                == 1
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM nodes WHERE graph_id=? AND kind='actor'",
                    (graph,),
                ).fetchone()[0]
                == 0
            )
        print(
            "PASS remote native CLI completion; one authoritative graph; borrow remains alive",
            flush=True,
        )

        failed = dispatch("native-failure", task="fixture:turn-fail")
        wait("remote failure policy", lambda: status(failed)["state"] == "failed")
        os.kill(worker, 0)
        print("PASS remote native failure return; no borrowed actor stop", flush=True)

        manual_registration = {
                "actor": manual,
                "sessionRef": session,
                "cwd": str(manual_workspace),
                "command": ["claude"],
                "source": "claude-channel",
                "channelConfirmed": True,
                "channelProtocolVersion": CHANNEL_PROTOCOL_VERSION,
                "channelLeaseToken": lease,
        }
        executor.request("session.register", manual_registration)
        manual_registered = True

        def binding(graph):
            row = wait(
                "request published",
                lambda: next(
                    (
                        n
                        for n in status(graph)["nodes"]
                        if n["nodeId"] == "work" and n.get("requestId")
                    ),
                    None,
                ),
            )

            def remote_registered():
                # Origin publication precedes delivery on the executor. Do
                # not call its strict inspection API before this exact grant
                # is durably registered; all inspection errors after that
                # boundary still fail the scenario.
                database = executor.root / "state/pac-graph.sqlite3"
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                    received = db.execute(
                        "SELECT 1 FROM remote_workflow_requests "
                        "WHERE request_id=? AND graph_id=? AND node_id=?",
                        (row["requestId"], graph, "work"),
                    ).fetchone()
                return received is not None

            # Registration and current-binding inspection share the original
            # 45s receipt budget, but have distinct failure labels.
            deadline = time.monotonic() + 45
            wait("executor registration", remote_registered, timeout=45)
            wait(
                "remote delivery current binding",
                lambda: executor.request(
                    "workflow.node.inspect",
                    {
                        "runId": graph,
                        "target": "work",
                        "actor": manual,
                        "sessionRef": session,
                    },
                )["current"],
                timeout=max(0.0, deadline - time.monotonic()),
            )
            return {
                "graphId": graph,
                "nodeId": "work",
                "requestId": row["requestId"],
                "reasonRef": "e2e:remote-proof",
                "actor": manual,
                "sessionRef": session,
            }

        cancelled = dispatch("cancelled", owner=manual, role="dispatch")
        bound = binding(cancelled)
        try:
            executor.request("workflow.complete", {**bound, "sessionRef": "forged"})
        except AssertionError as error:
            assert "CALLER_NOT_AUTHORIZED" in str(error)
        else:
            raise AssertionError("forged local binding accepted")
        origin.command("workflow", "cancel", cancelled, "--json")
        try:
            executor.request("workflow.complete", bound)
        except AssertionError as error:
            assert "WORKFLOW_REQUEST_STALE" in str(error)
        else:
            raise AssertionError("cancelled remote completion accepted")
        print("PASS local session authority and origin cancellation fence", flush=True)

        rework_path = root / "rework.yaml"
        rework_path.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "name": "rework",
                    "nodes": [
                        {
                            "id": "work",
                            "owner": manual,
                            "role": "dispatch",
                            "task": "Review this work.",
                        },
                        {
                            "id": "accept",
                            "kind": "end",
                            "owner": "user:e2e",
                            "task": "Accept.",
                            "after": ["work"],
                        },
                    ],
                }
            )
        )
        rework_graph = origin.command(
            "workflow", "run", str(rework_path), "--yes", "--json"
        )["graphId"]
        first = binding(rework_graph)
        assert (
            executor.request(
                "pac.flag.set", {**first, "expectedRequest": first["requestId"]}
            )["returnState"]
            == "accepted"
        )
        executor.request("pac.flag.reset", first)
        wait(
            "remote rework new request",
            lambda: (
                next(n for n in status(rework_graph)["nodes"] if n["nodeId"] == "work")[
                    "requestId"
                ]
                != first["requestId"]
            ),
        )
        fresh = binding(rework_graph)
        assert executor.request("workflow.complete", fresh)["returnState"] == "accepted"
        ready = wait(
            "human end ready",
            lambda: next(
                (
                    n
                    for n in status(rework_graph)["nodes"]
                    if n["nodeId"] == "accept" and n["state"] == "requested"
                ),
                None,
            ),
        )
        origin.request(
            "workflow.complete",
            {
                "graphId": rework_graph,
                "nodeId": "accept",
                "requestId": ready["requestId"],
                "reasonRef": "e2e:accepted",
                "actor": "user:e2e",
            },
        )
        print("PASS remote low-level flag set/reset and explicit rework", flush=True)

        pending_graph = dispatch("offline", owner=manual, role="dispatch")
        bound = binding(pending_graph)
        origin.stop()
        origin.wait_stopped()
        pending = executor.request("workflow.complete", bound)
        assert pending["returnState"] == "pending"
        executor.stop()
        executor.wait_stopped()
        executor.start()
        executor.request("session.register", manual_registration)
        origin.start()
        wait(
            "restart durable outcome returned",
            lambda: status(pending_graph)["state"] == "completed",
        )
        with closing(sqlite3.connect(executor.root / "state/pac-graph.sqlite3")) as db:
            result = json.loads(
                db.execute(
                    "SELECT result_json FROM remote_workflow_outbox WHERE request_id=?",
                    (bound["requestId"],),
                ).fetchone()[0]
            )
            assert result["returnState"] == "accepted"
        print("PASS offline completion custody across both daemon restarts", flush=True)

        routine = root / "routine.yaml"
        routine.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "mode": "scheduled",
                    "name": "remote-routine",
                    "schedule": {"interval": "60s"},
                    "actor": actor,
                    "task": "Coordinate a remote occurrence.",
                    "on_task_timeout": {
                        "action": "escalate",
                        "escalate_to": "user:e2e",
                    },
                }
            )
        )
        origin.command("routine", "add", str(routine), "--json")

        def occurrence():
            with closing(sqlite3.connect(origin.root / "state/pac-graph.sqlite3")) as db:
                return db.execute(
                    "SELECT graph_id FROM workflow_graphs WHERE routine_name='remote-routine'"
                ).fetchone()

        occurrence_id = wait("remote scheduled occurrence", occurrence, timeout=90)[0]
        wait(
            "remote routine completion",
            lambda: status(occurrence_id)["state"] == "completed",
        )
        sole_binding = origin.command(
            "routine", "remove", "remote-routine", "--json", exits=(1,)
        )
        assert sole_binding.get("code") == "ROUTINE_LAST_BINDING", sole_binding
        # #920 also fences the last binding of a remote borrowed actor.
        # Supply a successor binding before exercising removal and proving
        # that the borrowed native worker remains alive.
        replacement = root / "routine-replacement.yaml"
        replacement_spec = yaml.safe_load(routine.read_text())
        replacement_spec["name"] = "remote-routine-replacement"
        replacement.write_text(yaml.safe_dump(replacement_spec))
        origin.command("routine", "add", str(replacement), "--json")
        origin.command("routine", "remove", "remote-routine", "--json")
        latest_worker = worker_pid(executor.root, "borrowed")
        owned_pids.append(latest_worker)
        os.kill(latest_worker, 0)
        print(
            "PASS remote routine dispatch and removal leaves borrowed actor alive",
            flush=True,
        )
        for daemon in (origin, executor):
            with closing(sqlite3.connect(daemon.root / "state/pac-graph.sqlite3")) as db:
                assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                assert not db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        for daemon in reversed(started):
            daemon.stop()
            daemon.wait_stopped()
        for pid in set(owned_pids):
            wait("fixture PID cleanup", lambda: pid_gone(pid), timeout=15)
    print(f"RESULT PASS PAC-CROSS-NODE root={root} cleanup=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
