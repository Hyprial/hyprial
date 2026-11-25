"""Isolated short-path homes and bounded drivers for real daemon processes.

Decision: daemon-first-z2-integration-support-20261116.  Every home lives under
the short test root (AF_UNIX path limits), has no owner override, runs with the
native engine and a loopback-only network, and is removed on teardown.  Every
process a test starts is tracked (the children it spawned and the pids recorded
in ``daemon.json``/``bootstrap.json``) and is terminated, then killed, in the
fixture finalizer even when the test failed.  Nothing here reads or touches an
ambient home, a production daemon, a service unit or a credential.  All waits
are bounded by the constants below; none is retried silently.
Decision: portable-process-cleanup-20261119.  Every pid is recorded in a
``process_table.Ledger`` with its birth identity (the daemon records carry one)
and is signalled only while that birth still matches; "dead" and "gone" are
read through ``ps``, never ``/proc``, so they mean the same on macOS.  Children
start in their own session, and a test's launchers and hosts register a
``release`` callable that the fixture finalizer runs first, even after a failure.
"""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

from hyprial.kernel import daemon_request_once
from tests.support.process_table import Ledger, alive, wait_until
from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.support.runtime.environment import isolated_environment

REPO_ROOT = Path(__file__).resolve().parents[3]
CHILD_ENTRY = Path(__file__).with_name("child_entry.py")
CLI_SECONDS = 120
PHASE_SECONDS = 60
EXIT_SECONDS = 90


class LifecycleHome:
    """One ownerless home plus the processes started against it."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="z2", dir=hyprial_test_tmp_root())).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.socket = self.state / "daemon.sock"
        self.home.mkdir(mode=0o700)
        (self.home / "settings.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
        self.env = isolated_environment(self.root)
        self.env.pop("HYPRIAL_OWNER", None)
        self.env.pop("HYPRIAL_USER_ID", None)
        self.env.update({
            "HYPRIAL_NODE_ID": "z2-node",
            "HYPRIAL_PEER_DISCOVERY": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "Z2_REPO_ROOT": str(REPO_ROOT),
            "Z2_RECORD": str(self.root / "record.jsonl"),
            "COLUMNS": "200",
        })
        self.children: list[subprocess.Popen] = []
        self.ledger = Ledger()
        self.releasers: list[Callable[[], None]] = []

    def cli(self, *arguments: str, timeout: float = CLI_SECONDS) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "hyprial.cli", *arguments],
            env=self.env, capture_output=True, text=True, timeout=timeout, cwd=str(self.root),
        )

    def cli_json(self, *arguments: str, timeout: float = CLI_SECONDS) -> tuple[int, dict[str, Any]]:
        result = self.cli(*arguments, timeout=timeout)
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1, (result.returncode, result.stdout, result.stderr[-2000:])
        return result.returncode, json.loads(lines[0])

    def spawn(self, **extra_environment: str) -> subprocess.Popen:
        environment = {**self.env, **extra_environment}
        log = (self.root / f"child{len(self.children)}.log").open("w")
        child = subprocess.Popen(
            [sys.executable, str(CHILD_ENTRY)],
            env=environment, stdout=log, stderr=log, cwd=str(self.root), start_new_session=True,
        )
        log.close()
        self.children.append(child)
        self.ledger.record(child.pid)
        return child

    def child_log(self, child: subprocess.Popen) -> str:
        path = self.root / f"child{self.children.index(child)}.log"
        return path.read_text(encoding="utf-8", errors="replace")[-3000:]

    def call(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
        return daemon_request_once(method, params or {}, socket_path=self.socket, timeout=timeout)

    def ping(self) -> dict | None:
        try:
            reply = self.call("ping", timeout=2)
        except Exception:
            return None
        pid = reply.get("pid") if isinstance(reply, dict) else None
        if isinstance(pid, int) and pid > 0 and pid not in self.ledger.births:
            self.ledger.record(pid)
        return reply

    def wait_phase(self, wanted: set[str], child: subprocess.Popen | None = None,
                   budget: float = PHASE_SECONDS) -> dict:
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            if child is not None and child.poll() is not None:
                raise AssertionError(f"daemon exited {child.returncode} before {wanted}: {self.child_log(child)}")
            ping = self.ping()
            if ping is not None and ping.get("phase") in wanted:
                return ping
            time.sleep(0.05)
        raise AssertionError(f"no phase in {wanted} within {budget}s; last ping {self.ping()}")

    def login_and_initialize(self, tag: str) -> None:
        begun = self.call("auth.login.begin", {"intent": "login", "operationKey": f"z2-login-key-{tag}"})
        got = self.call("auth.operation.get", {"operationId": begun["operationId"]})
        assert got["operation"]["state"] == "authenticated", got
        initialized = self.call("onboarding.initialize", {"operationKey": f"z2-initialize-{tag}"})
        assert initialized["state"] == "promoting", initialized

    def serving(self, child: subprocess.Popen) -> dict:
        return self.wait_phase({"serving", "reconciled"}, child)

    def records(self) -> list[dict]:
        path = self.root / "record.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def state_files(self) -> list[str]:
        return sorted(p.name for p in self.state.iterdir()) if self.state.exists() else []

    def recorded_pids(self) -> set[int]:
        found: dict[int, str | None] = {}
        for name in ("daemon.json", "bootstrap.json"):
            try:
                record = json.loads((self.state / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pid = record.get("pid") if isinstance(record, dict) else None
            if isinstance(pid, int) and pid > 0:
                birth = record.get("birthIdentity")
                found[pid] = birth if isinstance(birth, str) and birth else None
        for pid, birth in found.items():
            self.ledger.record(pid, birth)
        return set(found)

    def stop_recorded(self) -> None:
        pids = self.recorded_pids()
        for pid in pids:
            self.ledger.signal(pid, signal.SIGTERM)
        for pid in pids:
            self.wait_dead(pid)

    def cleanup(self) -> None:
        problems: list[BaseException] = []
        for release in reversed(self.releasers):
            try:
                release()
            except Exception as error:
                problems.append(error)
        self.recorded_pids()
        for child in self.children:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        for child in self.children:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=20)
        survivors = self.ledger.stop(term=20, kill=10)
        shutil.rmtree(self.root, ignore_errors=True)
        if survivors:
            problems.append(AssertionError(f"processes of the test outlived its cleanup: {self.ledger.describe(survivors)}"))
        if len(problems) == 1:
            raise problems[0]
        if problems:
            raise ExceptionGroup("the test's processes were not all stopped", problems)

    def wait_dead(self, pid: int) -> None:
        assert wait_until(lambda: not self._live(pid), 30), pid

    def assert_nothing_left(self, *pids: int) -> None:
        wait_until(lambda: not any(self._live(pid) for pid in pids), 20)
        assert not [pid for pid in pids if self._live(pid)], pids
        leftovers = [name for name in self.state_files() if name in {"daemon.sock", "daemon.json", "bootstrap.json"}]
        assert leftovers == [], leftovers

    def _live(self, pid: int) -> bool:
        return self.ledger.same(pid) if pid in self.ledger.births else alive(pid)
