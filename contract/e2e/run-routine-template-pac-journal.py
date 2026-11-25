#!/usr/bin/env python3
"""Real PAC-journal source over managed workflow requests and a borrowed coordinator."""

from __future__ import annotations

from pathlib import Path
import runpy
import sys
import tempfile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hyprial.biz.impl.routine.templates import render_template
from _daemon_runner import IsolatedDaemon, hyprial_executable, short_temp_root

ROUTINE = "pac-template-e2e"
ACTOR = "agent:e2e:routine-template-pac-e2e:coordinator"
SESSION = "pac-source-e2e"


def main() -> int:
    root = Path(
        tempfile.mkdtemp(prefix="pac-journal-e2e-", dir=short_temp_root())
    ).resolve()
    daemon = IsolatedDaemon(hyprial_executable(), root, "routine-template-pac-e2e")
    helpers = runpy.run_path(str(Path(__file__).with_name("run-selfdrive-routine.py")))
    started = False
    try:
        started = True
        daemon.start()
        helpers["register"](daemon, ACTOR, SESSION)
        # Derive the packaged template explicitly for a borrowed interactive
        # coordinator. The owned native coordinator is covered by E2E-015.
        document = yaml.safe_load(
            render_template(
                "selfdrive",
                owner=ACTOR,
                escalate_to="user:e2e",
                name=ROUTINE,
                interval="1m",
                idle_threshold="1s",
            )
        )
        document.pop("produces")
        document["actor"] = ACTOR
        path = root / "routine.yaml"
        path.write_text(yaml.safe_dump(document, allow_unicode=True))
        daemon.command("routine", "add", str(path), "--json")
        workflow = root / "waiting.yaml"
        workflow.write_text(
            yaml.safe_dump(
                {
                    "version": 2,
                    "name": "waiting",
                    "nodes": [
                        {
                            "id": "work",
                            "owner": ACTOR,
                            "role": "dispatch",
                            "task": "Coordinate pending work.",
                            "timeout": "10m",
                        }
                    ],
                }
            )
        )
        graph = daemon.command("workflow", "run", str(workflow), "--yes", "--json")[
            "graphId"
        ]
        generated = daemon.wait(
            lambda: daemon.command("workflow", "list", "--json"),
            lambda value: any(r["routineName"] == ROUTINE for r in value["runs"]),
            timeout=120,
        )
        wake_graph = next(
            r["graphId"] for r in generated["runs"] if r["routineName"] == ROUTINE
        )
        wake = daemon.wait(
            lambda: helpers["pending"](daemon, ACTOR, SESSION),
            lambda messages: any(
                graph in str(m.get("summary"))
                and "你被 routine 唤醒:" in str(m.get("summary"))
                for m in messages
            ),
        )
        assert any(
            ACTOR in str(m.get("summary")) and "idle for" in str(m.get("summary"))
            for m in wake
        )
        node = daemon.command("workflow", "status", wake_graph, "--json")["nodes"][0]
        assert node["owner"] == ACTOR and node["state"] == "requested"
        daemon.request(
            "workflow.complete",
            {
                "graphId": wake_graph,
                "nodeId": node["nodeId"],
                "requestId": node["requestId"],
                "reasonRef": "e2e:journal-wake",
                "actor": ACTOR,
                "sessionRef": SESSION,
            },
        )
        daemon.wait(
            lambda: daemon.command("workflow", "status", wake_graph, "--json"),
            lambda value: value["state"] == "completed",
        )
        daemon.command("routine", "rm", ROUTINE, "--json")
        daemon.command("workflow", "cancel", graph, "--json")
    finally:
        if started:
            daemon.stop()
            daemon.wait_stopped()
    print(
        f"RESULT PASS ROUTINE-TEMPLATE-PAC managed assignment, exact URI, explicit flag, cleanup=PASS root={root}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
