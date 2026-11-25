"""What an upgrade left behind when it could not bring the daemon back.

On 2026-08-31 an autoupdate installed cleanly, stopped the running daemon, and
never started a replacement. Production was down for 56 minutes and nothing
recorded why where a person would look. This module writes that fact to disk
(``upgrade-restart-failed.json``) before anything else, renders the restart
failure detail, and runs the installed build's self-check.

R2 (owner ruling 2026-11-19): nothing is sent to any IM any more -- no owner DM
and no adapter default-route post.  The failure is in the upgrade result, its
exit code, this marker file and the daemon log (``daemon.start.failed``).  An
IM forward of daemon-log events may come later; it is not here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable



MARKER_FILENAME = "upgrade-restart-failed.json"

# ⭐ What the upgrade actually did. Named states rather than a bool, because a
# bool was forced to answer two questions at once ("did anything install?" and
# "did it succeed?") and got the common case wrong in both directions -- see
# `notify_upgrade_outcome`. Naming the states is what lets the declined
# downgrade be reported as itself instead of borrowing another state's words.
UPGRADE_INSTALLED = "installed"
UPGRADE_ALREADY_CURRENT = "already-current"
UPGRADE_DECLINED_DOWNGRADE = "declined-downgrade"
# ⭐ Installed, and the daemon has not yet confirmed it is ready. Neither
# success nor failure: reporting it as either one states something we do not
# know. Since card 3c116ad2 the restart path produces it again -- the launch
# proves only the serving boundary, so the flow reads ping's phase once and
# records unconfirmed whenever restore has not settled (phase != reconciled)
# -- and the follow-up poll closes the state from the same CLI process.
UPGRADE_UNCONFIRMED = "unconfirmed"
#: Installed by the timer and deliberately NOT restarted: the owner restarts
#: with ``hyprial daemon upgrade apply`` (Allen 2026-09-23: "改为用户手动回复重启
#: 才重启"; the reply path is a command he or an agent runs).
UPGRADE_AWAITING_RESTART = "awaiting-restart"
UPGRADE_FAILED = "failed"



@dataclass(frozen=True)
class StartFailureRecord:
    """One structured startup failure from the daemon's durable log."""

    timestamp: datetime
    phase: str
    error_type: str
    error: str


class RestartProcessState(StrEnum):
    """The three honest outcomes of the PID-and-birth observation."""

    RUNNING = "running"
    EXITED = "exited"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RestartProcessObservation:
    """Current state of the exact daemon process started by this restart."""

    pid: int | None
    state: RestartProcessState


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _start_failure_from_entry(entry: object) -> StartFailureRecord | None:
    if not isinstance(entry, dict) or entry.get("event") != "daemon.start.failed":
        return None
    timestamp = _timestamp(entry.get("ts"))
    if timestamp is None:
        return None

    def field(name: str) -> str:
        value = entry.get(name)
        return value if isinstance(value, str) and value else "unknown"

    return StartFailureRecord(
        timestamp=timestamp,
        phase=field("phase"),
        error_type=field("errorType"),
        error=field("error"),
    )


def latest_start_failure(
    log_path: Path, *, started_at: datetime
) -> StartFailureRecord | None:
    """Return the newest ``daemon.start.failed`` from this launch attempt.

    ``started_at`` is captured by the upgrade process immediately before it
    invokes the launcher.  That parent-side wall-clock reading precedes the
    child process and therefore excludes every prior daemon generation without
    requiring the failed daemon to have reached a correlation-id write point.
    Invalid or partial JSONL records do not become evidence.
    """

    latest: StartFailureRecord | None = None
    try:
        stream = Path(log_path).open("r", encoding="utf-8")
    except OSError:
        return None
    with stream:
        for line in stream:
            try:
                entry: Any = json.loads(line)
            except (UnicodeError, ValueError, RecursionError):
                continue
            candidate = _start_failure_from_entry(entry)
            if candidate is None or candidate.timestamp < started_at:
                continue
            if latest is None or candidate.timestamp >= latest.timestamp:
                latest = candidate
    return latest


def start_failure_line(start_failure: StartFailureRecord | None) -> str:
    """Render this launch's startup-failure diagnosis, or say there is none.

    ⭐ One renderer, two readers: the owner alert (:func:`restart_failure_detail`)
    and the durable failure marker (:func:`write_failure_marker`). Before this
    the alert named the cause and the marker did not, so the two told the same
    event in two different amounts -- and the one that survived on disk was the
    one without the cause. Sharing the sentence is what keeps them from drifting
    apart again.

    ⛔ Absence is stated, never omitted: "we found no record" and "this line was
    left out" must not read the same. The latter is the defect this replaced.
    """

    if start_failure is None:
        return "本次启动: 日志里还没有 start.failed 记录"
    return (
        "本次 daemon.start.failed: "
        f"phase={start_failure.phase} "
        f"errorType={start_failure.error_type} "
        f"error={start_failure.error}"
    )


def restart_failure_detail(
    *,
    error: str,
    start_failure: StartFailureRecord | None,
    process: RestartProcessObservation,
) -> str:
    """Render the failure cause and current process state without inference."""

    failure_line = start_failure_line(start_failure)

    if process.state is RestartProcessState.RUNNING and process.pid is not None:
        process_line = (
            f"进程状态: pid {process.pid} 仍在(可能在失败后的 shutdown 中)"
        )
    elif process.state is RestartProcessState.EXITED and process.pid is not None:
        process_line = f"进程状态: pid {process.pid} 已退出"
    elif process.pid is not None:
        process_line = f"进程状态: pid {process.pid} 观测失败"
    else:
        process_line = "进程状态: 观测失败(启动错误没有提供 pid)"
    return f"daemon restart failed: {error}\n{failure_line}\n{process_line}"


