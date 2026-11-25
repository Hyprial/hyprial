#!/usr/bin/env python3
"""E2E-007: add four Lark adapters, start them, and prove recovery.

Aligns with the E2E-006 report shape: every check is PASS/FAIL/BLOCKED and
the runner exits 0 only when no check failed and none was blocked.  The
isolated worker simulates only the Lark WebSocket ready boundary; no
production credential or network is used.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _diagnostic_text import clip, one_line
from _daemon_runner import (
    IsolatedDaemon,
    hyprial_executable,
    short_temp_root,
    teardown_guard,
)


# Four adapters on a fake Lark worker: nothing here sends a real Feishu
# request, so the names say so instead of borrowing the production bots'
# names (Allen, 2026-09-24: a test that does not reach Feishu is named for
# what it does).  Credentials and appIds are disposable.
NAMES = (
    "e2e007-fake-lark-1",
    "e2e007-fake-lark-2",
    "e2e007-fake-lark-3",
    "e2e007-fake-lark-4",
)
SECRET_MARKER = "e2e-secret-must-not-be-printed"


@dataclass(frozen=True, slots=True)
class Result:
    status: str
    check: str
    detail: str


class Report:
    def __init__(self) -> None:
        self.results: list[Result] = []

    def add(self, status: str, check: str, detail: str) -> None:
        clean = one_line(detail)
        clean = clip(clean)
        self.results.append(Result(status, check, clean))
        print(f"RESULT {status} E2E-007 {check} {clean}", flush=True)

    def exit_code(self) -> int:
        counts = {
            status: sum(item.status == status for item in self.results)
            for status in ("PASS", "FAIL", "BLOCKED")
        }
        overall = "FAIL" if counts["FAIL"] else (
            "BLOCKED" if counts["BLOCKED"] else "PASS"
        )
        print(
            "RESULT "
            f"{overall} E2E-007 four-lark-adapters "
            f"pass={counts['PASS']} fail={counts['FAIL']} "
            f"blocked={counts['BLOCKED']}",
            flush=True,
        )
        return 1 if counts["FAIL"] else (2 if counts["BLOCKED"] else 0)


def add_adapter(daemon: IsolatedDaemon, root: Path, name: str, index: int) -> None:
    secret_file = root / f"secret-{index}.txt"
    secret_file.write_text(f"{SECRET_MARKER}-{index}\n", encoding="utf-8")
    secret_file.chmod(0o600)
    added = daemon.command(
        "adapter",
        "add",
        name,
        "--app-id",
        f"isolated_contract_{index}",
        "--route",
        f"default=agent:isolated:{name}",
        "--default-route",
        "default",
        "--secret-file",
        str(secret_file),
        "--json",
    )
    if added.get("ok") is not True:
        raise AssertionError(f"adapter add failed for {name}: {added}")
    if SECRET_MARKER in json.dumps(added):
        raise AssertionError("adapter add displayed a Lark secret")


def fake_worker(name: str) -> int:
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Signal readiness on the dedicated fd the daemon hands over (PR #30). The
    # daemon drains and discards worker stdout, so a stdout-only signal is never
    # seen and the adapter would appear to hang until the startup timeout.
    payload = json.dumps({"status": "online", "name": name}).encode("utf-8") + b"\n"
    ready_fd = os.environ.get("HYPRIAL_LARK_READY_FD")
    if ready_fd:
        os.write(int(ready_fd), payload)
    else:
        sys.stdout.write(payload.decode("utf-8"))
        sys.stdout.flush()
    while running:
        time.sleep(0.05)
    return 0


def _wait_restored(daemon: IsolatedDaemon, names: set[str], timeout: float = 90.0) -> list[dict]:
    """Poll `ps --json` until every desired adapter reports online, bounded.

    intg/daemon-readiness (F2/U7): `init` returns at the serving boundary and
    restore runs in the daemon's own thread; the daemon reports the restore
    phase only on the IPC `ping` method, which the CLI does not expose, so the
    public milestone a runner can observe is the assertion's own object -- ps
    listing the desired adapters online.  On dev `init` still waits through
    restore and the first poll already satisfies it.
    """
    deadline = time.monotonic() + timeout
    adapters: list[dict] = []
    while True:
        adapters = daemon.ps().get("adapters", [])
        if {item.get("name") for item in adapters} == names and {
            item.get("status") for item in adapters
        } == {"online"}:
            return adapters
        if time.monotonic() >= deadline:
            return adapters
        time.sleep(0.5)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--fake-worker":
        return fake_worker(sys.argv[2])
    report = Report()
    try:
        with tempfile.TemporaryDirectory(
            prefix="hyprial-four-lark-", dir=short_temp_root()
        ) as raw:
            root = Path(raw)
            hyprial = hyprial_executable()
            daemon = IsolatedDaemon(hyprial, root / "python", "four-lark-contract")
            daemon.env["HYPRIAL_LARK_WORKER_COMMAND"] = json.dumps(
                [sys.executable, str(Path(__file__).resolve()), "--fake-worker"]
            )
            # Configure through the current public CLI seam before daemon boot.
            # The home is deliberately disposable and has no TS snapshot.
            (daemon.root / "home").mkdir(parents=True, mode=0o700)
            for index, name in enumerate(NAMES, 1):
                add_adapter(daemon, root, name, index)
            report.add(
                "PASS",
                "adapter-add",
                "four adapters configured through the public CLI without exposing secrets",
            )
            with teardown_guard(
                daemon.stop,
                daemon.wait_stopped,
                record=lambda detail: report.add(
                    "FAIL",
                    "teardown",
                    f"daemon stop during teardown also raised: {detail}",
                ),
            ):
                daemon.start()
                for name in NAMES:
                    started = daemon.command("adapter", "start", name, "--json")
                    if started.get("adapter", {}).get("status") != "online":
                        raise AssertionError(f"{name} did not become online: {started}")
                report.add(
                    "PASS", "four-online", "four adapter starts reported online"
                )
                ps = daemon.ps()
                adapters = ps.get("adapters", [])
                if {item.get("name") for item in adapters} != set(NAMES) or {
                    item.get("status") for item in adapters
                } != {"online"}:
                    raise AssertionError(f"four-adapter online contract failed: {adapters}")
                report.add(
                    "PASS",
                    "ps-four-online",
                    f"{' '.join(NAMES)} online",
                )
                # Read the desired-state AUTHORITY through its public store,
                # not a file name: on dev the store reads desired-state.json.v1,
                # on intg/daemon-readiness (U0a-2) it reads SQLite, and the next
                # storage change must not break this box again (E2E-007 box 5
                # on intg bb3b8757: "[Errno 2] ... desired-state.json.v1").
                from hyprial.daemon.impl.desired_state import DesiredStateStore

                store = DesiredStateStore(daemon.root / "state" / "desired-state.json")
                desired = store.load()
                recorded = {(spec.provider, spec.name, spec.status) for spec in desired.adapters}
                if recorded != {("lark", name, "running") for name in NAMES}:
                    raise AssertionError(
                        f"desired state did not record four running lark adapters: {desired}"
                    )
                report.add(
                    "PASS",
                    "desired-state",
                    "four running lark adapters recorded in desired state",
                )
                daemon.stop()
                daemon.start()
                # A ps read the instant start() returns sees no adapters on
                # intg (bb3b8757: "restart restore failed: []"): restore is
                # asynchronous there.  Wait, bounded, for the milestone this
                # box asserts -- the four desired adapters online in ps.
                restored = _wait_restored(daemon, set(NAMES))
                if len(restored) != 4 or {item.get("status") for item in restored} != {
                    "online"
                }:
                    raise AssertionError(f"restart restore failed: {restored}")
                report.add(
                    "PASS",
                    "restart-restore",
                    "four desired adapters restored online",
                )
        return report.exit_code()
    except Exception as error:  # noqa: BLE001 - executable contract boundary
        report.add("FAIL", "four-lark-adapters", str(error))
        return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
