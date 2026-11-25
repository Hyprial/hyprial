#!/usr/bin/env python3
"""Isolated CLI/daemon/native-protocol gate; the model is a deterministic fixture."""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import sys
import tempfile
import time
from uuid import uuid4

import yaml

from hyprial.kernel import is_agent_id_segment, parse_agent_uri

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _daemon_runner import IsolatedDaemon

ROOT = Path(__file__).resolve().parents[2]


def wait(label, predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out: {label}")


def worker_events(root, name):
    path = root / "state/logs/workers" / f"codex-{name}.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def worker_pid(root, name):
    return next(
        event["pid"]
        for event in reversed(worker_events(root, name))
        if event.get("event") == "worker.started"
    )


def pid_gone(pid):
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def agent_uri(daemon, name):
    agents = daemon.command("agent", "list", "--json")["agents"]
    for agent in agents:
        if agent.get("actor") == name:
            uri = agent.get("uri")
            if isinstance(uri, str) and is_agent_id_segment(uri.rsplit(":", 1)[-1]):
                return uri
    raise AssertionError(f"agent {name!r} has no canonical id address: {agents!r}")


def assert_failed_worker_attempt(root, graph_id, name, owner, protocol):
    events = worker_events(root, name)
    receipts = [event for event in events if event.get("event") == "worker.environment.receipt"]
    assert len(receipts) == 1, f"expected one native receipt for {name!r}: {receipts!r}"
    receipt = receipts[0]
    actor = receipt.get("actor")
    principal = parse_agent_uri(actor) if isinstance(actor, str) else None
    logical = parse_agent_uri(owner)
    assert receipt.get("name") == name, f"native receipt belongs to another worker: {receipt!r}"
    assert principal is not None and is_agent_id_segment(principal[2]), f"native receipt has no official actor: {receipt!r}"
    assert logical is not None and logical[2] == name and principal[:2] == logical[:2], (
        f"native receipt does not belong to logical owner {owner!r}: {receipt!r}"
    )
    turns = [
        event for event in events
        if event.get("event") == "worker.turn.started"
        and event.get("name") == name
        and event.get("actorId") == owner
        and event.get("conversationId") == f"pac-{graph_id}"
    ]
    assert len(turns) == 1, f"expected one associated graph/worker turn for {graph_id!r}/{name!r}: {turns!r}"
    assert sum(
        event.get("method") == "turn/start" and event.get("fixtureActor") == actor
        for event in protocol
    ) == 1, f"expected exactly one failed native turn for receipt actor {actor!r}"


def main():
    root = Path(tempfile.mkdtemp(prefix="pac-full-", dir=os.environ.get("HYPRIAL_E2E_ROOT", "/tmp")))
    executable = Path(
        os.environ.get("HYPRIAL_BIN", str(ROOT / ".venv/bin/hyprial"))
    ).resolve()
    daemon = IsolatedDaemon(executable, root, "pac-test", listen="", connect="")
    binary = root / "bin"
    binary.mkdir()
    wrapper = binary / "codex"
    wrapper.write_text(
        "#!/bin/sh\nexport FAKE_CODEX_MODE=pac-complete\nexport FAKE_CODEX_LOG="
        + shlex.quote(str(root / "protocol.jsonl"))
        + "\nexec "
        + shlex.quote(sys.executable)
        + " "
        # Hard cut C07: the carrier injects the summary only, so the fixture
        # needs the body the shim reads back as the worker (design §7).
        + shlex.quote(str(ROOT / "contract/e2e/_pac_complete_codex.py"))
        + ' "$@"\n'
    )
    wrapper.chmod(0o700)
    daemon.env.update(
        {
            "PATH": str(binary) + os.pathsep + daemon.env.get("PATH", ""),
            "CODEX_HOME": str(root / "codex-home"),
            "FAKE_CODEX_MODE": "pac-complete",
            "FAKE_CODEX_LOG": str(root / "protocol.jsonl"),
            "UV_CACHE_DIR": str(root / "uv-cache"),
        }
    )
    started = False
    try:
        preflight = root / "preflight.yaml"
        preflight.write_text(
            "version: 2\nname: preflight\nnodes: [{id: work, task: Check.}]\n"
        )
        refused = daemon.command(
            "workflow", "run", str(preflight), "--yes", "--json", exits=(1,)
        )
        assert refused.get("code") == "HYPRIAL_HOME_NOT_INITIALIZED"
        assert not (root / "home").exists()
        started = True
        result = daemon.command("init", "--no-service", "--listen", "", "--connect", "", "--json")
        started = True
        ping = wait(
            "daemon restore",
            lambda: (
                p
                if (p := daemon.request("ping", {}))
                .get("restore", {})
                .get("complete", p.get("ready", True))
                else None
            ),
        )
        print(
            json.dumps(
                {"root": str(root), "init": result.get("ok"), "pid": ping.get("pid")}
            ),
            flush=True,
        )
        daemon.command("agent", "create", "--name", "org-anchor", "--json")
        org = f"group-pac-unified-{uuid4().hex[:8]}"
        daemon.command(
            "org", "create", org, "--json", exits=(0, 1)
        )
        daemon.command("org", "acl", "grant", org, "org-anchor", "--json")
        path = root / "workflow.yaml"
        path.write_text("""version: 2
name: isolated-chain
defaults:
  launch: {harness: codex, model: fixture}
  timeout: 30s
nodes:
  - {id: build, task: Build the isolated fixture.}
  - {id: review, task: Review the isolated fixture., after: [build]}
""")
        plan = daemon.command("workflow", "plan", str(path), "--json")["plan"]
        assert plan["onFailure"] == "terminate"
        run = daemon.command(
            "workflow",
            "run",
            str(path),
            "--operation-key",
            "isolated-chain",
            "--yes",
            "--json",
        )
        graph = run["graphId"]
        status = wait(
            "workflow completion",
            lambda: (
                s
                if (s := daemon.command("workflow", "inspect", graph, "--json"))["state"]
                in {"completed", "failed"}
                else None
            ),
        )
        assert status["state"] == "completed", status
        assert all(n["state"] == "done" for n in status["nodes"])
        db_path = root / "state/pac-graph.sqlite3"

        def reclaimed():
            with closing(sqlite3.connect(db_path)) as db:
                return (
                    db.execute(
                        "SELECT COUNT(*) FROM actor_activations WHERE graph_id=? AND desired='down' AND op='done'",
                        (graph,),
                    ).fetchone()[0]
                    == 2
                )

        wait("both owned workers reclaimed", reclaimed)
        with closing(sqlite3.connect(db_path)) as db:
            owned_names = [
                r[0]
                for r in db.execute(
                    "SELECT actor_name FROM nodes WHERE graph_id=? AND kind='actor'",
                    (graph,),
                )
            ]
        wait(
            "owned worker PIDs gone",
            lambda: all(pid_gone(worker_pid(root, name)) for name in owned_names),
        )
        replay = daemon.command(
            "workflow",
            "run",
            str(path),
            "--operation-key",
            "isolated-chain",
            "--yes",
            "--json",
        )
        assert replay["graphId"] == graph
        with closing(sqlite3.connect(root / "state/workflows.sqlite3")) as legacy:
            assert legacy.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
            try:
                legacy.execute("INSERT INTO runs DEFAULT VALUES")
            except sqlite3.IntegrityError as error:
                assert "WORKFLOW_RETIRED" in str(error)
            else:
                raise AssertionError("legacy write barrier is missing")
        # Explicit borrowing never adopts or stops the existing actor.
        # Project the credential-free fixture config through explicit C,
        # just as a real P2 launch does; never write personality into N.
        personality = root / "borrowed-personality"
        config = personality / "native/codex/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('cli_auth_credentials_store = "ephemeral"\n')
        config.chmod(0o600)
        workspace = root / "borrowed-workspace"
        workspace.mkdir(mode=0o700)
        daemon.command(
            "agent", "create", "--name", "borrowed", "--config", str(personality),
            "--cwd", str(workspace), "--json",
        )
        daemon.command(
            "agent", "start", "codex", "--name", "borrowed", "--cwd", str(workspace),
            "--headless", "--json",
        )
        borrowed_pid = worker_pid(root, "borrowed")
        borrowed_uri = agent_uri(daemon, "borrowed")
        borrowed_file = root / "borrowed.yaml"
        borrowed_file.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "name": "borrowed",
                    "defaults": {"timeout": "30s"},
                    "nodes": [
                        {
                            "id": "work",
                            "task": "Check borrowed lifecycle.",
                            "owner": borrowed_uri,
                        }
                    ],
                }
            )
        )
        borrowed_graph = daemon.command(
            "workflow", "run", str(borrowed_file), "--yes", "--json"
        )["graphId"]
        wait(
            "borrowed task complete",
            lambda: (
                daemon.command("workflow", "inspect", borrowed_graph, "--json")["state"]
                == "completed"
            ),
        )
        os.kill(borrowed_pid, 0)
        assert not any(
            e.get("event") == "worker.stopped" for e in worker_events(root, "borrowed")
        )
        with closing(sqlite3.connect(db_path)) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM nodes WHERE graph_id=? AND kind='actor'",
                    (borrowed_graph,),
                ).fetchone()[0]
                == 0
            )

        # A failed native turn is one attempt and closes the graph immediately.
        failure_file = root / "failure.yaml"
        failure_file.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "name": "native-failure",
                    "defaults": {
                        "launch": {"harness": "codex", "model": "fixture"},
                        "timeout": "30s",
                    },
                    "nodes": [{"id": "work", "task": "fixture:turn-fail"}],
                }
            )
        )
        failure_graph = daemon.command(
            "workflow", "run", str(failure_file), "--yes", "--json"
        )["graphId"]
        wait(
            "native failure",
            lambda: (
                daemon.command("workflow", "inspect", failure_graph, "--json")["state"]
                == "failed"
            ),
        )
        failed_status = daemon.command("workflow", "inspect", failure_graph, "--json")
        failed_owner = next(node["owner"] for node in failed_status["nodes"] if node["nodeId"] == "work")
        with closing(sqlite3.connect(db_path)) as db:
            failed_name = db.execute(
                "SELECT actor_name FROM nodes WHERE graph_id=? AND kind='actor'",
                (failure_graph,),
            ).fetchone()[0]
        wait("failed worker reclaimed", lambda: pid_gone(worker_pid(root, failed_name)))
        protocol = [
            json.loads(line)
            for line in (root / "protocol.jsonl").read_text().splitlines()
        ]
        assert_failed_worker_attempt(root, failure_graph, failed_name, failed_owner, protocol)

        # Routine owns its coordinator across completed occurrence graphs.
        routine_file = root / "routine.yaml"
        routine_file.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "mode": "scheduled",
                    "name": "coordinate",
                    "schedule": {"interval": "60s"},
                    "launch": {"harness": "codex", "model": "fixture"},
                    "task": "Coordinate this isolated occurrence.",
                    "on_task_timeout": {
                        "action": "escalate",
                        "escalate_to": "user:e2e",
                    },
                }
            )
        )
        daemon.command("routine", "add", str(routine_file), "--json")
        coordinator_pid = worker_pid(root, "routine-coordinate")

        def occurrence_complete():
            with closing(sqlite3.connect(db_path)) as db:
                return db.execute(
                    "SELECT graph_id FROM workflow_graphs WHERE routine_name='coordinate' AND state='completed'"
                ).fetchone()

        occurrence = wait("scheduled occurrence", occurrence_complete, timeout=80)[0]
        os.kill(coordinator_pid, 0)
        assert (
            daemon.command("workflow", "inspect", occurrence, "--json")["nodes"][0][
                "owner"
            ]
            == agent_uri(daemon, "routine-coordinate")
        )
        sole_binding = daemon.command(
            "routine", "remove", "coordinate", "--json", exits=(1,)
        )
        assert sole_binding.get("code") == "ROUTINE_LAST_BINDING", sole_binding
        assert "hyprial routine set" in str(sole_binding.get("error")), sole_binding

        # #920: a last binding cannot be removed blindly.  Bind a replacement
        # routine to the existing coordinator, then remove the routine that
        # owns that coordinator and prove its process is reclaimed.
        coordinator = daemon.command("routine", "status", "coordinate", "--json")
        assert coordinator["name"] == "coordinate", coordinator
        assert parse_agent_uri(coordinator["produces"]) == (
            daemon.env["HYPRIAL_OWNER"], daemon.node_id, "routine-coordinate"
        ), coordinator
        coordinator_uri = agent_uri(daemon, "routine-coordinate")
        replacement_file = root / "routine-replacement.yaml"
        replacement_file.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "mode": "scheduled",
                    "name": "coordinate-replacement",
                    "schedule": {"interval": "60s"},
                    "actor": coordinator_uri,
                    "task": "Keep the coordinator bound during replacement.",
                    "on_task_timeout": {
                        "action": "escalate",
                        "escalate_to": "user:e2e",
                    },
                }
            )
        )
        daemon.command("routine", "add", str(replacement_file), "--json")
        daemon.command("routine", "remove", "coordinate", "--json")
        wait("routine coordinator reclaimed", lambda: pid_gone(coordinator_pid))
        # Removing a borrowing routine leaves the original actor alive.
        borrowed_routine = yaml.safe_load(routine_file.read_text())
        borrowed_routine.pop("launch")
        borrowed_routine.update(name="borrowed-routine", actor=borrowed_uri)
        routine_file.write_text(yaml.safe_dump(borrowed_routine))
        daemon.command("routine", "add", str(routine_file), "--json")
        daemon.command("routine", "remove", "borrowed-routine", "--json")
        os.kill(borrowed_pid, 0)
        daemon.command("agent", "stop", "borrowed", "--json")
        wait("test-owned borrowed fixture stopped", lambda: pid_gone(borrowed_pid))
        daemon.stop()
        daemon.wait_stopped()
        started = False
        daemon.command("init", "--no-service", "--listen", "", "--connect", "", "--json")
        started = True
        restored = daemon.command("workflow", "inspect", graph, "--json")
        assert restored["state"] == "completed"
        with closing(sqlite3.connect(db_path)) as db:
            assert db.execute("SELECT COUNT(*) FROM workflow_graphs").fetchone()[0] == 4
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        if started:
            daemon.stop()
            daemon.wait_stopped()
        shutil.rmtree(root / "borrowed-personality", ignore_errors=True)
        shutil.rmtree(root / "home/agents/borrowed/secrets/native/codex", ignore_errors=True)
        print(f"Evidence retained: {root}", flush=True)
    print(
        f"RESULT PASS PAC-WORKFLOW-UNIFIED root={root} graph={graph} cleanup=PASS",
        flush=True,
    )


if __name__ == "__main__":
    main()
