#!/usr/bin/env python3
"""E2E-016: real Taskwarrior source, explicit PAC completion and slot dedup."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from uuid import uuid4

import yaml

from hyprial.kernel import CHANNEL_PROTOCOL_VERSION

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import IsolatedDaemon, hyprial_executable, short_temp_root

ROUTINE = "e2e-selfdrive"
ACTOR_NAME = "duty-driver"
SESSION = "e2e-source-binding"
LEASE_TOKEN = uuid4().hex


def register(daemon, actor, session=SESSION):
    return daemon.request(
        "session.register",
        {
            "actor": actor,
            "sessionRef": session,
            "cwd": str(daemon.root / "workspace"),
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
            "channelProtocolVersion": CHANNEL_PROTOCOL_VERSION,
            "channelLeaseToken": LEASE_TOKEN,
        },
    )


def pending(daemon, actor, session=SESSION):
    daemon.request("session.heartbeat", {"actor": actor, "sessionRef": session, "channelLeaseToken": LEASE_TOKEN})
    return daemon.request(
        "message.pending.list", {"actor": actor, "sessionRef": session}
    )["messages"]


def body_text(
    daemon, message_id, actor, session=SESSION
) -> str:
    """The body behind a summary-only pending row (design §7).

    Hard cut C07: a pending row carries the summary and the byte count, never
    the body.  The task uuid lives in the body of the dispatched turn, so the
    runner reads it on demand exactly as a carrier would.
    """

    result = daemon.request(
        "message.read_body",
        {"actor": actor, "sessionRef": session, "messageId": message_id},
    )
    return str(result.get("message") or "")


def main() -> int:
    root = Path(
        tempfile.mkdtemp(prefix="hyprial-e2e-016-", dir=short_temp_root())
    ).resolve()
    daemon = IsolatedDaemon(hyprial_executable(), root, "sd-e2e016-node")
    data = root / "taskdata"
    data.mkdir()
    taskrc = root / "taskrc"
    taskrc.write_text(f"data.location={data}\nconfirmation=off\n")
    daemon.env.update(TASKDATA=str(data), TASKRC=str(taskrc))
    started = False
    try:

        def task(*args):
            result = subprocess.run(
                ["task", *args],
                env=daemon.env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            return result.stdout

        task("add", "Review source-mode dispatch", "+selfdrive", "+dispatch")
        task("add", "Untagged control")
        tasks = json.loads(task("+selfdrive", "-blocked", "export"))
        assert len(tasks) == 1
        assert {"selfdrive", "dispatch"}.issubset(tasks[0].get("tags", [])), tasks
        task_uuid = tasks[0]["uuid"]
        started = True
        daemon.start()
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        daemon.command("agent", "create", "--name", ACTOR_NAME, "--cwd", str(workspace), "--json")
        actor = next(item["uri"] for item in daemon.command("agent", "list", "--json")["agents"] if item["actor"] == ACTOR_NAME)
        register(daemon, actor)
        spec = {
            "version": 2,
            "mode": "source",
            "name": ROUTINE,
            "actor": actor,
            "schedule": {"interval": "60s"},
            "source": {"kind": "taskwarrior", "filter": "+selfdrive -blocked"},
            "policy": {
                "routes": [{"tag": "dispatch", "target": "self"}],
                "default": "escalate",
                "task_template": "Coordinate {{task.description}} (task {{task.uuid}}).",
            },
            "limits": {"max_in_flight": 1},
            "on_task_timeout": {"action": "escalate", "escalate_to": "user:e2e"},
        }
        path = root / "routine.yaml"
        path.write_text(yaml.safe_dump(spec))
        daemon.command("routine", "add", str(path), "--json")

        def routine_dispatch():
            """The one PAC turn whose body names this task uuid.

            The turn summary is ``workflow:<graph>#work``; the routine's task
            text (and its ``task.uuid``) is only in the body, so filter the
            candidate rows by the ``pac-wf-`` conversation prefix first and
            then read the body.
            """

            for item in pending(daemon, actor):
                if not str(item.get("conversationId", "")).startswith("pac-wf-"):
                    continue
                if task_uuid in body_text(daemon, item["messageId"], actor):
                    return item
            return None

        message = daemon.wait(
            routine_dispatch, lambda item: item is not None, timeout=120
        )
        assert message["conversationId"].startswith("pac-wf-")
        assert all(
            "Untagged control" not in body_text(daemon, m["messageId"], actor)
            for m in pending(daemon, actor)
        )

        def runs():
            records = [
                r
                for r in daemon.command("workflow", "list", "--json")["runs"]
                if r["routineName"] == ROUTINE
            ]
            with closing(sqlite3.connect(root / "state" / "pac-graph.sqlite3")) as database:
                operation_keys = {
                    row[0]: row[1]
                    for row in database.execute(
                        "SELECT graph_id, operation_key FROM graphs "
                        "WHERE graph_id IN (SELECT graph_id FROM workflow_graphs "
                        "WHERE routine_name = ?)",
                        (ROUTINE,),
                    )
                }
            return [
                {**record, "operationKey": operation_keys[record["graphId"]]}
                for record in records
            ]

        assert len(runs()) == 1
        graph = runs()[0]["graphId"]
        daemon.command(
            "message",
            "send",
            "--from",
            actor,
            "--to",
            actor,
            "--conversation",
            message["conversationId"],
            "--reply-to",
            message["messageId"],
            "--summary",
            "DONE this reply is not completion",
            "DONE this reply is not completion",
            "--json",
        )
        status = daemon.command("workflow", "inspect", graph, "--json")
        node = status["nodes"][0]
        assert status["state"] == "running" and node["flag"] is False
        daemon.request(
            "workflow.complete",
            {
                "graphId": graph,
                "nodeId": node["nodeId"],
                "requestId": node["requestId"],
                "reasonRef": "e2e:source-result",
                "actor": actor,
                "sessionRef": SESSION,
            },
        )
        daemon.wait(
            lambda: daemon.command("workflow", "inspect", graph, "--json"),
            lambda value: value["state"] == "completed",
        )
        assert (
            daemon.command("workflow", "inspect", graph, "--json")["nodes"][0][
                "reasonRef"
            ]
            == "e2e:source-result"
        )
        daemon.stop()
        daemon.wait_stopped()
        daemon.start()
        register(daemon, actor)
        outcome = daemon.wait(
            lambda: daemon.command("routine", "status", ROUTINE, "--json"),
            lambda value: "done" in value.get("outcomes", []),
            timeout=120,
        )
        assert outcome["name"] == ROUTINE
        assert outcome["outcomes"] == ["done"], outcome
        records = runs()
        operation_keys = [run["operationKey"] for run in records]
        assert all(
            key.removeprefix("workflow:").startswith(
                f"routine:{ROUTINE}:{task_uuid}:"
            )
            for key in operation_keys
        )
        assert len(operation_keys) == len(set(operation_keys)), (
            "the same admitted source slot was dispatched more than once: "
            f"{operation_keys}"
        )
        first_key = next(run["operationKey"] for run in records if run["graphId"] == graph)
        assert operation_keys.count(first_key) == 1, operation_keys
        # A later source slot may create another run; only replay of the same
        # slot is forbidden by #919.
        removed = daemon.command("routine", "remove", ROUTINE, "--json")
        assert removed.get("removed") is True, removed
        assert any(item.get("uri") == actor for item in daemon.command("agent", "list", "--json")["agents"])
    finally:
        if started:
            daemon.stop()
            daemon.wait_stopped()
    print(
        f"RESULT PASS E2E-016 source flags, ordinary-reply control, slot dedup, restart, cleanup=PASS root={root}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
