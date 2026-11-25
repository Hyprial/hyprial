"""Shared state, checks, and the live CLI environment.

Decisions (plan section 4.1, owner answers Q3 and Q6, 2026-11-20):
- Every live CLI command gets one hardened environment
  (``live_environment``): the target ``HYPRIAL_HOME`` / ``HARNESS_STATE_DIR``,
  ``PATH`` with the resolved live CLI's directory first (``--bin-dir`` or the
  fallback ``<tool-dir>/bin`` when the shim is absent, so the CLI's own
  ``shutil.which("hyprial")`` names this installation and attribution matches
  the unit while ``UV_TOOL_BIN_DIR`` keeps ``--bin-dir``), and without
  ``HYPRIAL_DAEMON_AUTOSTART``, ``HYPRIAL_SERVICE_AUTOINSTALL``,
  ``HARNESS_SOCKET_PATH``, ``HYPRIAL_OWNER`` and ``HYPRIAL_NODE_ID``, so a start
  can neither install a unit nor bypass it, and no other socket, owner or node
  is addressed.  The product's units set none of these, so the supervised
  daemon resolves socket, owner and node from the home (``<home>/node-id``);
  an ambient ``HYPRIAL_NODE_ID`` made the CLI disagree with it
  (``GUI_DAEMON_IDENTITY``, mismatch ``node``, real macOS drill 2026-11-21).
  ``droppedEnvironment`` in the summary names each dropped variable that was
  set in the caller's environment, never its value.
- A ``uv-receipt.toml`` ``python`` that is not an existing absolute path is the
  request string given to ``uv`` (``"3.12"``), not an interpreter; the tool
  environment's own ``bin/python`` is used instead.
- The summary schema is ``hyprial-dev-upgrade-smoke/v2``: ``service`` is the unit
  hyprial derives for the home, ``serviceControl`` names the control
  (``hyprial`` or ``legacy-systemd``), ``baseline.service`` is the
  ``hyprial daemon service status --json`` object (the systemctl properties on
  the legacy control), and ``serviceLogs`` lists the copied service log windows.
- A refusal found while preparing sets ``refusal_code`` (exit 2 usage/safety,
  exit 4 blocked); the dispatcher ends the run with it.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import socket
from pathlib import Path
from typing import Any, Sequence

from .common import (
    BLOCKED,
    EXIT_BLOCKED,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_ROLLBACK_FAILED,
    EXIT_USAGE,
    FAIL,
    NOT_RUN,
    PASS,
    SCHEMA,
    Check,
    Clock,
    CommandResult,
    Recorder,
    Runner,
    absolute_path,
    build_install_command,
    load_json,
    receipt_python,
    utc_now,
)
from .logs import ServiceLogs
from .process import ProcessProbe

LIVE_ENV_DROPPED = (
    "HYPRIAL_DAEMON_AUTOSTART",
    "HYPRIAL_SERVICE_AUTOINSTALL",
    "HARNESS_SOCKET_PATH",
    "HYPRIAL_OWNER",
    "HYPRIAL_NODE_ID",
)


class SmokeBase:
    def __init__(self, options: argparse.Namespace, runner: Runner | None = None):
        self.options = options
        self.runner = runner or Runner()
        self.clock = Clock()
        for name in ("home", "state_dir"):
            if getattr(options, name, None):
                setattr(options, name, absolute_path(getattr(options, name)))
        self.hostname = socket.gethostname()
        if options.artifacts_dir is None:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            options.artifacts_dir = Path.home() / "hyprial-dev-upgrade-smoke" / stamp
        self.artifacts_dir = Path(options.artifacts_dir).expanduser().resolve()
        self.artifacts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.recorder = Recorder(self.artifacts_dir)
        self.checks: list[Check] = []
        self.commands: list[dict[str, Any]] = []
        self.new_info: dict[str, Any] = {}
        self.old_info: dict[str, Any] = {}
        self.new_problems: list[str] = []
        self.old_problems: list[str] = []
        self.baseline: dict[str, Any] = {}
        self.isolated: dict[str, Any] = {}
        self.gui: dict[str, Any] = {}
        self.gui_left: int | None = None
        self.gui_lines: list[str] = []
        self.rollback: dict[str, Any] = {}
        self.service_properties: dict[str, str] = {}
        self.unit_environment: dict[str, str] = {}
        self.service: Any = None
        self.refusal_code: int | None = None
        self.start_payload: dict[str, Any] | None = None
        self.start_issued_at: float | None = None
        self.python_tried: list[str] = []
        self.process = ProcessProbe(self.run_command)
        self.service_logs = ServiceLogs(self.recorder.logs)
        self.tool_dir: Path | None = None
        self.bin_dir: Path | None = None
        self.live_cli: Path | None = None
        self.python: Path | None = None
        self.home: Path | None = None
        self.state_dir: Path | None = None
        self.old_wheel: Path | None = None
        self.installed_origin: Path | None = None
        self.new_wheel = Path(options.new_wheel).expanduser()
        self.wheelhouses = [Path(item).expanduser() for item in options.wheelhouse]
        self.prepared = False
        self.mutated = False
        self.candidate_started: bool = False
        self.result = "NOT_RUN"

    def check(
        self,
        name: str,
        status: str,
        detail: str,
        data: dict[str, Any] | None = None,
    ) -> Check:
        item = Check(name, status, detail, data or {})
        self.checks.append(item)
        return item

    def pass_if(self, name: str, ok: bool, ok_detail: str, fail_detail: str,
                data: dict[str, Any] | None = None) -> bool:
        self.check(name, PASS if ok else FAIL, ok_detail if ok else fail_detail, data)
        return ok

    def status(self) -> str:
        if any(item.status == FAIL for item in self.checks):
            return FAIL
        if any(item.status == BLOCKED for item in self.checks):
            return "PASS_BLOCKED"
        return PASS

    def exit_code(self) -> int:
        return EXIT_OK if self.status() != FAIL else EXIT_FAILED

    def run_command(
        self,
        label: str,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
        redact: bool = False,
    ) -> CommandResult:
        result = self.runner.run(argv, timeout=timeout, env=env)
        self.recorder.command(label, result, redact=redact)
        return result

    def live_version(self) -> tuple[dict[str, Any] | None, str | None]:
        if self.live_cli is None or not self.live_cli.exists():
            return None, f"live CLI not found: {self.live_cli}"
        result = self.run_command(
            "live-version",
            [str(self.live_cli), "version", "--json"],
            timeout=20,
            env=self.live_environment(),
        )
        if result.returncode != 0:
            return None, f"version rc={result.returncode}: {result.stderr.strip()[:300]}"
        payload = load_json(result.stdout)
        if not isinstance(payload, dict):
            return None, "version output is not a JSON object"
        return payload, None

    def daemon_status(self, *, label: str = "daemon-status") -> tuple[dict[str, Any] | None, str | None]:
        if self.live_cli is None or not self.live_cli.exists():
            return None, f"live CLI not found: {self.live_cli}"
        result = self.run_command(
            label,
            [str(self.live_cli), "daemon", "status", "--json"],
            timeout=15,
            env=self.live_environment(),
        )
        if result.returncode != 0:
            return None, f"daemon status rc={result.returncode}: {result.stderr.strip()[:300]}"
        payload = load_json(result.stdout)
        if not isinstance(payload, dict):
            return None, "daemon status output is not a JSON object"
        return payload, None

    def live_home_variables(self) -> dict[str, str]:
        variables: dict[str, str] = {}
        if self.home is not None:
            variables["HYPRIAL_HOME"] = str(self.home)
        if self.state_dir is not None:
            variables["HARNESS_STATE_DIR"] = str(self.state_dir)
        return variables

    def live_environment(self) -> dict[str, str]:
        env = {
            key: value for key, value in os.environ.items() if key not in LIVE_ENV_DROPPED
        }
        env.update(self.live_home_variables())
        cli_dir = self.live_cli.parent if self.live_cli is not None else self.bin_dir
        if cli_dir is not None:
            first = str(cli_dir)
            rest = [
                item
                for item in env.get("PATH", "").split(os.pathsep)
                if item and item != first
            ]
            env["PATH"] = os.pathsep.join([first, *rest])
        return env

    def resolve_paths(self) -> bool:
        tool_root: Path | None = None
        if self.options.tool_dir:
            self.tool_dir = Path(self.options.tool_dir).expanduser()
        else:
            result = self.run_command(
                "uv-tool-dir", [self.options.uv, "tool", "dir"], timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                tool_root = Path(result.stdout.strip())
            else:
                xdg = os.environ.get("XDG_DATA_HOME")
                base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
                tool_root = base / "uv" / "tools"
                self.check(
                    "paths.uv-tool-dir",
                    NOT_RUN,
                    f"uv tool dir failed; using fallback {tool_root}",
                )
            self.tool_dir = tool_root / "hyprial"

        if self.options.bin_dir:
            self.bin_dir = Path(self.options.bin_dir).expanduser()
        else:
            result = self.run_command(
                "uv-tool-bin-dir",
                [self.options.uv, "tool", "dir", "--bin"],
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                self.bin_dir = Path(result.stdout.strip())
            else:
                self.bin_dir = Path.home() / ".local" / "bin"
                self.check(
                    "paths.uv-tool-bin-dir",
                    NOT_RUN,
                    f"uv tool dir --bin failed; using fallback {self.bin_dir}",
                )
        self.live_cli = self.bin_dir / "hyprial"
        if not self.live_cli.exists():
            alternate = self.tool_dir / "bin" / "hyprial"
            if alternate.exists():
                self.live_cli = alternate
        self.python_tried = []
        if self.options.python:
            self.python = Path(self.options.python).expanduser()
            self.python_tried.append(f"--python {self.python}")
        elif self.tool_dir is not None:
            self.python = None
            requested = receipt_python(self.tool_dir)
            if requested is not None:
                self.python_tried.append(f"uv-receipt.toml python = {str(requested)!r}")
                if requested.is_absolute() and requested.exists():
                    self.python = requested
            if self.python is None:
                candidate = self.tool_dir / "bin" / "python"
                self.python_tried.append(str(candidate))
                if candidate.exists():
                    self.python = candidate
        return (
            self.tool_dir is not None
            and self.bin_dir is not None
            and self.live_cli is not None
            and self.live_cli.exists()
            and self.python is not None
            and self.python.exists()
        )

    def _resolve_home(self, unit_env: dict[str, str]) -> Path | None:
        if self.options.home:
            return Path(self.options.home)
        value = unit_env.get("HYPRIAL_HOME") or os.environ.get("HYPRIAL_HOME")
        if value:
            return Path(value).expanduser()
        return Path.home() / ".hyprial"

    def _resolve_state_dir(self, unit_env: dict[str, str]) -> Path | None:
        if self.options.state_dir:
            return Path(self.options.state_dir)
        value = unit_env.get("HARNESS_STATE_DIR") or os.environ.get("HARNESS_STATE_DIR")
        if value:
            return Path(value).expanduser()
        if self.home is not None:
            return self.home / "state"
        return None

    def isolated_environment(self, root: Path) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("HYPRIAL_", "HARNESS_", "GIT_"))
        }
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV"):
            env.pop(key, None)
        env.update(
            {
                "UV_TOOL_DIR": str(root / "tools"),
                "UV_TOOL_BIN_DIR": str(root / "bin"),
                "UV_CACHE_DIR": str(root / "uv-cache"),
                "PYTHONNOUSERSITE": "1",
            }
        )
        return env

    def live_install_environment(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.tool_dir is not None:
            env["UV_TOOL_DIR"] = str(self.tool_dir.parent)
        if self.bin_dir is not None:
            env["UV_TOOL_BIN_DIR"] = str(self.bin_dir)
        return env

    def install_wheel(
        self,
        label: str,
        wheel: Path,
        *,
        env: dict[str, str],
        timeout: float = 300.0,
    ) -> CommandResult:
        if self.python is None:
            raise RuntimeError("python is not resolved")
        command = build_install_command(
            self.options.uv, wheel, self.python, self.wheelhouses
        )
        return self.run_command(label, command, timeout=timeout, env=env)

    def summary(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "generatedAt": utc_now(),
            "mode": self.options.mode,
            "host": self.hostname,
            "service": self.service_unit(),
            "serviceControl": self.service_control_name(),
            "result": self.result,
            "status": self.status(),
            "paths": {
                "toolDir": str(self.tool_dir) if self.tool_dir else None,
                "binDir": str(self.bin_dir) if self.bin_dir else None,
                "liveCli": str(self.live_cli) if self.live_cli else None,
                "python": str(self.python) if self.python else None,
                "home": str(self.home) if self.home else None,
                "stateDir": str(self.state_dir) if self.state_dir else None,
                "artifacts": str(self.artifacts_dir),
            },
            "wheels": {
                "new": self.new_info,
                "old": self.old_info,
                "newProblems": self.new_problems,
                "oldProblems": self.old_problems,
            },
            "baseline": self.baseline,
            "isolated": self.isolated,
            "rollback": self.rollback,
            "gui": self.gui,
            "serviceLogs": self.service_logs.copies,
            "droppedEnvironment": sorted(
                name for name in LIVE_ENV_DROPPED if name in os.environ
            ),
            "checks": [dataclasses.asdict(item) for item in self.checks],
            "commands": str(self.artifacts_dir / "commands.json"),
        }

    def finish_refusal(self) -> int:
        code = self.refusal_code or EXIT_FAILED
        self.result = {EXIT_USAGE: "ABORTED", EXIT_BLOCKED: "BLOCKED"}.get(code, "FAILED")
        return self.finish(code)

    def finish(self, exit_code: int) -> int:
        self.service_logs.capture("final")
        if self.result in ("NOT_RUN", "ROLLBACK"):
            self.result = {
                EXIT_OK: self.status(),
                EXIT_FAILED: "FAILED",
                EXIT_USAGE: "ABORTED",
                EXIT_ROLLBACK_FAILED: "ROLLBACK_FAILED",
                EXIT_BLOCKED: "BLOCKED",
            }.get(exit_code, self.result)
        summary = self.summary()
        self.recorder.write_json("summary.json", summary)
        self.recorder.write_json("commands.json", self.recorder.commands)
        if self.options.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print(f"RESULT {summary['result']} (exit {exit_code})")
            for item in self.checks:
                print(f"{item.status:7} {item.name}: {item.detail}")
            print(f"artifacts: {self.artifacts_dir}")
        return exit_code