class _StartFailureNotApplicable:
    """Sentinel: this marker has no start-failure dimension at all.

    ⭐ The field has **three** states, and collapsing any two of them is a lie
    about a search:

      * a ``StartFailureRecord`` -- this launch's ``daemon.start.failed`` was
        found in the log;
      * ``None``                  -- we looked and there is no such record
        (the restart-failure path);
      * this sentinel             -- the event is not a start failure, so the
        question does not apply (the survivor path).

    ⛔ Writing ``null`` for the third state asserts "we looked and found
    nothing" about a search nobody ran. Its key is omitted instead, and the
    omission is the honest record. This is why a bare default cannot be
    ``None``: a caller who names nothing has not answered the question, and
    the default must say so rather than answer it for them.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid only
        return "START_FAILURE_NOT_APPLICABLE"


START_FAILURE_NOT_APPLICABLE = _StartFailureNotApplicable()


def write_failure_marker(
    *,
    state_dir: Path,
    host: str,
    summary: str,
    detail: str,
    start_failure: (
        StartFailureRecord | None | _StartFailureNotApplicable
    ) = START_FAILURE_NOT_APPLICABLE,
) -> Path:
    """Put the failure on disk where an operator looks for it.

    On 2026-08-31 the fact sat in the autoupdate status for 56 minutes and
    nobody looked; this file is the durable record the upgrade result and
    ``hyprial daemon upgrade status`` point at.  R2: it is the record, nothing
    is sent.

    Raising here is acceptable and deliberate: if we cannot even write a local
    file, the caller should hear about it.
    """

    path = Path(state_dir) / MARKER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "kind": "upgrade-restart-failed",
        "host": host,
        "summary": summary,
        "detail": detail,
        "recordedAtEpoch": int(time.time()),
        "pid": os.getpid(),
    }
    # ⭐ The diagnosis, not only the symptom. `detail` carries the same sentence
    # `start_failure_line` renders; this is the machine-readable half for a
    # reader that should not have to parse prose. Before this field the marker
    # said only "daemon restart failed: <timeout>" and the cause
    # (`daemon.start.failed` phase/errorType/error) lived exclusively in
    # daemon.jsonl -- so whoever read the marker had to already know to look
    # somewhere else, which is exactly what an unattended failure cannot assume.
    #
    # ⚠️ `None` (passed explicitly) means "we looked and no start.failed record
    # was found for this restart", and it is written out rather than omitted so
    # that meaning is readable. `{}` would be a third, dishonest shape: present
    # and unreadable. What None means is spelled out by the same sentence that
    # lands in `detail` -- a marker that carries None and no such sentence
    # would be the silent omission this field exists to prevent.
    #
    # ⛔ The key is **omitted** when the caller did not pass `start_failure` at
    # all: that means the event has no start-failure dimension (the survivor
    # path), and writing `null` there would assert a search that never ran.
    if not isinstance(start_failure, _StartFailureNotApplicable):
        payload["startFailure"] = (
            None
            if start_failure is None
            else {
                "phase": start_failure.phase,
                "errorType": start_failure.error_type,
                "error": start_failure.error,
            }
        )
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    return path


@dataclass(frozen=True)
class SelfCheckResult:
    """Did the build we just installed actually work?"""

    ok: bool
    detail: str

    def to_json(self) -> dict[str, object]:
        return {"ok": self.ok, "detail": self.detail}


def installed_executable() -> Path:
    """The `hyprial` the *next* process will run -- not this one's modules.

    ⚠️ This process holds the pre-upgrade code in memory (`autoupdate` preloads
    it deliberately, so the restart survives the distribution being replaced).
    Checking `import hyprial` from in here would therefore check the **old** build
    and pass no matter how broken the new one is: the check and the thing being
    checked would be different artefacts wearing the same name.
    """

    return Path(sys.executable).with_name("hyprial")


def run_self_check(
    *,
    executable: Path | None = None,
    timeout: float = 30.0,
    runner: Callable[[list[str]], "subprocess.CompletedProcess[str]"] | None = None,
) -> SelfCheckResult:
    """Ask the freshly installed build to answer one question about itself.

    On 2026-08-31 an upgrade installed cleanly and then died with
    `cannot import name 'updates' from 'hyprial'` -- the artefact was on disk and
    unusable, and nothing looked at it. So the check is deliberately the
    shallowest thing that would have caught that: run the installed binary and
    require a well-formed answer.

    ⚠️ It never raises. A self-check that can fail an upgrade would turn "we
    could not verify" into "the upgrade failed", and those are different facts
    with different responses -- the upgrade did complete, and saying otherwise
    would send someone to re-run an install that already worked.
    """

    binary = installed_executable() if executable is None else executable
    command = [str(binary), "version", "--json"]
    try:
        completed = (
            subprocess.run(
                command, text=True, capture_output=True, timeout=timeout, check=False
            )
            if runner is None
            else runner(command)
        )
    except Exception as error:  # noqa: BLE001 -- see the docstring
        return SelfCheckResult(False, f"could not run {binary}: {error}")
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip()[-400:]
        return SelfCheckResult(
            False, f"`hyprial version` exited {completed.returncode}: {tail}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return SelfCheckResult(False, f"`hyprial version` printed non-JSON: {error}")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return SelfCheckResult(False, f"`hyprial version` answered {completed.stdout[:200]}")
    version = payload.get("packageVersion")
    if not isinstance(version, str) or not version:
        return SelfCheckResult(False, "`hyprial version` reported no packageVersion")
    return SelfCheckResult(True, f"hyprial {version} answers `version --json`")


