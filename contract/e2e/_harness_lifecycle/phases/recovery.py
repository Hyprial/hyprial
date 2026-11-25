"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
def run_liveness_phase(_entry: dict[str, Any], /, daemon, root, case, report):
    connector_id = f"{case.harness}:{case.name}"
    try:
        owner = _entry['daemon_pid'](daemon)
        pids = daemon.wait(
            lambda: _entry['pi_worker_pids'](owner),
            lambda found: len(found) == 1,
            timeout=15.0,
        )
    except (AssertionError, OSError) as error:
        report.add("FAIL", "liveness.worker-pid", str(error))
        report.block(_entry['LIVENESS_CHECKS'][1:], "worker child pid was not identified")
        return
    victim = pids[0]
    report.add(
        "PASS",
        "liveness.worker-pid",
        f"one pi worker descendant of daemon pid={owner}: child pid={victim}",
    )

    _entry['os'].kill(victim, _entry['signal'].SIGKILL)
    try:
        daemon.wait(
            lambda: _entry['connector_status'](daemon.ps(), connector_id),
            lambda status: bool(status) and status.get("running") is not True,
            timeout=15.0,
        )
    except (AssertionError, OSError) as error:
        report.add(
            "FAIL",
            "liveness.stopped-interval",
            f"no observable stopped/error interval after kill -9: {error}",
        )
    else:
        report.add(
            "PASS",
            "liveness.stopped-interval",
            "ps exposed running!=true after kill -9 (one observable interval, "
            "#106)",
        )

    log_path = root / "state" / "logs" / "workers" / f"pi-{case.name}.jsonl"
    try:
        def exit_record() -> dict[str, Any] | None:
            try:
                lines = log_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return None
            for line in reversed(lines):
                try:
                    record = _entry['json'].loads(line)
                except _entry['json'].JSONDecodeError:
                    continue
                if (
                    isinstance(record, dict)
                    and record.get("event") == "worker.exited"
                    and record.get("pid") == victim
                ):
                    return record
            return None

        record = daemon.wait(
            exit_record, lambda value: value is not None, timeout=15.0
        )
        assert isinstance(record, dict)
        if record.get("returnCode") != -_entry['signal'].SIGKILL:
            raise AssertionError(f"unexpected worker exit returnCode: {record}")
        if (
            record.get("component") != "pi-worker"
            or record.get("name") != case.name
        ):
            raise AssertionError(f"worker exit log lost its scope: {record}")
    except (AssertionError, OSError) as error:
        report.add("FAIL", "liveness.exit-log", str(error))
    else:
        report.add(
            "PASS",
            "liveness.exit-log",
            f"{log_path.name} records worker.exited pid={victim} returnCode=-9",
        )

    try:
        _entry['wait_running'](daemon, connector_id, timeout=45.0)
        owner = _entry['daemon_pid'](daemon)
        fresh = daemon.wait(
            lambda: _entry['pi_worker_pids'](owner),
            lambda found: len(found) == 1 and found[0] != victim,
            timeout=15.0,
        )
    except (AssertionError, OSError) as error:
        report.add("FAIL", "liveness.recovers-fresh-pid", str(error))
        report.add(
            "BLOCKED", "liveness.post-recovery-roundtrip", "worker did not recover"
        )
        return
    report.add(
        "PASS",
        "liveness.recovers-fresh-pid",
        f"desired-state recovery after backoff: child pid {victim}->{fresh[0]}",
    )

    nonce = f"N{_entry['time'].time_ns()}"
    try:
        text = _entry['roundtrip'](
            daemon,
            root,
            to=case.nickname,
            prompt=(
                f"Harness E2E-013 recovery probe {nonce}. "
                "Reply with exactly E2E013_RECOVERED_OK and nothing else."
            ),
            conversation=f"e2e013-liveness-{nonce}",
            timeout=_entry['REPLY_TIMEOUT'],
        )
        if "E2E013_RECOVERED_OK" not in text:
            raise AssertionError("reply lacks the demanded marker")
    except (AssertionError, OSError, KeyError) as error:
        report.add("FAIL", "liveness.post-recovery-roundtrip", str(error))
    else:
        report.add(
            "PASS",
            "liveness.post-recovery-roundtrip",
            "recovered worker completed a correlated roundtrip, acknowledged",
        )


