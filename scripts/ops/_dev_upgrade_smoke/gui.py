"""Optional GUI check for the apply flow.

Decisions (owner-approved 2026-11-20, plan sections 2 and 4.4, owner answers Q1
(a), Q2 and Q9; PR-3):
- ``hyprial gui --no-open --json --port <p> --stdin-stop`` runs as a child of
  this script through ``Runner.spawn`` (own session, stdin and stdout pipes,
  stderr in ``<artifacts>/gui-<epoch>.err.log``), on Linux and macOS alike.  No
  ``systemd-run``, no transient unit, no service manager: the GUI never outlives
  the script, because a closed stdin (also when the script dies) stops it.
- The child gets the live environment (``live_environment``: the target
  HYPRIAL_HOME / HARNESS_STATE_DIR, since ``hyprial gui`` ensures the daemon of
  the home it resolves) without any ``HYPRIAL_GUI_*`` variable,
  ``HYPRIAL_BACKEND_ENDPOINT`` or ``HYPRIAL_DEV_GUI_HOME``: inherited GUI
  configuration is refused by the product (``GUI_CONFIG_CONFLICT``) or would
  pick another driver, home or grant.
- Q9: the script owns ``HYPRIAL_GUI_LAUNCH_TTL_MS``.  Without a hold it is not
  set, so the product default (60 s) applies: nobody opens the GUI and the
  checks never use the grant.  With ``--gui-hold-minutes N`` it is N minutes
  capped at 10 (at least 60 s), so a person can still log in during a hold.
  ``--gui-ttl-minutes`` is gone.
- Line 1 on stdout is the startup record or a failure line
  (``contract/cli/gui-startup-record.v1.json``); it is read within 120 s
  (``GUI_RECORD_WAIT_SECONDS``: daemon ensure plus the host budget).  A child
  that exits first fails ``gui.start``; exit 0 with no line is a stop before the
  record, which nothing here requested, so it is a failure too.
- A ``GUI_RUNTIME_BUSY`` failure line (another foreground GUI holds the runtime
  of the home) fails ``gui.start`` naming the home and the runtime, and the
  upgrade is rolled back (decision of 2026-11-21, point 3: the fallback for a
  GUI that appears after the pre-stop probe of ``gui_probe.py``).
- The record is validated before any of its fields is used
  (``validate_gui_record``); the listener and the printed tunnel use the
  record's host and port, never ``--gui-port`` (which may be 0).
- A failed GUI check is a failed upgrade: ``start_gui`` returns False and the
  caller rolls back.
- Stop: close stdin and wait up to 30 s for exit 0; else SIGTERM to the child's
  process group (10 s), then SIGKILL (5 s).  Once the root exits,
  ``group_alive()`` verifies the entire process group is empty; any surviving
  descendants receive bounded SIGTERM (10 s) then SIGKILL (5 s), failing
  ``gui.stop`` (or leaving ``ROLLED_BACK_GUI_LEFT`` / exit 3 if still alive).
  A child that needed a signal, or
  that exited non-zero after its startup record, fails ``gui.stop`` (rolled
  back).  A child that already exited after a failure line (exit 1), or after
  an earlier GUI check failed, is not stopped again.  A child still alive after
  SIGKILL is manual work: the upgrade is rolled back first, then the run ends as
  ``ROLLED_BACK_GUI_LEFT`` / exit 3 and the summary names the process group and
  the kill command.  Closing the GUI never stops the daemon;
  ``gui.daemon-survives-close`` proves it.
- The stop steps name their signal (``"SIGTERM"``, ``"SIGKILL"``) and the value
  is read from the ``signal`` module only when ``end_child`` sends it, so
  importing this module needs neither: a platform whose ``signal`` module
  lacks a name fails when a child must be signalled, not at import, which
  would take every importer of the package down with it.
- The script observes the target daemon (pid, socket, the unit's main pid
  through the service seam) before and after the GUI start.  It cannot observe a
  daemon started for ANOTHER home without reading that home, which it never
  does; an unchanged target daemon is the strongest check available.
- The handoff file is only ``lstat``-ed; its one-time grant is never read.
- ``--gui-hold-minutes N`` (0..60, default 0) keeps the GUI running for N
  minutes on the script's clock after every GUI check passed, prints the tunnel
  command and says the GUI closes when the hold ends.  Ctrl-C ends the hold
  normally (upgrade kept); a GUI that exits by itself ends it as a failure.
  ``gui.tunnel`` is NOT_RUN (the script's informational status) unless a hold
  really keeps the GUI open, then it is PASS.
- One failed ``daemon status`` read before the GUI start must not fail a healthy
  upgrade: it is retried up to 3 times before the check fails.
"""

