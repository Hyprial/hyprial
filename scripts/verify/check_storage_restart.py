#!/usr/bin/env python3
"""Real isolated daemon SIGKILL/ack/epoch/graceful restart evidence.

Uses the shared isolated scenario owner; never invokes the incident's hardcoded
production prune script. No model provider, account or production socket.
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "contract" / "e2e"))
from _daemon_runner import IsolatedDaemon, hyprial_executable, scenario_tempdir, short_temp_root  # noqa: E402


def main():
    executable = hyprial_executable()
    require_counter = "--require-orphan-counter" in sys.argv[1:]
    with scenario_tempdir("storage-restart", directory=short_temp_root()) as temporary:
        daemon = IsolatedDaemon(executable, temporary, "storage-restart-node")
        try:
            daemon.start()
            before = daemon.request("ping", {})
            assert before["storageHealth"]["healthy"] is True
            writer_available = before["storageHealth"]["writeRate"]["available"] is True
            if require_counter:
                assert writer_available, "integrated orphan counter API is required"
                status = daemon.wait(
                    lambda: daemon.command("daemon", "status", "--json"),
                    lambda value: "orphanPersistence" in value,
                )
                counters = status["orphanPersistence"]
                assert before["storageHealth"]["runtime"]["orphanWriteBytes"] == (
                    counters["appendBytes"] + counters["snapshotBytes"]
                )
            first_epoch = before["epoch"]
            actor = "storage-probe"
            daemon.command("agent", "create", "--name", actor, "--json")
            ids = [daemon.command("message", "send", "--from", actor, "--to", actor, text, "--json")["messageId"]
                   for text in ("ack-before-crash", "pending-survives")]
            assert daemon.command("message", "ack", "--from", actor, ids[0], "--json")["acknowledged"]
            metadata = json.loads((daemon.root / "state" / "daemon.json").read_text())
            assert metadata["pid"] == before["pid"] != os.getpid()
            os.kill(metadata["pid"], signal.SIGKILL)
            daemon.wait_stopped()
            daemon.start()
            current = daemon.request("ping", {})
            assert current["epoch"] != first_epoch
            assert current["storageHealth"]["healthy"] is True
            assert [m["messageId"] for m in daemon.command("daemon", "status", "--json")["pendingMessages"]] == [ids[1]]
            rejected = daemon.command("daemon", "stop", "--expected-epoch", first_epoch,
                                      "--json", exits=(1,))
            assert rejected["code"] == "DAEMON_EPOCH_MISMATCH"
            assert daemon.request("ping", {})["epoch"] == current["epoch"]
            daemon.command("daemon", "stop", "--expected-epoch", current["epoch"], "--json")
            daemon.wait_stopped()
            daemon.start()
            assert [m["messageId"] for m in daemon.command("daemon", "status", "--json")["pendingMessages"]] == [ids[1]]
            health = json.loads((daemon.root / "state" / "daemon-health.json").read_text())
            assert health["healthy"] is True
            assert health["epoch"] == daemon.request("ping", {})["epoch"]
            records = [json.loads(line) for line in (daemon.root / "state" / "logs" / "daemon.jsonl").read_text().splitlines()]
            recovered = [r for r in records if r.get("event") == "inbox.recovery.pending"]
            assert recovered, "restart must audit pending recovery"
            print("RESULT PASS storage-restart acked_replayed=0 pending=1 stale_epoch_rejected=1 "
                  f"healthy_restarts=2 writer_available={int(writer_available)}")
        finally:
            daemon.stop()
            daemon.wait_stopped()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
