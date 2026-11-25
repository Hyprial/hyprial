"""The service seam: how the smoke observes, stops and starts the live daemon.

Decisions (plan sections 3, 4.1 and 4.5; owner answers Q3 to Q7, 2026-11-20):
- One path on Linux and macOS: every service action is a command of the live
  CLI, run by absolute path with the hardened live environment
  (``SmokeBase.live_environment``): ``hyprial daemon service status --json``,
  ``hyprial daemon stop --json`` and ``hyprial daemon start --json``.  hyprial
  derives the per-home unit (``hyprial.service`` / ``hyprial-<id>.service``,
  ``com.hyprial.hyprial.daemon[.<id>]``) from the home; ``--service`` is only
  compared with it (Q3).
- A home is refused (exit 2) unless its unit is installed and attributable to
  this installation, and, when a daemon answers, the manager supervises it
  (``supervision == "supervised"``, which the product reports only when the
  unit's pid is the serving pid).  ``SERVICE_OWNERSHIP_REFUSED`` is refused too
  (exit 2, naming ``data.ownershipReason``); ``SERVICE_PLATFORM_UNSUPPORTED`` is
  BLOCKED (exit 4).  The script never installs a unit (Q6).
- Legacy (Q5): a baseline CLI whose ``daemon service status`` has no
  ``supervision``/``attributed`` keys keeps the former ``systemctl --user`` code
  on Linux, moved here unchanged as ``LegacySystemdControl``; on any other
  platform it is refused.  The keys exist since 2026-10-20, while ``daemon
  stop`` goes through the manager only since 2026-11-06; the stop check below
  closes that gap (a stop answer must name this home's supervisor).  Before
  2026-10-20 there is no ``daemon service`` command at all: the CLI's entry
  turns click's usage error into ``{"ok": false, "code": "INVALID_ARGUMENT",
  "error": "No such command 'service'."}`` under ``--json`` with exit 2 (read
  from the entry module at that date); exit 2 without any JSON is accepted as
  the same answer from even older entries.  An ``ok`` object without the two
  keys is legacy as well.  Delete the legacy control once every test host runs
  a newer wheel.
- A CLI that answers without a JSON object for any other reason (a crash) is a
  failed baseline, never the legacy path.
- Stop: ``daemon stop --json`` with a runner budget of at least
  ``STOP_COMMAND_FLOOR_SECONDS`` (longer than the unit's own stop timeout and
  the CLI's supervisor stop wait).  It fails at once when the answer says
  ``supervisorMayRestart`` or, being ``ok``, does not carry
  ``supervisor: {platform, unit}`` for this home's unit (the stop then went
  over IPC and the manager may relaunch).  Otherwise it is proven by polling for
  ``--stop-timeout`` seconds: service status not running with no pid,
  ``daemon status`` not running, and the pid seen before the stop gone
  (``process.ProcessProbe``).  A non-``ok`` answer alone is not a failure when
  that proof holds (for example ``DAEMON_STOP_TIMEOUT`` followed by the exit).
- Start: ``daemon start --json``.  ``alreadyRunning: true`` fails, except when
  restoring the untouched old daemon.  ``DAEMON_START_TIMEOUT`` is "launched,
  not confirmed": the CLI's supervised start issues the manager's start and
  then gives up after its own idle window (30 s without startup progress,
  120 s hard cap), measured on a loaded host while the daemon was still in a
  107 s startup phase.  The runner budget is ``START_COMMAND_FLOOR_SECONDS``
  (150 s, above the product's 120 s hard cap) while readiness is still checked
  within ``--ready-timeout`` of the start command.  When the service status then shows the unit running
  with a pid (``servingPid: null`` and ``supervision: "lost"`` are the normal
  picture while it starts), the start check passes with the answer in its data
  and readiness is waited for within ``--ready-timeout`` of the start command;
  with nothing running it fails at once.  Every other non-ok answer fails,
  ``DAEMON_START_FAILED`` included: it also covers refusals before any launch
  (socket path too long) and a reachable daemon with an unhealthy database.  The start answer is kept: its
  ``version`` is the readiness version for a bootstrap daemon, whose
  ``daemon status`` carries none (Q7), when its ``pid`` is the status pid.
- Custody and baseline pinning: ``HyprialServiceControl`` captures
  ``(platform, unit)`` on the baseline read and rejects any drift in
  pre-stop, stop, attribution, start confirmation, custody (``ready`` /
  ``post.service``) and failed-stop settle; during rollback stop, a shifted
  candidate CLI falls back to ``raw_stop`` of the baseline unit only when
  ``candidate_started`` is still false.  Once candidate start has been
  attempted, identity drift during rollback stop fails ``service.stop`` and
  the rollback (``ROLLBACK_FAILED``) instead of calling ``raw_stop`` or
  reinstalling the old daemon on top of a potentially running drifted unit.
  Custody requires ``supervised`` and both ``pid`` and ``servingPid`` equal to
  the daemon's pid.
- After the candidate install the candidate CLI must still attribute the unit,
  and in the rollback the old CLI must, before any start.
- Rollback stop (Q4): when the live (candidate) CLI answers without a JSON
  object (to its service status or to ``daemon stop``; a JSON refusal is a
  failed stop, not a reason for the raw stop), the unit recorded at baseline is
  stopped directly only before a candidate start attempt
  (``not candidate_started``):
  ``systemctl --user stop <unit>`` (exit 0, or 5 for not loaded) or
  ``launchctl bootout gui/<uid>/<label>`` (exit 0).  Both are synchronous, the
  candidate CLI cannot observe the result, so the exit code is the proof; the
  old CLI's status and its start (``alreadyRunning`` fails) check it again.
  Once candidate start has been attempted (``candidate_started``), an
  unreadable service-status or stop response fails ``service.stop`` and
  the rollback (``ROLLBACK_FAILED``) instead of calling ``raw_stop`` on only
  the baseline unit or reinstalling over a potentially running daemon.
  This raw stop exists only in the rollback; starts always go through hyprial.
- Restore after a failed stop: ``daemon start`` returns at once when the old
  daemon still answers (it does not queue behind a pending stop job the way
  ``systemctl start`` does), so the restore first waits up to
  ``RESTORE_SETTLE_SECONDS`` for the stop outcome: proven stopped (the same
  proof as the stop, including the pid seen before it), then start;
  running and supervised, then no start; anything else (still deactivating) is
  left to the operator.  The legacy control starts at once, as before
  (measured on systemd 259: the start queues behind the pending stop).
"""