def run_teardown_persistence_phase(_entry: dict[str, Any], /, daemon, root, cases, available, report):
    """Teardown semantics: `hyprial agent stop` is manual, permanent, and must stick.

    The differential this pins (production incident 2026-08-15: four
    historical workers revived on every daemon restart because they were
    never removed from desired state): E2E-006 proves desired workers DO
    revive; nobody proved a downed worker MUST NOT.  Both halves are
    asserted here against the same restart.
    """

    case = cases["codex"]
    connector_id = f"{case.harness}:{case.name}"
    owner = _entry['daemon_pid'](daemon)
    pid_before = _entry['codex_appserver_pids'](owner)

    try:
        downed = daemon.command("agent", "stop", case.name, "--json")
        if connector_id not in (downed.get("removed") or []):
            raise AssertionError(f"down did not remove the worker: {downed}")
    except (AssertionError, OSError) as error:
        report.add("FAIL", "teardown.down", str(error))
        report.block(_entry['TEARDOWN_CHECKS'][1:], "worker was not downed")
        return
    report.add(
        "PASS",
        "teardown.down",
        f"removed {connector_id} (child pid before down: {pid_before or 'none'})",
    )

    try:
        daemon.wait(
            lambda: _entry['codex_appserver_pids'](_entry['daemon_pid'](daemon)),
            lambda found: len(found) == 0,
            timeout=20.0,
        )
    except (AssertionError, OSError) as error:
        report.add("FAIL", "teardown.process-terminated", str(error))
    else:
        report.add(
            "PASS",
            "teardown.process-terminated",
            "no codex app-server descendant of the daemon remains",
        )

    try:
        if not _entry['desired_absent'](root, case.harness, case.name):
            raise AssertionError(
                f"desired state still records {connector_id} after down"
            )
    except (AssertionError, OSError, _entry['json'].JSONDecodeError) as error:
        report.add("FAIL", "teardown.desired-state-removed", str(error))
    else:
        report.add(
            "PASS",
            "teardown.desired-state-removed",
            f"{connector_id} is absent from desired-state.json.v1",
        )

    agent_uri = ""
    try:
        record = _entry['agent_record'](daemon, case.name)
        agent_uri = str(record.get("uri"))
        ps = daemon.ps()
        if _entry['connector_status'](ps, connector_id) is not None:
            raise AssertionError(f"ps still lists connector {connector_id}")
        # New contract (persona presence): down does NOT make the row
        # disappear — the daemon-backed delivery promise stays listed and
        # deliverable — but status is the runtime verdict and must say
        # offline.  (targets: status = running?, deliverable = sendable?)
        daemon.wait(
            lambda: _entry['target_status'](daemon, agent_uri),
            lambda status: status == "offline",
            timeout=20.0,
        )
        row = _entry['target_row'](daemon, agent_uri)
        if row is None or row.get("deliverable") is not True:
            raise AssertionError(
                f"downed agent must stay a listed deliverable promise: {row}"
            )
    except (AssertionError, OSError) as error:
        report.add("FAIL", "teardown.deregistered", str(error))
    else:
        report.add(
            "PASS",
            "teardown.deregistered",
            f"connector gone from ps; {agent_uri} listed offline, still deliverable",
        )

    try:
        record = _entry['agent_record'](daemon, case.name)
        if record.get("status") == "online":
            raise AssertionError(
                "agent record reports online with no connector behind it"
            )
    except (AssertionError, OSError) as error:
        report.add("FAIL", "teardown.record-persists", str(error))
    else:
        report.add(
            "PASS",
            "teardown.record-persists",
            "down is not destroy: the agent record remains, offline",
        )

    # The key differential: restart the daemon; desired workers revive,
    # the downed one must not.
    try:
        daemon.stop()
        daemon.start()
        revived: list[str] = []
        for other in ("claude", "pi"):
            if available.get(other, False):
                _entry['wait_running'](daemon, f"{other}:{cases[other].name}", timeout=45.0)
                revived.append(f"{other}:{cases[other].name}")
        # By the time the desired workers are running again, several
        # reconcile passes have completed — a resurrection would already
        # have happened.  Hold the observation a moment longer, then judge.
        _entry['time'].sleep(3.0)
        ps = daemon.ps()
        problems: list[str] = []
        if _entry['connector_status'](ps, connector_id) is not None:
            problems.append(f"ps lists {connector_id} again")
        if _entry['codex_appserver_pids'](_entry['daemon_pid'](daemon)):
            problems.append("a codex app-server child is running again")
        if not _entry['desired_absent'](root, case.harness, case.name):
            problems.append("desired state regained the downed entry")
        if agent_uri:
            status = _entry['target_status'](daemon, agent_uri)
            if status == "online":
                problems.append("targets reports the downed agent online")
            # The restart sweep re-advertises the registered agent (offline,
            # deliverable) — presence is not revival; a running connector is.
            if status is None:
                problems.append("targets lost the downed agent's registry row")
        if problems:
            raise AssertionError(
                "the downed worker revived after daemon restart: "
                + "; ".join(problems)
            )
    except (AssertionError, OSError, _entry['json'].JSONDecodeError) as error:
        report.add("FAIL", "teardown.no-revival-after-restart", str(error))
    else:
        report.add(
            "PASS",
            "teardown.no-revival-after-restart",
            f"desired workers revived ({', '.join(revived) or 'none applicable'}) "
            f"while {connector_id} stayed down: no resurrection",
        )
