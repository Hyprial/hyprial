"""Mutating apply flow, postchecks, GUI, and rollback.

Decisions (plan sections 4.1 and 4.5, owner answers Q4 and Q7, 2026-11-20):
- Every service action goes through ``self.service`` (``service.py``): the
  hyprial control on both platforms, or the legacy systemctl control on Linux.
- Before the stop the service is read again (``service_pre_stop``); a home that
  no longer qualifies is refused with exit 2 (the legacy MainPID guard too).
- After the candidate install and before its start, the candidate CLI must
  still attribute the unit (``service.attributed-after-install``); otherwise
  the rollback runs and the candidate is never started.
- Readiness: ``daemon.running``, a positive pid, no ``lifecycle.crashed``, the
  expected version and custody, within ``--ready-timeout`` counted from the
  start command (so a start answered ``DAEMON_START_TIMEOUT`` after the CLI's own
  30 s window is still waited for).  An attempt may finish after the deadline,
  but the wait never sleeps past it and the failure names the real wait.  A bootstrap ``daemon status`` has no
  ``version``; then the ``version`` of the last ``daemon start`` answer is used
  when that answer names the same pid (``versionSource`` in the check data).
- Rollback order is unchanged: stop with the live CLI (``raw_stop`` of the
  baseline unit applies only before any candidate start attempt, i.e.
  ``not candidate_started``, when the live CLI cannot answer with JSON or
  reports a drifted identity), reinstall the old wheel, the old CLI must
  attribute the unit (``rollback.attributed``), start, wait ready.  Once
  ``candidate_started`` is set immediately before ``apply-start``, any
  unreadable service-status/stop response or baseline identity drift during
  rollback stop fails ``service.stop`` and ends in ``ROLLBACK_FAILED`` without
  calling ``raw_stop``, reinstalling, or claiming ``ROLLED_BACK``.
- A failed stop before any install restores the old daemon once the stop
  outcome is known (``settle_after_failed_stop``); result names stay
  ``FAILED_STOP_TIMEOUT_RESTORED`` / ``FAILED_STOP_TIMEOUT_RESTORE_FAILED``.
- With ``--gui`` the runtime probe (``gui_probe.py``) runs after the pre-stop
  re-check and before the stop; a busy runtime, a probe that cannot be stopped
  or a changed daemon refuses with exit 2.
- A failed GUI check rolls back; a GUI child still alive after SIGKILL then
  ends the run as ``ROLLED_BACK_GUI_LEFT`` / exit 3 with ``rollback.guiPid`` and
  ``rollback.guiStopCommand`` (``gui.py``).
"""

from __future__ import annotations

import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .common import (
    BLOCKED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_ROLLBACK_FAILED,
    EXIT_USAGE,
    FAIL,
    NOT_RUN,
    PASS,
    daemon_fields,
    daemon_ready,
    install_origin_path,
    load_json,
    same_file,
)


READY_POLL_SECONDS = 2.0


