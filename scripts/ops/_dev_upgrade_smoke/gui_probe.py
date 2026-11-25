"""Pre-stop GUI runtime probe of ``apply --gui``.

Decisions (owner, 2026-11-21, after PR-3 found no read-only product query):
- With ``--gui`` and before ``service.stop`` the INSTALLED CLI answers the one
  question "does a foreground GUI already hold this home's runtime": the script
  starts ``hyprial gui --no-open --json --port 0 --stdin-stop`` as its child
  (the same spawn seam and environment as the post-upgrade GUI check, without a
  grant override, so the product default applies), reads the first stdout line
  and stops the child at once (stdin close, then SIGTERM, then SIGKILL to its
  group).  The script carries no rule of its own: the answer is the product's
  ``GUI_RUNTIME_BUSY``.  The probe is not read-only: it starts a GUI host for a
  moment and the product writes a one-time grant file, which is never read.
- Answers (``gui.runtime-free``):
  - ``GUI_RUNTIME_BUSY`` -> FAIL, refused with exit 2 / ``ABORTED`` before the
    stop, no upgrade and no rollback; the detail names the home and the runtime
    directory from the record.
  - a record that passes ``validate_gui_record`` -> PASS, the upgrade proceeds.
  - anything else (an old CLI without ``--stdin-stop``, no record in time, a
    child that cannot start, another failure code, a record that fails
    validation) -> NOT_RUN with the reason; it answers nothing and does not
    block.  The post-upgrade GUI check stays the fallback (it rolls back on a
    busy runtime).
- A probe child still alive after SIGKILL (``gui.probe-stop``), or a target
  daemon changed across the probe (``gui.probe-daemon-identity``, the same
  pid/socket/unit-pid comparison as ``gui.daemon-identity``), refuses before the
  stop as well: no upgrade with a stray GUI holding the lock or with another
  daemon.
- No running target daemon before the probe: ``hyprial gui`` would start one,
  so the probe is NOT_RUN and the upgrade proceeds.
- The probe's stderr goes to ``<artifacts>/gui-probe-<epoch>.err.log``; when that
  file exists the run is refused before the stop (a reused artifacts dir).
- ``preflight`` does not probe: it is documented read-only and has no ``--gui``.
- Probe facts are kept in ``baseline.guiProbe``.
"""

from __future__ import annotations

from typing import Any

from .common import (
    EXIT_USAGE,
    FAIL,
    NOT_RUN,
    PASS,
    safe_gui_summary,
    validate_gui_record,
)


class GuiProbeMixin:
    def refuse_probe(self, name: str, detail: str) -> int:
        self.check(name, FAIL, detail)
        self.result = "ABORTED"
        return EXIT_USAGE

    def probe_gui_runtime(self) -> int | None:
        if not self.options.gui:
            return None
        before = self.read_target_daemon("gui-probe-pre-daemon")
        if before is None:
            self.check(
                "gui.runtime-free",
                NOT_RUN,
                "no running target daemon to keep: hyprial gui would start one, "
                "so the runtime was not probed",
            )
            return None
        stderr_log = self.artifacts_dir / f"gui-probe-{int(self.clock.wall())}.err.log"
        if stderr_log.exists():
            return self.refuse_probe(
                "gui.runtime-free",
                f"{stderr_log} already exists (left by an earlier run); use a "
                "fresh --artifacts-dir",
            )
        command = self.gui_command(port=0)
        probe: dict[str, Any] = {"command": command, "stderrLog": str(stderr_log)}
        self.baseline["guiProbe"] = probe
        self.gui_lines = []
        try:
            child = self.runner.spawn(
                command, env=self.gui_environment(grant=False), stderr_path=stderr_log
            )
        except OSError as error:
            self.check(
                "gui.runtime-free", NOT_RUN, f"probe not started: {error}; not answered"
            )
            return None
        probe["pid"] = child.pid
        try:
            payload, problem = self.read_gui_record(child, stderr_log)
        finally:
            code, method, _ = self.end_child(child, "gui-probe", command, str(stderr_log))
        probe.update({"exitCode": code, "stopMethod": method})
        if payload is not None:
            probe["startup"] = safe_gui_summary(payload)
        if code is None:
            kill = f"kill -KILL -- -{child.pid}"
            probe["stopCommand"] = kill
            return self.refuse_probe(
                "gui.probe-stop",
                f"probe GUI process group {child.pid} is still alive after SIGKILL "
                f"and may hold the runtime lock; stop it manually: {kill}",
            )
        self.check("gui.probe-stop", PASS, f"probe GUI ended ({method}, rc={code})")
        after = self.target_daemon("gui-probe-post-daemon")
        if after != before:
            return self.refuse_probe(
                "gui.probe-daemon-identity",
                f"target daemon changed across the GUI probe: before={before} "
                f"after={after}; not stopping it",
            )
        self.check(
            "gui.probe-daemon-identity", PASS, f"target daemon unchanged by the probe: {before}"
        )
        return self.answer_probe(payload, problem)

    def answer_probe(self, payload: dict[str, Any] | None, problem: str) -> int | None:
        if payload is None:
            self.check("gui.runtime-free", NOT_RUN, f"probe gave no answer: {problem}")
            return None
        if payload.get("code") == "GUI_RUNTIME_BUSY":
            data = payload.get("data")
            runtime = data.get("runtime") if isinstance(data, dict) else None
            return self.refuse_probe(
                "gui.runtime-free",
                f"GUI_RUNTIME_BUSY: another foreground GUI holds the runtime "
                f"{runtime} of home {self.home}; nothing was stopped or installed. "
                "Stop that GUI, or run without --gui",
            )
        problems = validate_gui_record(payload, 0)
        if problems:
            self.check(
                "gui.runtime-free",
                NOT_RUN,
                "probe gave no answer: " + "; ".join(problems)[:400],
            )
            return None
        self.check(
            "gui.runtime-free", PASS, f"runtime of home {self.home} is free (probe stopped)"
        )
        return None