from __future__ import annotations

import dataclasses
import os
import shlex
import sys
from typing import Any

from .common import (
    BLOCKED,
    EXIT_BLOCKED,
    EXIT_FAILED,
    EXIT_USAGE,
    FAIL,
    NOT_RUN,
    PASS,
    CommandResult,
    daemon_fields,
    load_json,
)

STOP_COMMAND_FLOOR_SECONDS = 180.0
START_COMMAND_FLOOR_SECONDS = 150.0
STATUS_TIMEOUT_SECONDS = 30.0
RESTORE_SETTLE_SECONDS = 60.0
POLL_SECONDS = 2.0
LEGACY_UNIT = "hyprial.service"
SYSTEMD_NOT_LOADED_RC = 5
HYPRIAL = "hyprial"
LEGACY = "legacy-systemd"
LAUNCHED_NOT_CONFIRMED = ("DAEMON_START_TIMEOUT",)


def parse_systemd_properties(text: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value.strip()
    return properties


def parse_environment(text: str) -> dict[str, str]:
    if not text:
        return {}
    try:
        tokens = shlex.split(text)
    except ValueError:
        return {}
    environment: dict[str, str] = {}
    for token in tokens:
        key, separator, value = token.partition("=")
        if separator:
            environment[key] = value
    return environment


def service_stopped(properties: dict[str, str]) -> bool:
    """A stop is complete when inactive, or failed (SIGKILL after the stop
    timeout) with no main process left."""
    no_main = properties.get("MainPID", "0") in ("0", "")
    state = properties.get("ActiveState")
    return no_main and state in ("inactive", "failed")


def no_such_command(result: CommandResult, payload: Any) -> bool:
    if result.returncode != 2 or result.timed_out:
        return False
    if payload is None:
        return True
    return (
        isinstance(payload, dict)
        and payload.get("code") == "INVALID_ARGUMENT"
        and "service" in str(payload.get("error"))
    )


def classify_status(result: CommandResult, payload: Any) -> str:
    if no_such_command(result, payload):
        return LEGACY
    if isinstance(payload, dict):
        if payload.get("ok") is not True:
            return "error"
        if "supervision" in payload and "attributed" in payload:
            return HYPRIAL
        return LEGACY
    return "unreadable"


def stopped_status(status: dict[str, Any]) -> bool:
    return status.get("running") is not True and status.get("pid") is None


def describe(status: dict[str, Any]) -> str:
    return " ".join(
        f"{key}={status.get(key)}"
        for key in ("platform", "unit", "installed", "attributed", "supervision",
                    "running", "pid", "servingPid")
    )


class HyprialServiceControl:
    name = HYPRIAL

    def __init__(self, smoke: Any):
        self.smoke = smoke
        self.unit: str | None = None
        self.platform: str | None = None
        self.stop_target: tuple[int, str | None] | None = None

    def unit_name(self) -> str | None:
        return self.unit

    def identity_problem(self, status: dict[str, Any]) -> str | None:
        platform = status.get("platform")
        unit = status.get("unit")
        if (self.platform is not None and platform != self.platform) or (
            self.unit is not None and unit != self.unit
        ):
            return (
                f"service identity changed from baseline "
                f"(platform={self.platform}, unit={self.unit}) to "
                f"(platform={platform}, unit={unit})"
            )
        return None

    def read(self, label: str) -> tuple[CommandResult, Any]:
        smoke = self.smoke
        result = smoke.run_command(
            label,
            [str(smoke.live_cli), "daemon", "service", "status", "--json"],
            timeout=STATUS_TIMEOUT_SECONDS,
            env=smoke.live_environment(),
        )
        payload = load_json(result.stdout)
        if isinstance(payload, dict) and payload.get("ok") is True:
            if self.unit is None and isinstance(payload.get("unit"), str):
                self.unit = payload["unit"]
            if self.platform is None and isinstance(payload.get("platform"), str):
                self.platform = payload["platform"]
        return result, payload

    def snapshot(self, label: str) -> dict[str, Any] | None:
        _, payload = self.read(label)
        if isinstance(payload, dict) and payload.get("ok") is True:
            return payload
        return None

    def evaluate(self, status: dict[str, Any]) -> list[tuple[str, bool | None, str]]:
        found: list[tuple[str, bool | None, str]] = []
        wanted = self.smoke.options.service
        if wanted:
            found.append((
                "service.unit",
                wanted == status.get("unit"),
                f"--service {wanted} vs unit {status.get('unit')} derived by hyprial "
                "for this home (--service is deprecated; omit it)",
            ))
        found.append((
            "service.installed",
            status.get("installed") is True,
            f"unit {status.get('unit')} installed={status.get('installed')}; the "
            "script never installs one (install it with `hyprial daemon service "
            "install` first)",
        ))
        found.append((
            "service.attributed",
            status.get("attributed") is True,
            f"unit {status.get('unit')} attributed={status.get('attributed')} "
            f"ownershipReason={status.get('ownershipReason')}",
        ))
        serving = status.get("servingPid")
        if isinstance(serving, int):
            found.append((
                "service.supervised",
                status.get("supervision") == "supervised",
                f"daemon pid {serving} answers; supervision={status.get('supervision')} "
                f"unit pid={status.get('pid')}",
            ))
        else:
            found.append(("service.supervised", None, "no daemon answers"))
        return found

    def baseline(self) -> bool:
        smoke = self.smoke
        refused = False
        for name, ok, detail in self.evaluate(smoke.baseline.get("service", {})):
            if ok is None:
                smoke.check(name, NOT_RUN, detail)
                continue
            smoke.check(name, PASS if ok else FAIL, detail)
            refused = refused or not ok
        if refused:
            smoke.refusal_code = EXIT_USAGE
            return False
        return True

    def daemon_expected(self) -> bool:
        status = self.smoke.baseline.get("service", {})
        return status.get("running") is True or isinstance(status.get("servingPid"), int)

    def pre_stop_problems(self) -> list[str]:
        status = self.snapshot("service-status-pre-stop")
        if status is None:
            return ["daemon service status gave no status object"]
        problems = [detail for _, ok, detail in self.evaluate(status) if ok is False]
        identity = self.identity_problem(status)
        if identity is not None:
            problems.append(identity)
        return problems

    def main_pid(self) -> Any:
        status = self.snapshot("service-status-gui")
        return status.get("pid") if status else None

    def target(self, status: dict[str, Any]) -> tuple[int, str | None] | None:
        pid = status.get("servingPid") or status.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return None
        seen = self.smoke.process.observe(pid)
        return pid, seen.birth if seen.status == "alive" else None

    def proven_stopped(
        self, label: str, target: tuple[int, str | None] | None
    ) -> tuple[bool, str]:
        smoke = self.smoke
        status = self.snapshot(f"service-status-{label}")
        if status is None:
            return False, "service status unreadable"
        problem = self.identity_problem(status)
        if problem is not None:
            return False, problem
        if not stopped_status(status):
            return False, f"running={status.get('running')} pid={status.get('pid')}"
        payload, error = smoke.daemon_status(label=f"daemon-status-{label}")
        if payload is None:
            return False, error or "daemon status unreadable"
        if daemon_fields(payload).get("running") is True:
            return False, "a daemon still answers"
        if target is None:
            return True, "not running, no pid, no daemon answers"
        gone = smoke.process.gone(*target)
        if gone is None:
            return False, f"cannot tell whether pid {target[0]} is gone"
        if not gone:
            return False, f"pid {target[0]} is still alive"
        return True, f"not running, no pid, no daemon answers, pid {target[0]} gone"

    def stop(self, label: str = "service.stop", *, rollback: bool = False) -> bool:
        smoke = self.smoke
        result, status = self.read("service-status-before-stop")
        if not isinstance(status, dict):
            if rollback and not smoke.candidate_started:
                return self.raw_stop()
            if rollback and smoke.candidate_started:
                smoke.check(
                    label,
                    FAIL,
                    f"service status rc={result.returncode} timed_out={result.timed_out} gave no JSON object "
                    f"after candidate start was attempted; refusing to call raw_stop on baseline unit {self.unit!r} "
                    "or declare rollback stopped -- inspect and stop any running daemon/unit manually",
                )
                return False
            smoke.check(
                label, FAIL, f"service status rc={result.returncode} gave no JSON object"
            )
            return False
        if status.get("ok") is not True:
            smoke.check(
                label,
                FAIL,
                f"service status refused before the stop: {status.get('code')} "
                f"{str(status.get('error'))[:300]}",
            )
            return False
        problem = self.identity_problem(status)
        if problem is not None:
            if rollback and not smoke.candidate_started:
                return self.raw_stop()
            if rollback and smoke.candidate_started:
                smoke.check(
                    label,
                    FAIL,
                    f"{problem}; candidate start was already attempted, refusing to declare rollback stopped while observed unit {status.get('unit')!r} (pid={status.get('pid')}, servingPid={status.get('servingPid')}) may still be running -- inspect and stop both baseline unit {self.unit!r} and observed unit {status.get('unit')!r} manually",
                    {"status": status},
                )
                return False
            smoke.check(label, FAIL, problem)
            return False
        target = self.target(status)
        self.stop_target = target
        if stopped_status(status):
            done, detail = self.proven_stopped("stop-0", target)
            if done:
                smoke.check(label, PASS, f"already stopped: {detail}")
                return True
        result = smoke.run_command(
            "service-stop",
            [str(smoke.live_cli), "daemon", "stop", "--json"],
            timeout=max(smoke.options.stop_timeout, STOP_COMMAND_FLOOR_SECONDS),
            env=smoke.live_environment(),
        )
        smoke.service_logs.capture(label)
        payload = load_json(result.stdout)
        if not isinstance(payload, dict):
            if rollback and not smoke.candidate_started:
                return self.raw_stop()
            if rollback and smoke.candidate_started:
                smoke.check(
                    label,
                    FAIL,
                    f"daemon stop rc={result.returncode} timed_out={result.timed_out} gave no JSON object "
                    f"after candidate start was attempted; refusing to call raw_stop on baseline unit {self.unit!r} "
                    "or declare rollback stopped -- inspect and stop any running daemon/unit manually",
                )
                return False
            answer = f"daemon stop rc={result.returncode} gave no JSON object"
        else:
            refusal = self.stop_refusal(payload)
            if refusal is not None:
                smoke.check(label, FAIL, refusal, {"answer": payload})
                return False
            answer = (
                f"daemon stop rc={result.returncode} ok={payload.get('ok')} "
                f"code={payload.get('code')}"
            )
        deadline = smoke.clock.monotonic() + smoke.options.stop_timeout
        attempt = 0
        while True:
            attempt += 1
            done, detail = self.proven_stopped(f"stop-{attempt}", target)
            if done:
                smoke.check(label, PASS, f"{detail}; {answer}")
                return True
            if smoke.clock.monotonic() >= deadline:
                break
            smoke.clock.sleep(min(POLL_SECONDS, max(0.0, deadline - smoke.clock.monotonic())))
        smoke.check(
            label,
            FAIL,
            f"not proven stopped within {smoke.options.stop_timeout:.0f}s: {detail}; {answer}",
        )
        return False

    def stop_refusal(self, payload: dict[str, Any]) -> str | None:
        if payload.get("supervisorMayRestart") is True:
            return (
                "daemon stop reports supervisorMayRestart: a unit this CLI does not "
                f"own may relaunch the daemon ({payload.get('unattributedSupervisor')})"
            )
        if payload.get("ok") is not True:
            return None
        supervisor = payload.get("supervisor")
        expected = {"platform": self.platform, "unit": self.unit}
        if not isinstance(supervisor, dict) or {
            "platform": supervisor.get("platform"), "unit": supervisor.get("unit")
        } != expected:
            return (
                f"daemon stop did not go through this home's supervisor {expected} "
                f"(answer supervisor={supervisor}); the manager may relaunch the daemon"
            )
        return None

    def raw_stop(self) -> bool:
        smoke = self.smoke
        if self.platform == "systemd" and self.unit:
            argv = ["systemctl", "--user", "stop", self.unit]
            accepted: tuple[int, ...] = (0, SYSTEMD_NOT_LOADED_RC)
        elif self.platform == "launchd" and self.unit:
            argv = ["launchctl", "bootout", f"gui/{os.getuid()}/{self.unit}"]
            accepted = (0,)
        else:
            smoke.check(
                "rollback.raw-stop",
                FAIL,
                f"the live CLI cannot answer and no baseline unit is known "
                f"(platform={self.platform} unit={self.unit})",
            )
            return False
        result = smoke.run_command(
            "rollback-raw-stop",
            argv,
            timeout=max(smoke.options.stop_timeout, STOP_COMMAND_FLOOR_SECONDS),
        )
        smoke.service_logs.capture("rollback.raw-stop")
        ok = result.returncode in accepted and not result.timed_out
        smoke.pass_if(
            "rollback.raw-stop",
            ok,
            f"the live CLI could not answer; stopped baseline unit {self.unit} "
            f"directly: {' '.join(argv)} rc={result.returncode}",
            f"raw stop of baseline unit {self.unit} failed: {' '.join(argv)} "
            f"rc={result.returncode} {result.stderr.strip()[:300]}",
        )
        return ok

    def start(self, label: str, *, allow_running: bool = False) -> bool:
        smoke = self.smoke
        smoke.start_payload = None
        smoke.start_issued_at = smoke.clock.monotonic()
        result = smoke.run_command(
            label,
            [str(smoke.live_cli), "daemon", "start", "--json"],
            timeout=max(smoke.options.ready_timeout, START_COMMAND_FLOOR_SECONDS),
            env=smoke.live_environment(),
        )
        smoke.service_logs.capture(label)
        payload = load_json(result.stdout)
        answer = {"answer": payload} if isinstance(payload, dict) else {}
        code = payload.get("code") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and code in LAUNCHED_NOT_CONFIRMED:
            return self.launched_not_confirmed(label, payload)
        if result.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
            smoke.check(
                label,
                FAIL,
                f"daemon start rc={result.returncode} code={code} "
                f"{str(payload.get('error'))[:300] if isinstance(payload, dict) else ''} "
                f"{result.stderr.strip()[:300]}",
                answer,
            )
            return False
        if payload.get("alreadyRunning") is True and not allow_running:
            smoke.check(
                label,
                FAIL,
                f"daemon start answered alreadyRunning: true (pid {payload.get('pid')}): "
                "a daemon was serving although it had been stopped",
                answer,
            )
            return False
        smoke.start_payload = payload
        smoke.check(
            label,
            PASS,
            f"started={payload.get('started')} alreadyRunning={payload.get('alreadyRunning')} "
            f"pid={payload.get('pid')} version={payload.get('version')}",
            answer,
        )
        return True

    def launched_not_confirmed(self, label: str, payload: dict[str, Any]) -> bool:
        smoke = self.smoke
        said = f"start answer {payload.get('code')}: {str(payload.get('error'))[:400]}"
        status = self.snapshot(f"service-status-{label}")
        data: dict[str, Any] = {"answer": payload, "status": status}
        problem = self.identity_problem(status) if status is not None else None
        if problem is not None:
            smoke.check(label, FAIL, f"{said}; {problem}", data)
            return False
        if status is None or status.get("running") is not True or not isinstance(
            status.get("pid"), int
        ):
            smoke.check(
                label,
                FAIL,
                f"{said}; and the unit is not running for this home "
                f"({describe(status) if status else 'status unreadable'}): nothing to wait for",
                data,
            )
            return False
        smoke.check(
            label,
            PASS,
            f"{said}; the unit runs (pid {status.get('pid')}, servingPid "
            f"{status.get('servingPid')}, supervision {status.get('supervision')}): "
            f"waiting for readiness within --ready-timeout "
            f"{smoke.options.ready_timeout:g}s of the start command",
            data,
        )
        return True

    def ready_gate(self) -> str | None:
        return None

    def custody(self, pid: Any, label: str) -> tuple[bool, str]:
        status = self.snapshot(label)
        if status is None:
            return False, "service status unreadable"
        detail = f"{describe(status)} daemon_pid={pid}"
        problem = self.identity_problem(status)
        if problem is not None:
            return False, f"{problem}; {detail}"
        ok = (
            status.get("supervision") == "supervised"
            and status.get("pid") == pid
            and status.get("servingPid") == pid
        )
        return ok, detail

    def ready_problem(self, pid: Any) -> str | None:
        ok, detail = self.custody(pid, "service-status-ready")
        return None if ok else f"not supervised by this home's unit: {detail}"

    def post_service(self, pid: Any) -> tuple[bool, str]:
        return self.custody(pid, "service-status-post")

    def attributed_problem(self, label: str) -> str | None:
        status = self.snapshot(f"service-status-{label}")
        if status is None:
            return "the live CLI gave no service status object"
        problem = self.identity_problem(status)
        if problem is not None:
            return problem
        if status.get("installed") is not True or status.get("attributed") is not True:
            return (
                f"the live CLI does not attribute unit {status.get('unit')}: "
                f"installed={status.get('installed')} attributed={status.get('attributed')} "
                f"ownershipReason={status.get('ownershipReason')}"
            )
        return None

    def settle_after_failed_stop(self) -> str | None:
        smoke = self.smoke
        deadline = smoke.clock.monotonic() + RESTORE_SETTLE_SECONDS
        attempt = 0
        while True:
            attempt += 1
            status = self.snapshot(f"service-status-settle-{attempt}")
            if status is not None and self.identity_problem(status) is None:
                if status.get("running") is True and status.get("supervision") == "supervised":
                    return "running"
                if stopped_status(status):
                    done, _ = self.proven_stopped(f"settle-{attempt}", self.stop_target)
                    if done:
                        return "stopped"
            if smoke.clock.monotonic() >= deadline:
                return None
            smoke.clock.sleep(min(POLL_SECONDS, max(0.0, deadline - smoke.clock.monotonic())))


class LegacySystemdControl:
    """The pre-seam ``systemctl --user`` control, unchanged (owner Q5).

    The unit may carry unrelated secrets in ``Environment=``.  Only the
    service-state keys are evidence; ``service_show`` keeps the unit
    environment out of the logs.
    """

    name = LEGACY

    def __init__(self, smoke: Any):
        self.smoke = smoke
        self.unit = smoke.options.service or LEGACY_UNIT

    def unit_name(self) -> str:
        return self.unit

    def service_show(self) -> tuple[dict[str, str], CommandResult]:
        smoke = self.smoke
        result = smoke.runner.run(
            [
                "systemctl",
                "--user",
                "show",
                self.unit,
                "--property=ActiveState,SubState,MainPID,NRestarts,Result,Environment",
            ],
            timeout=15,
        )
        properties = parse_systemd_properties(result.stdout)
        logged = dataclasses.replace(
            result,
            stdout="\n".join(
                f"{key}={properties[key]}"
                for key in ("ActiveState", "SubState", "MainPID", "NRestarts", "Result")
                if key in properties
            ),
        )
        smoke.recorder.command("service-show", logged)
        return properties, result

    def baseline(self) -> bool:
        smoke = self.smoke
        systemctl = smoke.run_command(
            "systemctl-version", ["systemctl", "--user", "--version"], timeout=15
        )
        smoke.pass_if(
            "toolchain.systemctl",
            systemctl.returncode == 0,
            "systemctl --user available",
            f"systemctl failed: {systemctl.stderr.strip()[:300]}",
        )
        properties, show_result = self.service_show()
        smoke.service_properties = properties
        if show_result.returncode != 0:
            smoke.check(
                "baseline.service",
                FAIL,
                f"systemctl show rc={show_result.returncode}: "
                f"{show_result.stderr.strip()[:300]}",
            )
            return False
        smoke.baseline["service"] = {
            key: properties.get(key)
            for key in ("ActiveState", "SubState", "MainPID", "NRestarts", "Result")
        }
        smoke.check(
            "baseline.service",
            PASS,
            f"ActiveState={properties.get('ActiveState', 'unknown')} "
            f"SubState={properties.get('SubState', 'unknown')} "
            f"MainPID={properties.get('MainPID', '0')}",
            smoke.baseline["service"],
        )
        unit_env = parse_environment(properties.get("Environment", ""))
        smoke.unit_environment = {
            key: unit_env[key] for key in ("HYPRIAL_HOME", "HARNESS_STATE_DIR") if key in unit_env
        }
        smoke.home = smoke._resolve_home(unit_env)
        smoke.state_dir = smoke._resolve_state_dir(unit_env)
        for option, key, name in (
            ("home", "HYPRIAL_HOME", "baseline.unit-home"),
            ("state_dir", "HARNESS_STATE_DIR", "baseline.unit-state"),
        ):
            given = getattr(smoke.options, option)
            if given and key in unit_env:
                configured = str(os.path.expanduser(unit_env[key]))
                if str(given) != configured:
                    smoke.check(name, FAIL, f"--{option.replace('_', '-')} {given} != unit {key} {configured}")
        return True

    def daemon_expected(self) -> bool:
        return self.smoke.service_properties.get("ActiveState") in ("active", "activating")

    def pre_stop_problems(self) -> list[str]:
        fresh, _ = self.service_show()
        payload, _ = self.smoke.daemon_status(label="pre-stop-daemon")
        fields = daemon_fields(payload or {})
        if fields.get("running") is True and (
            fresh.get("ActiveState") != "active"
            or (
                isinstance(fields.get("pid"), int)
                and fresh.get("MainPID") not in (None, "", str(fields["pid"]))
            )
        ):
            return [
                "daemon is running outside the service MainPID; refusing to "
                "replace the tool environment under an untracked process"
            ]
        return []

    def main_pid(self) -> Any:
        properties, _ = self.service_show()
        return properties.get("MainPID")

    def stop(self, label: str = "service.stop", *, rollback: bool = False) -> bool:
        smoke = self.smoke
        properties, _ = self.service_show()
        if service_stopped(properties):
            smoke.check(label, PASS, "already stopped")
            return True
        result = smoke.run_command(
            "service-stop",
            ["systemctl", "--user", "stop", self.unit],
            timeout=smoke.options.stop_timeout + 15,
        )
        smoke.service_logs.capture(label)
        deadline = smoke.clock.monotonic() + smoke.options.stop_timeout
        last: dict[str, str] = {}
        while smoke.clock.monotonic() < deadline:
            last, _ = self.service_show()
            if service_stopped(last):
                smoke.check(label, PASS, f"{last.get('ActiveState')} after rc={result.returncode}")
                return True
            smoke.clock.sleep(0.5)
        smoke.check(label, FAIL, f"service did not stop; last={last} stop_rc={result.returncode}")
        return False

    def start(self, label: str, *, allow_running: bool = False) -> bool:
        smoke = self.smoke
        smoke.start_issued_at = None
        result = smoke.run_command(
            label, ["systemctl", "--user", "start", self.unit], timeout=60
        )
        smoke.service_logs.capture(label)
        deadline = smoke.clock.monotonic() + 30
        last: dict[str, str] = {}
        while smoke.clock.monotonic() < deadline:
            last, _ = self.service_show()
            if last.get("ActiveState") == "active" and last.get("SubState") == "running":
                smoke.check(label, PASS, f"active/running; start_rc={result.returncode}")
                return True
            smoke.clock.sleep(0.5)
        smoke.check(
            label,
            FAIL,
            f"service did not become active/running; last={last} start_rc={result.returncode}",
        )
        return False

    def ready_gate(self) -> str | None:
        properties, _ = self.service_show()
        if properties.get("ActiveState") != "active" or properties.get("SubState") != "running":
            return f"service is {properties.get('ActiveState')}/{properties.get('SubState')}"
        return None

    def ready_problem(self, pid: Any) -> str | None:
        return None

    def post_service(self, pid: Any) -> tuple[bool, str]:
        properties, _ = self.service_show()
        main_pid = properties.get("MainPID")
        ok = (
            properties.get("ActiveState") == "active"
            and properties.get("SubState") == "running"
            and (pid is None or main_pid == str(pid))
        )
        return ok, (
            f"ActiveState={properties.get('ActiveState')} "
            f"SubState={properties.get('SubState')} MainPID={main_pid} daemon_pid={pid}"
        )

    def attributed_problem(self, label: str) -> str | None:
        return None

    def settle_after_failed_stop(self) -> str | None:
        return "stopped"


class ServiceMixin:
    def service_control_name(self) -> str | None:
        return self.service.name if self.service is not None else None

    def service_unit(self) -> str | None:
        if self.service is not None and self.service.unit_name():
            return self.service.unit_name()
        return self.options.service

    def autostart_refusal(self) -> str | None:
        if os.environ.get("HYPRIAL_DAEMON_AUTOSTART") == "0":
            return (
                "HYPRIAL_DAEMON_AUTOSTART=0 is set: `hyprial daemon start` would "
                "bypass the service and spawn a detached daemon; unset it"
            )
        return None

    def select_service(self, label: str) -> int | None:
        control = HyprialServiceControl(self)
        result, payload = control.read(label)
        kind = classify_status(result, payload)
        if kind == HYPRIAL:
            self.service = control
            self.baseline["service"] = payload
            self.check("service.control", PASS, "hyprial daemon service status / daemon stop / daemon start")
            self.check("baseline.service", PASS, describe(payload), payload)
            return None
        if kind == LEGACY:
            reason = (
                "the baseline CLI's `daemon service status` has no supervision/attributed "
                f"(rc={result.returncode})"
            )
            if sys.platform.startswith("linux"):
                self.service = LegacySystemdControl(self)
                self.check("service.control", PASS, f"legacy systemctl --user: {reason}")
                return None
            self.check(
                "service.control",
                FAIL,
                f"{reason}; there is no legacy path on {sys.platform}: upgrade the "
                "baseline wheel first",
            )
            return EXIT_USAGE
        if kind == "error":
            code = payload.get("code")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            detail = f"{code}: {str(payload.get('error'))[:300]}"
            if code == "SERVICE_PLATFORM_UNSUPPORTED":
                self.check("baseline.service", BLOCKED, f"{detail} platform={data.get('platform')}")
                return EXIT_BLOCKED
            if code == "SERVICE_OWNERSHIP_REFUSED":
                self.check(
                    "baseline.service",
                    FAIL,
                    f"{detail} ownershipReason={data.get('ownershipReason')}",
                )
                return EXIT_USAGE
            self.check("baseline.service", FAIL, f"{detail} data={data}")
            return EXIT_FAILED
        self.check(
            "baseline.service",
            FAIL,
            f"daemon service status rc={result.returncode} gave no JSON object: "
            f"{result.stderr.strip()[:300]}",
        )
        return EXIT_FAILED

    def service_pre_stop(self) -> int | None:
        if self.service is None:
            code = self.select_service("service-status-pre-stop-select")
            if code is not None:
                self.result = {EXIT_USAGE: "ABORTED", EXIT_BLOCKED: "BLOCKED"}.get(code, "FAILED")
                return code
        problems = self.service.pre_stop_problems()
        if problems:
            self.check("apply.pre-stop", FAIL, "; ".join(problems))
            self.result = "ABORTED"
            return EXIT_USAGE
        self.service_logs.mark(self.state_dir)
        return None