class ApplyMixin:
    ready_payload: dict[str, Any] | None = None

    def apply_guard(self) -> str | None:
        if not self.options.expect_host:
            return "--expect-host is required for apply"
        expected = {self.hostname.lower(), socket.getfqdn().lower()}
        if self.options.expect_host.lower() not in expected:
            return (
                f"--expect-host {self.options.expect_host!r} does not match "
                f"{sorted(expected)!r}; refusing to touch the service"
            )
        if not self.options.yes:
            if not sys.stdin.isatty():
                return "apply needs --yes when stdin is not a TTY"
        return self.autostart_refusal()

    def confirm_plan(self) -> bool:
        print(
            f"About to stop {self.service_unit()} on {self.hostname}, "
            f"install {self.new_wheel} version {self.new_info.get('version')}, "
            f"and start it against {self.home}.",
            file=sys.stderr,
        )
        print(
            f"Rollback wheel: {self.old_wheel} "
            f"version {self.old_info.get('version')}",
            file=sys.stderr,
        )
        if self.options.yes:
            return True
        answer = input("Type 'upgrade' to continue: ").strip()
        return answer == "upgrade"

    def readiness_payload(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        fields = daemon_fields(payload)
        start = self.start_payload or {}
        if (
            fields.get("version") is None
            and fields.get("phase") == "bootstrap"
            and isinstance(start.get("version"), str)
            and start.get("pid") == fields.get("pid")
        ):
            return {**fields, "version": start["version"]}, "daemon start"
        return payload, "daemon status"

    def wait_ready(
        self, expected_version: str, *, since: float | None = None
    ) -> tuple[bool, dict[str, Any] | None]:
        started = self.clock.monotonic() if since is None else since
        deadline = started + self.options.ready_timeout
        last_error = "no daemon status attempt"
        last_payload: dict[str, Any] | None = None
        attempt = 0
        while True:
            attempt += 1
            problem = self.ready_attempt(attempt, expected_version)
            if problem is None:
                return True, self.ready_payload
            last_error, payload, final = problem
            last_payload = payload or last_payload
            now = self.clock.monotonic()
            if final or now >= deadline:
                break
            self.clock.sleep(min(READY_POLL_SECONDS, deadline - now))
        self.check(
            "daemon.ready",
            FAIL,
            f"not ready within --ready-timeout {self.options.ready_timeout:g}s "
            f"(waited {self.clock.monotonic() - started:.0f}s): {last_error}",
            {"lastPayload": daemon_fields(last_payload)} if last_payload else {},
        )
        return False, last_payload

    def ready_attempt(
        self, attempt: int, expected_version: str
    ) -> tuple[str, dict[str, Any] | None, bool] | None:
        gate = self.service.ready_gate()
        if gate is not None:
            return gate, None, False
        payload, error = self.daemon_status(label=f"ready-{attempt}")
        if payload is None:
            return error or "daemon status unavailable", None, False
        readable, source = self.readiness_payload(payload)
        ready, detail, fields = daemon_ready(readable, expected_version)
        if not ready:
            wrong = "version" in detail and fields.get("version") not in (None, expected_version)
            return detail, payload, wrong
        problem = self.service.ready_problem(fields.get("pid"))
        if problem is not None:
            return problem, payload, False
        self.baseline["ready"] = {
            key: fields.get(key) for key in ("running", "pid", "version", "nodeId", "socket")
        }
        self.baseline["ready"]["versionSource"] = source
        self.check("daemon.ready", PASS, detail, self.baseline["ready"])
        self.ready_payload = payload
        return None

    def postcheck(self, ready_payload: dict[str, Any] | None) -> bool:
        version_payload, error = self.live_version()
        if version_payload is None:
            self.check("post.version", FAIL, error or "version unavailable")
        else:
            version_ok = (
                version_payload.get("packageVersion") == self.new_info.get("version")
            )
            origin = install_origin_path(version_payload.get("installOrigin"))
            origin_ok = same_file(origin, self.new_wheel)
            self.check(
                "post.version",
                PASS if version_ok and origin_ok else FAIL,
                f"packageVersion={version_payload.get('packageVersion')} "
                f"installOrigin={version_payload.get('installOrigin')}",
                {
                    "versionMatches": version_ok,
                    "originMatches": origin_ok,
                },
            )
        daemon_pid = None
        if ready_payload is not None:
            daemon_pid = daemon_fields(ready_payload).get("pid")
        service_ok, service_detail = self.service.post_service(daemon_pid)
        self.check("post.service", PASS if service_ok else FAIL, service_detail)
        if self.options.expect_routine:
            self.check_routines()
        if self.options.require_pac_schema:
            database = (self.state_dir or Path()) / "pac-graph.sqlite3"
            if not database.is_file():
                self.check("post.pac-schema", FAIL, f"missing {database}")
            else:
                try:
                    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
                    try:
                        row = connection.execute("PRAGMA user_version").fetchone()
                    finally:
                        connection.close()
                except sqlite3.Error as error:
                    self.check("post.pac-schema", FAIL, f"{database}: {error}")
                else:
                    schema = row[0] if row else None
                    self.check(
                        "post.pac-schema",
                        PASS if isinstance(schema, int) and schema > 0 else FAIL,
                        f"user_version={schema} database={database}",
                        {"userVersion": schema},
                    )
        if self.options.require_native_actor:
            native = self.new_info.get("nativeFiles", [])
            if not native:
                self.check(
                    "post.native-actor",
                    FAIL,
                    "candidate wheel has no hyprial/_core native library",
                )
            else:
                installed = []
                if self.tool_dir is not None:
                    installed = sorted(
                        str(path)
                        for path in self.tool_dir.glob(
                            "lib/python*/site-packages/hyprial/_core/*.so"
                        )
                    )
                self.pass_if(
                    "post.native-actor.installed",
                    bool(installed),
                    f"installed={installed}",
                    "candidate wheel declares a native actor library but none "
                    f"was installed under {self.tool_dir}",
                )
                if installed and sys.platform.startswith("linux") and daemon_pid:
                    maps = Path(f"/proc/{daemon_pid}/maps")
                    try:
                        text = maps.read_text(errors="replace")
                    except OSError as error:
                        self.check(
                            "post.native-actor.loaded",
                            BLOCKED,
                            f"cannot read {maps}: {error}",
                        )
                    else:
                        loaded = [path for path in installed if path in text]
                        self.pass_if(
                            "post.native-actor.loaded",
                            bool(loaded),
                            f"loaded={loaded}",
                            "native actor library is installed but not mapped "
                            "in the daemon process",
                        )
                elif installed and sys.platform == "darwin" and daemon_pid:
                    status, loaded, detail = self.process.check_loaded_libraries(
                        daemon_pid, installed
                    )
                    self.check(
                        "post.native-actor.loaded",
                        status,
                        detail,
                        {"loaded": loaded} if loaded else {},
                    )
                elif installed:
                    self.check(
                        "post.native-actor.loaded",
                        NOT_RUN,
                        "process-map check is supported on Linux and macOS only",
                    )
        return self.status() != FAIL

    def check_routines(self) -> None:
        result = self.run_command(
            "post-routine-list",
            [str(self.live_cli), "routine", "list", "--all", "--json"],
            timeout=20,
            env=self.live_environment(),
        )
        payload = load_json(result.stdout)
        if (
            result.returncode != 0
            or not isinstance(payload, dict)
            or payload.get("ok") is False
        ):
            code = payload.get("code") if isinstance(payload, dict) else None
            error = payload.get("error") if isinstance(payload, dict) else result.stderr
            self.check(
                "post.routines",
                FAIL,
                f"routine list failed: rc={result.returncode} code={code} "
                f"{str(error).strip()[:300]}; expected {self.options.expect_routine} "
                "could not be checked",
                {"answer": payload} if isinstance(payload, dict) else {},
            )
            return
        routines = payload.get("routines")
        by_name: dict[str, dict[str, Any]] = {}
        if isinstance(routines, list):
            by_name = {
                item.get("name"): item
                for item in routines
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
        missing = [name for name in self.options.expect_routine if name not in by_name]
        disabled = [
            name
            for name in self.options.expect_routine
            if name in by_name and by_name[name].get("enabled") is not True
        ]
        self.check(
            "post.routines",
            PASS if not missing and not disabled else FAIL,
            f"missing={missing} disabled={disabled} "
            f"listed={sorted(by_name)}",
            {
                "missing": missing,
                "disabled": disabled,
                "inFlight": {
                    name: len(by_name[name].get("inFlight", []))
                    for name in by_name
                },
            },
        )

    def record_attributed(self, name: str) -> None:
        if self.service_control_name() == "hyprial":
            self.check(name, PASS, "the live CLI attributes the installed unit")

    def install_live(self, wheel: Path, label: str) -> bool:
        result = self.install_wheel(
            label,
            wheel,
            env=self.live_install_environment(),
        )
        self.pass_if(
            label,
            result.returncode == 0,
            f"installed {wheel.name}",
            f"install rc={result.returncode}: {result.stderr.strip()[:400]}",
        )
        return result.returncode == 0

    def rollback_after_failure(self, reason: str) -> int:
        self.result = "ROLLBACK"
        self.rollback = {"reason": reason, "attempted": True, "ok": False}
        self.check("rollback.reason", NOT_RUN, reason)
        if not self.mutated:
            self.rollback["ok"] = True
            self.rollback["mutated"] = False
            self.result = "FAILED_NO_MUTATION"
            return EXIT_FAILED
        if not self.service.stop("service.stop", rollback=True):
            self.rollback["ok"] = False
            self.result = "ROLLBACK_FAILED"
            return EXIT_ROLLBACK_FAILED
        if not self.install_live(self.old_wheel, "rollback-install"):
            self.rollback["ok"] = False
            self.result = "ROLLBACK_FAILED"
            return EXIT_ROLLBACK_FAILED
        problem = self.service.attributed_problem("rollback-attributed")
        if problem is not None:
            self.check("rollback.attributed", FAIL, f"{problem}; not starting")
            self.rollback["ok"] = False
            self.result = "ROLLBACK_FAILED"
            return EXIT_ROLLBACK_FAILED
        self.record_attributed("rollback.attributed")
        if not self.service.start("rollback-start"):
            self.rollback["ok"] = False
            self.result = "ROLLBACK_FAILED"
            return EXIT_ROLLBACK_FAILED
        old_version = self.old_info.get("version")
        if not isinstance(old_version, str):
            self.rollback["ok"] = False
            self.result = "ROLLBACK_FAILED"
            return EXIT_ROLLBACK_FAILED
        ready, _ = self.wait_ready(old_version, since=self.start_issued_at)
        if not ready:
            self.rollback["ok"] = False
            self.result = "ROLLBACK_FAILED"
            return EXIT_ROLLBACK_FAILED
        self.rollback["ok"] = True
        self.result = "ROLLED_BACK"
        return EXIT_FAILED

    def flag_gui_left(self, code: int) -> int:
        pid = self.gui_left
        if pid is None:
            return code
        command = f"kill -KILL -- -{pid}"
        self.rollback["guiPid"] = pid
        self.rollback["guiStopCommand"] = command
        self.check(
            "rollback.manual",
            FAIL,
            f"GUI process group {pid} is still running; stop it manually: {command}",
        )
        if code != EXIT_ROLLBACK_FAILED:
            self.result = "ROLLED_BACK_GUI_LEFT"
        return EXIT_ROLLBACK_FAILED

    def restore_after_stop_failure(self) -> int:
        self.rollback = {
            "reason": "service stop did not complete",
            "attempted": True,
            "ok": False,
            "mutated": False,
        }
        old_version = self.old_info.get("version")
        outcome = self.service.settle_after_failed_stop()
        if outcome is None:
            self.check(
                "restore.start",
                FAIL,
                "the stop outcome is still unknown (the daemon is neither proven "
                "stopped nor running under its unit); not starting on top of a "
                "pending stop: check the service by hand",
            )
            restored = False
        elif outcome == "running":
            self.check(
                "restore.start",
                PASS,
                "the old daemon is still running under its unit; no start issued",
            )
            restored = True
        else:
            restored = self.service.start("restore.start", allow_running=True)
        if restored and isinstance(old_version, str):
            restored, _ = self.wait_ready(old_version)
        self.rollback["ok"] = restored
        if restored:
            self.result = "FAILED_STOP_TIMEOUT_RESTORED"
            return EXIT_FAILED
        self.result = "FAILED_STOP_TIMEOUT_RESTORE_FAILED"
        return EXIT_ROLLBACK_FAILED

    def run_apply(self) -> int:
        guard = self.apply_guard()
        if guard is not None:
            self.check("apply.guard", FAIL, guard)
            self.result = "ABORTED"
            return EXIT_USAGE
        if not self.confirm_plan():
            self.check("apply.guard", FAIL, "operator declined the plan")
            self.result = "ABORTED"
            return EXIT_USAGE
        code = self.service_pre_stop()
        if code is not None:
            return code
        code = self.probe_gui_runtime()
        if code is not None:
            return code
        if not self.service.stop():
            return self.restore_after_stop_failure()
        self.mutated = True
        if not self.install_live(self.new_wheel, "apply-install"):
            return self.rollback_after_failure("candidate install failed")
        problem = self.service.attributed_problem("attributed-after-install")
        if problem is not None:
            self.check("service.attributed-after-install", FAIL, problem)
            return self.rollback_after_failure(
                "the candidate CLI does not attribute the unit; not starting it"
            )
        self.record_attributed("service.attributed-after-install")
        self.candidate_started = True
        if not self.service.start("apply-start"):
            return self.rollback_after_failure("service start failed")
        new_version = self.new_info.get("version")
        if not isinstance(new_version, str):
            return self.rollback_after_failure("candidate wheel has no version")
        ready, prepared = self.wait_ready(new_version, since=self.start_issued_at)
        if not ready:
            return self.rollback_after_failure("candidate daemon did not become ready")
        if not self.postcheck(prepared):
            return self.rollback_after_failure("post-upgrade checks failed")
        if not self.start_gui():
            code = self.rollback_after_failure(self.first_gui_failure())
            return self.flag_gui_left(code)
        self.result = self.status()
        return EXIT_OK if self.status() != FAIL else EXIT_FAILED