from __future__ import annotations

import signal
import socket
import sys
from pathlib import Path
from typing import Any

from .common import (
    FAIL,
    NOT_RUN,
    PASS,
    CommandResult,
    daemon_fields,
    handoff_problems,
    handoff_stat,
    load_json,
    safe_gui_summary,
    validate_gui_record,
)

GUI_RECORD_WAIT_SECONDS = 120.0
GUI_STATUS_ATTEMPTS = 3
GUI_HOLD_STEP_SECONDS = 1.0
GUI_POLL_SECONDS = 0.25
GUI_STOP_STEPS = (
    ("stdin closed", None, 30.0),
    ("SIGTERM", "SIGTERM", 10.0),
    ("SIGKILL", "SIGKILL", 5.0),
)
GUI_TTL_MIN_MS = 60_000
GUI_TTL_MAX_MS = 600_000
GUI_ENV_DROPPED = ("HYPRIAL_BACKEND_ENDPOINT", "HYPRIAL_DEV_GUI_HOME")


class GuiMixin:
    def target_daemon(self, label: str) -> dict[str, Any] | None:
        payload, _ = self.daemon_status(label=label)
        fields = daemon_fields(payload or {})
        if fields.get("running") is not True or not isinstance(fields.get("pid"), int):
            return None
        return {
            "pid": fields.get("pid"),
            "socket": fields.get("socket"),
            "mainPid": self.service.main_pid(),
        }

    def launch_ttl_ms(self) -> int | None:
        hold = self.options.gui_hold_minutes
        if hold <= 0:
            return None
        return int(min(max(hold * 60_000, GUI_TTL_MIN_MS), GUI_TTL_MAX_MS))

    def gui_environment(self, *, grant: bool = True) -> dict[str, str]:
        env = {
            key: value
            for key, value in self.live_environment().items()
            if not key.startswith("HYPRIAL_GUI_") and key not in GUI_ENV_DROPPED
        }
        ttl = self.launch_ttl_ms() if grant else None
        if ttl is not None:
            env["HYPRIAL_GUI_LAUNCH_TTL_MS"] = str(ttl)
        return env

    def read_target_daemon(self, label: str = "gui-pre-daemon") -> dict[str, Any] | None:
        for attempt in range(1, GUI_STATUS_ATTEMPTS + 1):
            found = self.target_daemon(f"{label}-{attempt}")
            if found is not None:
                return found
            if attempt < GUI_STATUS_ATTEMPTS:
                self.clock.sleep(1.0)
        return None

    def gui_command(self, port: int | None = None) -> list[str]:
        return [
            str(self.live_cli),
            "gui",
            "--no-open",
            "--json",
            "--port",
            str(self.options.gui_port if port is None else port),
            "--stdin-stop",
        ]

    def start_gui(self) -> bool:
        self.gui_left = None
        if not self.options.gui:
            self.check("gui", NOT_RUN, "not requested")
            return True
        before = self.read_target_daemon()
        if before is None:
            self.check(
                "gui.daemon-identity",
                FAIL,
                "cannot establish the target daemon before the GUI start; "
                "refusing to start a GUI that could start another daemon",
            )
            return False
        stderr_log = self.artifacts_dir / f"gui-{int(self.clock.wall())}.err.log"
        if stderr_log.exists():
            self.check(
                "gui.start",
                FAIL,
                f"{stderr_log} already exists (left by an earlier run); refusing "
                "to start: its output would be mixed with this run's. Use a "
                "fresh --artifacts-dir.",
            )
            return False
        command = self.gui_command()
        first = len(self.checks)
        self.gui = {
            "command": command,
            "stderrLog": str(stderr_log),
            "launchTtlMs": self.launch_ttl_ms(),
        }
        self.gui_lines = []
        try:
            child = self.runner.spawn(
                command, env=self.gui_environment(), stderr_path=stderr_log
            )
        except OSError as error:
            self.check("gui.start", FAIL, f"cannot start hyprial gui: {error}")
            return False
        self.gui["pid"] = child.pid
        try:
            payload = self.inspect_gui(child, before, stderr_log)
            if payload is not None:
                self.hold_gui(
                    child,
                    payload["host"],
                    payload["port"],
                    passed=not self.failed_since(first),
                )
        finally:
            stopped = self.stop_gui(child, first)
        if stopped and not self.failed_since(first):
            self.check_daemon_survives(before)
        return not self.failed_since(first)

    def failed_since(self, first: int) -> bool:
        return any(item.status == FAIL for item in self.checks[first:])

    def first_gui_failure(self) -> str:
        for item in self.checks:
            if item.name.startswith("gui.") and item.status == FAIL:
                return f"GUI check failed: {item.name}: {item.detail[:200]}"
        return "GUI check failed"

    def next_gui_line(self, child: Any) -> str | None:
        line = child.read_line()
        if line is not None:
            self.gui_lines.append(line)
        return line

    def read_gui_record(
        self, child: Any, stderr_log: Path
    ) -> tuple[dict[str, Any] | None, str]:
        deadline = self.clock.monotonic() + GUI_RECORD_WAIT_SECONDS
        while True:
            line = self.next_gui_line(child)
            code = None
            if line is None:
                code = child.poll()
                if code is not None:
                    line = self.next_gui_line(child)
            if line is not None:
                payload = load_json(line)
                if isinstance(payload, dict):
                    return payload, ""
                return None, f"first stdout line is not a JSON object: {line.strip()[:200]!r}"
            if code == 0:
                return None, (
                    "hyprial gui stopped (exit 0) before its startup record; "
                    "nothing requested a stop"
                )
            if code is not None:
                return None, (
                    f"hyprial gui exited rc={code} without a startup record; "
                    f"see {stderr_log}"
                )
            now = self.clock.monotonic()
            if now >= deadline:
                return None, (
                    f"no startup record within {GUI_RECORD_WAIT_SECONDS:.0f}s; "
                    f"see {stderr_log}"
                )
            self.clock.sleep(min(GUI_POLL_SECONDS, deadline - now))

    def gui_start_detail(self, payload: dict[str, Any]) -> str:
        if payload.get("ok") is True:
            return f"pid={self.gui.get('pid')} backend_driver={payload.get('backend_driver')}"
        code = payload.get("code")
        error = str(payload.get("error"))[:200]
        if code == "GUI_RUNTIME_BUSY":
            data = payload.get("data")
            runtime = data.get("runtime") if isinstance(data, dict) else None
            return (
                f"GUI_RUNTIME_BUSY: {error}: another foreground GUI holds the "
                f"runtime {runtime} of home {self.home}; stop that GUI, or run "
                "without --gui"
            )
        return f"hyprial gui failed: code={code} error={error!r}"

    def inspect_gui(
        self, child: Any, before: dict[str, Any], stderr_log: Path
    ) -> dict[str, Any] | None:
        payload, problem = self.read_gui_record(child, stderr_log)
        if payload is None:
            self.check("gui.start", FAIL, problem)
            return None
        self.gui["startup"] = safe_gui_summary(payload)
        self.check(
            "gui.start",
            PASS if payload.get("ok") is True else FAIL,
            self.gui_start_detail(payload),
            self.gui["startup"],
        )
        problems = validate_gui_record(payload, self.options.gui_port)
        if problems:
            self.check(
                "gui.record",
                FAIL,
                "startup record rejected: " + "; ".join(problems),
                self.gui["startup"],
            )
            return None
        self.check("gui.record", PASS, "startup record is a valid daemon-driver record")
        host, port = payload["host"], payload["port"]
        try:
            with socket.create_connection((host, port), timeout=2):
                pass
        except OSError as error:
            self.check("gui.listener", FAIL, f"{host}:{port}: {error}")
        else:
            self.check("gui.listener", PASS, f"{host}:{port} accepts connections")
        info = handoff_stat(Path(payload["handoff_path"]))
        self.gui["handoff"] = info
        wrong = handoff_problems(info)
        self.check(
            "gui.handoff-mode",
            PASS if not wrong else FAIL,
            "handoff file is a regular 0600 file of this user (content not read)"
            if not wrong
            else f"handoff file rejected: {'; '.join(wrong)} (content not read)",
            info,
        )
        self.check(
            "gui.grant",
            PASS,
            "handoff file was not read; read it only through the owner's command",
        )
        after = self.target_daemon("gui-post-daemon")
        self.pass_if(
            "gui.daemon-identity",
            after == before,
            f"target daemon unchanged by the GUI start: {before}",
            f"target daemon changed by the GUI start: before={before} after={after}",
        )
        return payload

    def wait_gui_exit(self, child: Any, seconds: float) -> int | None:
        deadline = self.clock.monotonic() + seconds
        while True:
            code = child.poll()
            now = self.clock.monotonic()
            if code is not None or now >= deadline:
                return code
            self.clock.sleep(min(GUI_POLL_SECONDS, deadline - now))

    def wait_group_empty(self, child: Any, seconds: float) -> bool:
        deadline = self.clock.monotonic() + seconds
        while True:
            if not child.group_alive():
                return True
            now = self.clock.monotonic()
            if now >= deadline:
                return False
            self.clock.sleep(min(GUI_POLL_SECONDS, deadline - now))

    def end_child(
        self, child: Any, label: str, command: list[str], stderr_log: str
    ) -> tuple[int | None, str, bool]:
        code = child.poll()
        exited_first = code is not None
        method = "already exited"
        child.close_stdin()
        if code is None:
            for step, name, seconds in GUI_STOP_STEPS:
                if name is not None and not child.signal_group(getattr(signal, name)):
                    code = child.poll()
                    break
                method = step
                code = self.wait_gui_exit(child, seconds)
                if code is not None:
                    break
        if code is not None and child.group_alive():
            for step, name, seconds in GUI_STOP_STEPS[1:]:
                if name is not None:
                    child.kill_residual_group(getattr(signal, name))
                if self.wait_group_empty(child, seconds):
                    method = f"{step} (residual group)"
                    exited_first = False
                    break
            else:
                code = None
                method = "SIGKILL"
                exited_first = False
        child.close()
        self.recorder.command(
            label,
            CommandResult(
                list(command),
                code if code is not None else -1,
                "".join(self.gui_lines),
                f"stderr: {stderr_log}\nstop: {method}\n",
            ),
        )
        return code, method, exited_first

    def stop_gui(self, child: Any, first: int) -> bool:
        earlier_failure = self.failed_since(first)
        failure_line = self.gui.get("startup", {}).get("ok") is False
        code, method, exited_first = self.end_child(
            child, "gui", self.gui.get("command", []), str(self.gui.get("stderrLog"))
        )
        self.gui["exitCode"] = code
        self.gui["stopMethod"] = method
        pid = child.pid
        if code is None:
            command = f"kill -KILL -- -{pid}"
            self.check(
                "gui.stop",
                FAIL,
                f"GUI process group {pid} is still alive after SIGKILL; stop it "
                f"manually: {command}",
            )
            self.gui_left = pid
            return False
        if exited_first:
            if earlier_failure or failure_line:
                self.check(
                    "gui.stop", PASS, f"GUI had already exited (rc={code}); nothing to stop"
                )
            else:
                self.check(
                    "gui.stop",
                    FAIL,
                    f"GUI exited by itself (rc={code}) before it was stopped; "
                    f"see {self.gui.get('stderrLog')}",
                )
            return True
        if method != "stdin closed":
            self.check(
                "gui.stop",
                FAIL,
                f"GUI did not stop within 30s of its stdin closing; ended by "
                f"{method} (rc={code})",
            )
            return True
        if code == 0 or (failure_line and code == 1):
            self.check("gui.stop", PASS, f"stdin closed; GUI exited rc={code}")
            return True
        self.check(
            "gui.stop",
            FAIL,
            f"stdin closed; GUI exited rc={code}, expected 0; see {self.gui.get('stderrLog')}",
        )
        return True

    def hold_gui(self, child: Any, host: str, port: int, *, passed: bool) -> None:
        hold = self.options.gui_hold_minutes
        if not passed:
            self.check("gui.tunnel", NOT_RUN, "not held: an earlier GUI check failed")
            return
        if hold <= 0:
            self.check(
                "gui.tunnel",
                NOT_RUN,
                "the GUI is stopped when the check ends; pass --gui-hold-minutes "
                "N (and --ssh-host) to keep it open for a tunnel",
            )
            return
        target = f"[{host}]" if ":" in host else host
        ssh_host = self.options.ssh_host or "<ssh-host>"
        tunnel = f"ssh -N -L {port}:{target}:{port} {Path.home().name}@{ssh_host}"
        ttl = (self.launch_ttl_ms() or GUI_TTL_MIN_MS) / 60_000
        print(
            f"GUI is running for the upgraded home and stays open for "
            f"{hold:g} minutes. Tunnel: {tunnel}\n"
            f"The login grant is valid for {ttl:g} minutes from the GUI start. "
            f"The GUI closes when the hold ends (Ctrl-C ends it earlier; the "
            f"upgrade is kept). Read the login grant only through the owner's "
            f"command.",
            file=sys.stderr,
        )
        if self.options.ssh_host:
            self.gui["tunnelCommand"] = tunnel
            self.check(
                "gui.tunnel",
                PASS,
                f"{tunnel} (host key verification stays enabled)",
            )
        else:
            self.check(
                "gui.tunnel", NOT_RUN, "pass --ssh-host to include the SSH tunnel command"
            )
        started = self.clock.monotonic()
        end = started + hold * 60
        interrupted = False
        try:
            while child.poll() is None:
                left = end - self.clock.monotonic()
                if left <= 0:
                    break
                self.clock.sleep(min(left, GUI_HOLD_STEP_SECONDS))
        except KeyboardInterrupt:
            interrupted = True
        held = self.clock.monotonic() - started
        if child.poll() is not None and not interrupted:
            self.check(
                "gui.hold",
                FAIL,
                f"the GUI exited by itself after {held:.0f}s of {hold * 60:.0f}s "
                f"(rc={child.poll()}); see {self.gui.get('stderrLog')}",
            )
            return
        self.check(
            "gui.hold",
            PASS,
            f"interrupted by the operator after {held:.0f}s of {hold * 60:.0f}s; "
            "the GUI is being stopped"
            if interrupted
            else f"GUI held open for {held:.0f}s, now being stopped",
        )

    def check_daemon_survives(self, before: dict[str, Any]) -> None:
        after = self.target_daemon("gui-closed-daemon")
        self.pass_if(
            "gui.daemon-survives-close",
            after == before,
            "target daemon still running with the same pid after the GUI closed",
            f"target daemon changed after the GUI closed: before={before} after={after}",
        )
