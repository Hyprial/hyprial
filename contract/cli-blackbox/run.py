#!/usr/bin/env python3
"""Language-neutral black-box contract checks for an installed hyprial CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable


Json = dict[str, Any]
Check = tuple[str, Callable[[], None]]


class ContractFailure(AssertionError):
    pass


def resolve_hyprial_executable(value: str, source: str) -> str:
    if not value:
        raise ContractFailure(f"{source} is empty; set it to an executable hyprial CLI")

    candidate = Path(value)
    if candidate.parent != Path(".") or os.sep in value:
        if candidate.is_symlink() and not candidate.exists():
            raise ContractFailure(f"{source} does not exist (broken symlink): {value}")
        if not candidate.exists():
            raise ContractFailure(f"{source} does not exist: {value}")
        if not candidate.is_file():
            raise ContractFailure(f"{source} is not a file: {value}")
        if not os.access(candidate, os.X_OK):
            raise ContractFailure(f"{source} is not executable: {value}")
        return value

    resolved = shutil.which(value)
    if resolved is None:
        for directory in os.get_exec_path():
            existing = Path(directory) / value
            if existing.exists():
                if not existing.is_file():
                    raise ContractFailure(f"{source} is not a file: {existing}")
                raise ContractFailure(f"{source} is not executable: {existing}")
        raise ContractFailure(f"{source} was not found on PATH: {value}")
    if not Path(resolved).is_file():
        raise ContractFailure(f"{source} is not a file: {resolved}")
    if not os.access(resolved, os.X_OK):
        raise ContractFailure(f"{source} is not executable: {resolved}")
    return resolved


class Runner:
    def __init__(self, executable: str, timeout: float, doctor_schema: str) -> None:
        self.executable = executable
        self.timeout = timeout
        self.doctor_schema = doctor_schema
        self.last_returncode: int | None = None
        self._temporary = tempfile.TemporaryDirectory(prefix="hyprial-cli-contract-")
        root = Path(self._temporary.name)
        self.isolated_home = root / "home"
        self.isolated_state = root / "state"
        self.npm_cache = root / "npm-cache"
        self.isolated_state.mkdir(parents=True)
        # Only `hyprial init` may create a home in the product; the contract
        # runner creates its disposable home explicitly, like a fixture.
        self.isolated_home.mkdir(parents=True)

    def close(self) -> None:
        if (self.isolated_state / "daemon.sock").exists():
            try:
                self.invoke("daemon", "stop", "--json", isolated=True, exits=(0, 1))
            except (ContractFailure, OSError):
                pass
        self._temporary.cleanup()

    def ensure_daemon(self) -> None:
        value = self.invoke("init", "--json", isolated=True)
        require(value.get("ok") is True, "managed daemon init.ok must be true")
        # init's readiness answer is the flat ping payload (running/pid/
        # phase), not the nested ps-shaped daemon object it once spread.
        daemon = value.get("daemon")
        require(
            value.get("running") is True
            or (isinstance(daemon, dict) and daemon.get("running") is True),
            "managed daemon must report ready",
        )

    def env(self, *, isolated: bool) -> dict[str, str]:
        env = dict(os.environ)
        env["NPM_CONFIG_CACHE"] = str(self.npm_cache)
        if isolated:
            env["HYPRIAL_HOME"] = str(self.isolated_home)
            env["HARNESS_STATE_DIR"] = str(self.isolated_state)
            env["HYPRIAL_OWNER"] = "e2e"
            env["HYPRIAL_NETWORK_ISOLATED"] = "1"
            for name in (
                "HARNESS_CONFIG",
                "HYPRIAL_AGENT_SESSION",
                "HYPRIAL_MANAGED_WORKER",
                "HYPRIAL_WORKER_ACTOR",
                "HYPRIAL_WORKER_SESSION_REF",
                "HYPRIAL_WORKER_SESSION_REF_FILE",
            ):
                env.pop(name, None)
        return env

    def invoke(
        self,
        *args: str,
        isolated: bool,
        exits: tuple[int, ...] = (0,),
    ) -> Json:
        completed = self.invoke_text(*args, isolated=isolated, exits=exits)

        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ContractFailure(
                f"hyprial {' '.join(args)} did not emit exactly one JSON value: "
                f"{completed.stdout.strip()!r}"
            ) from error
        require(isinstance(value, dict), "top-level JSON value must be an object")
        return value

    def invoke_text(
        self,
        *args: str,
        isolated: bool,
        exits: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                [self.executable, *args],
                env=self.env(isolated=isolated),
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise ContractFailure(f"hyprial executable not found: {self.executable}") from error
        except subprocess.TimeoutExpired as error:
            raise ContractFailure(f"command timed out: hyprial {' '.join(args)}") from error

        self.last_returncode = completed.returncode
        if completed.returncode not in exits:
            raise ContractFailure(
                f"hyprial {' '.join(args)} exited {completed.returncode}; "
                f"stderr={completed.stderr.strip()!r} stdout={completed.stdout.strip()!r}"
            )
        return completed


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractFailure(message)


def require_keys(value: Json, *keys: str) -> None:
    missing = [key for key in keys if key not in value]
    require(not missing, f"missing keys: {', '.join(missing)}")


def build_checks(runner: Runner, include_live: bool) -> list[Check]:
    checks: list[Check] = []

    def help_contract() -> None:
        value = runner.invoke("help", "--json", isolated=True)
        require(value.get("ok") is True, "help.ok must be true")
        usage = value.get("usage")
        require(isinstance(usage, str), "help.usage must be a string")
        for command in ("version", "doctor", "log", "message"):
            require(f"hyprial {command}" in usage, f"help must advertise hyprial {command}")
        require("hyprial ps" not in usage, "help must not advertise the retired hyprial ps (R7)")
        if runner.doctor_schema == "sealed":
            for command in ("hyprial service", "hyprial adapter pin"):
                require(command in usage, f"sealed help must advertise {command}")

    checks.append(("help JSON and command inventory", help_contract))

    def logout_contract() -> None:
        usage = runner.invoke("help", "--json", isolated=True).get("usage", "")
        # Older sealed references do not implement the user-login session
        # surface. Keep their compatibility suite runnable, but enforce the
        # complete contract for every implementation that advertises logout.
        if "hyprial logout" not in usage:
            return

        home = runner.isolated_home
        issuer = "https://issuer.invalid"
        login = home / "secrets" / "login.json"
        device = home / "secrets" / "tailcat" / "device-key.json"
        agent = home / "agents" / "contract-agent" / "auth.json"
        for path in (login, device, agent):
            path.parent.mkdir(parents=True, exist_ok=True)
        login.write_text(
            json.dumps(
                {
                    "version": 1,
                    "issuer": issuer,
                    "refreshToken": "contract-refresh-token",
                    "obtainedAt": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        device.write_text('{"private":"preserve"}', encoding="utf-8")
        agent.write_text('{"credential":"preserve"}', encoding="utf-8")
        (home / "settings.json").write_text(
            json.dumps(
                {
                    "owner": "e2e",
                    "identityMode": "casdoor",
                    "identityIssuer": issuer,
                    "unrelated": "preserve",
                }
            ),
            encoding="utf-8",
        )

        value = runner.invoke("logout", "--yes", "--json", isolated=True)
        require(value.get("ok") is True, "logout.ok must be true")
        require(value.get("status") == "logged-out", "logout must report logged-out")
        reported_home = value.get("home")
        require(
            isinstance(reported_home, str)
            and Path(reported_home).resolve() == home.resolve(),
            "logout must report its scoped home",
        )
        require(not login.exists(), "logout must remove the user login credential")
        require(device.exists(), "logout must preserve the Tailcat device key")
        require(agent.exists(), "logout must preserve agent credentials")
        settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        require(
            settings == {"unrelated": "preserve"},
            "logout must remove only Casdoor login fields from settings",
        )

        repeated = runner.invoke("logout", "--yes", "--json", isolated=True)
        require(
            repeated.get("ok") is True
            and repeated.get("status") == "not-logged-in"
            and repeated.get("removed") == [],
            "repeated logout must be an idempotent success",
        )

    checks.append(("scoped idempotent logout", logout_contract))

    def reasoning_effort_contract() -> None:
        help_result = runner.invoke_text("agent", "start", "--help", isolated=True)
        require("--effort" in help_result.stdout, "agent start must advertise --effort")
        for harness, effort in (("codex", "bogus"), ("claude", "ultra"), ("pi", "high")):
            result = runner.invoke(
                "agent", "start", harness, "--name", "effort-contract",
                "--effort", effort, "--headless", "--json", isolated=True,
                exits=(1,),
            )
            require(result.get("ok") is False, "invalid effort must fail")
            require(
                result.get("code") == "INVALID_ARGUMENT",
                "invalid effort must fail before daemon access",
            )

    checks.append(("official reasoning effort validation", reasoning_effort_contract))

    def service_command_contract() -> None:
        if runner.doctor_schema != "sealed":
            return

        # R7 retired the top-level `ps` CLI command.  `daemon status` carries
        # the daemon block it used to expose; the equivalence can no longer be
        # checked against a second CLI spelling, so assert the invariant here.
        daemon = runner.invoke("daemon", "status", "--json", isolated=True)
        require(daemon.get("ok") is True, "daemon status.ok must be true")
        daemon_block = daemon.get("daemon")
        require(
            isinstance(daemon_block, dict)
            and isinstance(daemon_block.get("running"), bool),
            "daemon status must carry daemon.running as a boolean",
        )
        for restore_field in ("restoring", "restorePending"):
            if restore_field in daemon:
                require(
                    isinstance(daemon[restore_field], bool),
                    f"daemon status {restore_field} must be boolean",
                )

        retired_ps = runner.invoke("ps", "--json", isolated=True, exits=(2,))
        require(
            retired_ps.get("ok") is False and "daemon" not in retired_ps,
            "retired hyprial ps must emit only a CLI error",
        )

        listing = runner.invoke(
            "org", "service", "list", "--json", isolated=True, exits=(1,)
        )
        require(listing.get("ok") is False, "offline service list must fail")
        require(
            listing.get("code") == "DAEMON_UNAVAILABLE",
            "offline service list must use DAEMON_UNAVAILABLE",
        )

        connecting = runner.invoke(
            "org", "service", "connect", "api", "--json", isolated=True, exits=(1,)
        )
        require(connecting.get("ok") is False, "offline service connect must fail")
        require(
            connecting.get("code") == "DAEMON_UNAVAILABLE",
            "offline service connect must use DAEMON_UNAVAILABLE",
        )

        daemon_help = runner.invoke_text("daemon", isolated=True, exits=(2,))
        require("service" in daemon_help.stdout + daemon_help.stderr, "daemon service missing")
        daemon_service_help = runner.invoke_text(
            "daemon", "service", isolated=True, exits=(2,)
        )
        for command in ("install", "uninstall", "status"):
            require(
                command in daemon_service_help.stdout + daemon_service_help.stderr,
                f"daemon service help must show {command}",
            )

        help_result = runner.invoke_text("org", "service", isolated=True, exits=(2,))
        help_text = help_result.stdout + help_result.stderr
        for command in ("connect", "disconnect", "guide", "list"):
            require(command in help_text, f"service help must show {command}")
        require(
            "installed" not in help_text and "mode" not in help_text,
            "bare org service must be help, not the retired daemon-status payload",
        )

        for old, new in (
            ("service", "org service"), ("fs", "org fs"),
            ("user-proxy", "user proxy"), ("link", "agent link"),
            ("onboarding", "init onboarding"),
        ):
            for suffix in ((), ("--json",)):
                retired = runner.invoke_text(old, *suffix, isolated=True, exits=(2,))
                tail = " --json" if suffix else ""
                require(retired.stdout == "", "moved paths must not fabricate a result")
                require(
                    retired.stderr == f"moved: hyprial {old}{tail}  →  hyprial {new}{tail}\n",
                    "moved paths must emit exactly one migration line",
                )

    checks.append(("service group and daemon-status command move", service_command_contract))

    def version_contract() -> None:
        value = runner.invoke("version", "--json", isolated=True)
        require_keys(
            value,
            "ok",
            "packageVersion",
            "localVersion",
            "registry",
        )
        require(value["ok"] is True, "version.ok must be true")
        require(isinstance(value["packageVersion"], str), "packageVersion must be a string")
        require(isinstance(value["localVersion"], str), "localVersion must be a string")
        require(re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", value["localVersion"]) is not None,
                "localVersion must look like a semantic version")
        require(isinstance(value["registry"], str), "registry must be a string")
        # Frozen TS reference binaries predate this reporting extension. Accept
        # its total absence, but never a partial or malformed current schema.
        update_keys = (
            "persistentUpdateSource",
            "installOrigin",
            "dependencyLock",
        )
        if any(key in value for key in update_keys):
            require_keys(value, *update_keys)
            require(
                value["persistentUpdateSource"] == value["registry"],
                "persistentUpdateSource must match the compatibility registry field",
            )
            require(
                value["installOrigin"] is None
                or isinstance(value["installOrigin"], str),
                "installOrigin must be a string or null",
            )
            require(
                isinstance(value["dependencyLock"], dict)
                and value["dependencyLock"].get("status")
                in {"match", "mismatch", "unknown"},
                "dependencyLock must report match, mismatch, or unknown",
            )

    checks.append(("version JSON schema", version_contract))

    def isolated_daemon_status_contract() -> None:
        value = runner.invoke("daemon", "status", "--json", isolated=True)
        require(value == {"ok": True, "daemon": {"running": False}},
                "isolated daemon status must report a stopped daemon without creating state")

    checks.append(("isolated daemon status is side-effect free", isolated_daemon_status_contract))

    def uninitialized_home_contract() -> None:
        missing = runner.isolated_home.parent / "never-initialized-home"
        env = runner.env(isolated=True)
        env["HYPRIAL_HOME"] = str(missing)
        completed = subprocess.run(
            [runner.executable, "daemon", "status", "--json"],
            env=env, text=True, capture_output=True,
            timeout=runner.timeout, check=False,
        )
        require(completed.returncode == 1, "uninitialized-home daemon status must exit 1")
        value = json.loads(completed.stdout)
        require(value.get("ok") is False, "uninitialized-home daemon status must fail")
        require(value.get("code") == "HYPRIAL_HOME_NOT_INITIALIZED",
                "uninitialized-home daemon status must use HYPRIAL_HOME_NOT_INITIALIZED")
        # Fact-reporting contract: name the path and its source, no remedies.
        require(str(missing) in str(value.get("error")),
                "uninitialized-home error must name the configured path")
        require("HYPRIAL_HOME environment variable" in str(value.get("error")),
                "uninitialized-home error must name the HYPRIAL_HOME source")
        require("hyprial init" not in str(value.get("error")),
                "uninitialized-home error must not prescribe remedies")
        require(not missing.exists(),
                "a failing command must never create the home")

    checks.append(("uninitialized home fails loud with path and source",
                   uninitialized_home_contract))

    def upgrade_contract() -> None:
        # The upgrade status surface is py-only: the sealed TS reference and
        # the resolver-test CLI stubs do not advertise it, so gate the whole
        # check on the implementation's own help inventory.  The py build
        # advertises it (pinned by tests/shell/cli/installation/test_cli_autoupdate.py),
        # which keeps this check strict where the command exists without
        # demanding it elsewhere.
        usage = runner.invoke("help", "--json", isolated=True)
        if "hyprial daemon" not in usage.get("usage", ""):
            return
        value = runner.invoke("daemon", "upgrade", "status", "--json", isolated=True)
        require(value.get("ok") is True, "daemon upgrade status.ok must be true")
        require(isinstance(value.get("platform"), str), "daemon upgrade status.platform must be a string")
        require(isinstance(value.get("installed"), bool), "daemon upgrade status.installed must be boolean")
        require(isinstance(value.get("loaded"), bool), "daemon upgrade status.loaded must be boolean")
        require(
            isinstance(value.get("autoUpgradeEnabled"), bool),
            "daemon upgrade status.autoUpgradeEnabled must be boolean (spec r1: "
            "automatic upgrades default OFF; the switch reads here)",
        )
        schedule = value.get("schedule")
        require(
            isinstance(schedule, list) and len(schedule) == 2,
            "daemon upgrade status.schedule must hold the two daily runs",
        )
        for point in schedule:
            require(
                isinstance(point, dict)
                and isinstance(point.get("hour"), int)
                and isinstance(point.get("minute"), int),
                "each schedule point must be {hour, minute} integers",
            )
        last_run = value.get("lastRun")
        require(
            last_run is None or isinstance(last_run, dict),
            "daemon upgrade status.lastRun must be null or an object",
        )
        require(
            not (runner.isolated_state / "autoupdate-last.json").exists(),
            "upgrade status must be side-effect free",
        )

    checks.append(("isolated upgrade status schema (py)", upgrade_contract))

    def log_contract() -> None:
        logs = runner.isolated_state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        entries = [
            {
                "ts": "2026-08-07T00:00:00.000Z",
                "level": "info",
                "component": "contract",
                "name": "contract-daemon",
                "event": "send.received",
                "correlationId": "contract-message",
                "conversationId": "contract-conversation",
            },
            {
                "ts": "2026-08-07T00:00:01.000Z",
                "level": "info",
                "component": "contract",
                "name": "contract-daemon",
                "event": "delivery.accepted",
                "correlationId": "other-message",
            },
        ]
        payload = "\n".join(json.dumps(entry, separators=(",", ":")) for entry in entries)
        (logs / "contract.jsonl").write_text(payload + "\n{malformed\n", encoding="utf-8")
        value = runner.invoke(
            "log", "--correlation-id", "contract-message", "--json", isolated=True
        )
        require(value.get("ok") is True, "log.ok must be true")
        require(value.get("skippedLines") == 1, "log must count malformed JSONL lines")
        require(isinstance(value.get("entries"), list) and len(value["entries"]) == 1,
                "log must filter by exact correlation ID")
        require(value["entries"][0].get("event") == "send.received", "log returned the wrong entry")

        workers = logs / "workers"
        workers.mkdir()
        (workers / "pi-contract-worker.jsonl").write_text(
            json.dumps(
                {
                    "ts": "2026-08-07T00:00:02.000Z",
                    "level": "error",
                    "component": "pi-worker",
                    "name": "contract-worker",
                    "event": "worker.exited",
                    "runtime": "pi",
                    "returnCode": 23,
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        scoped = runner.invoke(
            "log",
            "--component", "pi-worker",
            "--name", "contract-worker",
            "--level", "error",
            "--json",
            isolated=True,
        )
        require(
            isinstance(scoped.get("entries"), list)
            and len(scoped["entries"]) == 1
            and scoped["entries"][0].get("event") == "worker.exited",
            "log must recurse into worker routes and apply scope filters",
        )

    checks.append(("isolated log filtering and damage accounting", log_contract))

    def reversed_log_window() -> None:
        value = runner.invoke(
            "log",
            "--since", "2026-08-07T02:00:00Z",
            "--until", "2026-08-07T01:00:00Z",
            "--json",
            isolated=True,
            exits=(1,),
        )
        require(value.get("ok") is False, "invalid log window must fail")
        require("--since must not be later than --until" in str(value.get("error")),
                "invalid log window must explain the ordering error")

    checks.append(("isolated log invalid-window error", reversed_log_window))

    def unavailable_command(name: str, args: tuple[str, ...]) -> Callable[[], None]:
        def check() -> None:
            value = runner.invoke(*args, isolated=True, exits=(1,))
            require(value.get("ok") is False, f"isolated {name} must fail")
            require(value.get("code") == "DAEMON_UNAVAILABLE", f"isolated {name} must use DAEMON_UNAVAILABLE")
            # Fact-reporting contract: the error must name the socket it
            # tried and the OS-level cause, and must not embed remedies
            # (guidance lives in skills/docs, not in string constants).
            require("daemon.sock" in str(value.get("error")),
                    f"isolated {name} must name the socket path")
            require("hyprial init" not in str(value.get("error")),
                    f"isolated {name} must not prescribe remedies")
        return check

    checks.append((
        "isolated message send fails before delivery",
        unavailable_command("message send", (
            "message", "send", "--from", "@contract-source", "--to", "@contract-unreachable",
            "--summary", "probe", "probe", "--json"
        )),
    ))

    def local_summary_guard() -> None:
        value = runner.invoke(
            "message", "send",
            "--from", "@contract-source",
            "--to", "@contract-unreachable",
            "probe",
            "--json",
            isolated=True,
            exits=(1,),
        )
        require(value.get("ok") is False, "send without a summary must fail")
        # The guard fires locally, before the daemon is ever contacted.
        require(
            value.get("code") == "SUMMARY_REQUIRED",
            "send without a summary must fail locally with SUMMARY_REQUIRED",
        )

    checks.append((
        "isolated message send without summary fails locally",
        local_summary_guard,
    ))
    checks.append((
        "isolated message ack fails before acknowledgement",
        unavailable_command("message ack", (
            "message", "ack", "--from", "@contract-source", "00000000-0000-4000-8000-000000000000", "--json"
        )),
    ))
    checks.append((
        "isolated targets fails before discovery",
        unavailable_command("message targets", ("message", "targets", "--json")),
    ))

    if include_live:
        def live_daemon_status_contract() -> None:
            runner.ensure_daemon()
            value = runner.invoke("daemon", "status", "--json", isolated=True)
            require(value.get("ok") is True, "daemon status.ok must be true")
            daemon = value.get("daemon")
            require(isinstance(daemon, dict) and isinstance(daemon.get("running"), bool),
                    "daemon status.daemon.running must be boolean")
            ipc_stats = daemon.get("ipcStats") if isinstance(daemon, dict) else None
            require(isinstance(ipc_stats, dict), "daemon status.daemon.ipcStats must be an object")
            require(
                isinstance(ipc_stats.get("runtime"), dict),
                "daemon status.daemon.ipcStats.runtime must be an object",
            )
            require(
                isinstance(ipc_stats.get("runtimeOverflowFolds"), int),
                "daemon status.daemon.ipcStats.runtimeOverflowFolds must be an integer",
            )
            require(
                isinstance(ipc_stats.get("coverageCpuMs"), (int, float)),
                "daemon status.daemon.ipcStats.coverageCpuMs must be numeric",
            )
            ipc_clients = daemon.get("ipcClients")
            require(
                isinstance(ipc_clients, dict),
                "ps.daemon.ipcClients must be an object",
            )
            require(
                isinstance(ipc_clients.get("active"), int)
                and isinstance(ipc_clients.get("highWater"), int)
                and isinstance(ipc_clients.get("limit"), int)
                and isinstance(ipc_clients.get("saturated"), bool),
                "ps.daemon.ipcClients must expose active, highWater, limit, and saturated",
            )
            require(
                ipc_clients["highWater"] >= ipc_clients["active"],
                "ps.daemon.ipcClients.highWater must cover active clients",
            )
            require(
                ipc_clients["saturated"]
                == (ipc_clients["active"] >= ipc_clients["limit"]),
                "ps.daemon.ipcClients.saturated must match active versus limit",
            )
            ipc_requests = daemon.get("ipcRequests")
            require(
                isinstance(ipc_requests, dict)
                and isinstance(ipc_requests.get("active"), int)
                and isinstance(ipc_requests.get("highWater"), int),
                "ps.daemon.ipcRequests must expose active and highWater",
            )
            require(
                ipc_requests["highWater"] >= ipc_requests["active"],
                "ps.daemon.ipcRequests.highWater must cover active requests",
            )
            forwarding = value.get("forwarding")
            forwarding_counters = (
                forwarding.get("counters") if isinstance(forwarding, dict) else None
            )
            require(
                isinstance(forwarding_counters, dict)
                and isinstance(forwarding_counters.get("map_peer_remap"), int)
                and forwarding_counters["map_peer_remap"] >= 0,
                "ps.forwarding.counters.map_peer_remap must be non-negative",
            )
            zenoh = value.get("zenoh")
            session = zenoh.get("session") if isinstance(zenoh, dict) else None
            zenoh_counters = (
                session.get("counters") if isinstance(session, dict) else None
            )
            if isinstance(session, dict) and session.get("state") != "unknown":
                require(
                    isinstance(zenoh_counters, dict)
                    and isinstance(zenoh_counters.get("zenoh_link_delete"), int)
                    and zenoh_counters["zenoh_link_delete"] >= 0,
                    "known ps.zenoh.session must expose a non-negative "
                    "zenoh_link_delete counter",
                )
            require(isinstance(value.get("connectors"), list), "ps.connectors must be an array")
            for connector in value.get("connectors", []):
                require(isinstance(connector, dict), "ps connector must be an object")
                if "restoreStatus" not in connector:
                    continue
                require(
                    connector.get("restoreStatus") in {"idle-suppressed", "blocked"},
                    "ps connector restoreStatus must be idle-suppressed or blocked",
                )
                require(
                    connector.get("lastActiveAtMs") is None
                    or isinstance(connector.get("lastActiveAtMs"), int),
                    "ps connector lastActiveAtMs must be integer or null",
                )
                require(
                    connector.get("idleAgeMs") is None
                    or isinstance(connector.get("idleAgeMs"), int),
                    "ps connector idleAgeMs must be integer or null",
                )
                require(
                    isinstance(connector.get("restoreThresholdMs"), int),
                    "ps connector restoreThresholdMs must be an integer",
                )
                require(
                    connector.get("restoreOverride")
                    in {"none", "pending-work", "per-agent"},
                    "ps connector restoreOverride has an unknown value",
                )
                require(
                    isinstance(connector.get("activityUnknown"), bool),
                    "ps connector activityUnknown must be boolean",
                )

        checks.append(("live read-only daemon status schema", live_daemon_status_contract))

        def live_status_refresh_contract() -> None:
            ps_help = runner.invoke_text("ps", "--help", isolated=True)
            if "--refresh" not in ps_help.stdout:
                return
            requested = runner.invoke("ps", "--refresh", "--json", isolated=True)
            refresh = requested.get("refresh")
            require(isinstance(refresh, dict), "refresh admission must be nested")
            require(
                refresh.get("accepted") is True and refresh.get("queued") is True,
                "refresh must report accepted queued admission",
            )
            operation_id = refresh.get("operationId")
            require(isinstance(operation_id, str), "refresh operationId must be a string")
            require(
                isinstance(refresh.get("coalesced"), bool),
                "refresh coalesced must be boolean",
            )
            require(
                not {"accepted", "queued", "operationId", "coalesced"}.intersection(
                    requested.keys() - {"refresh"}
                ),
                "refresh admission fields must not leak to the top level",
            )

            deadline = time.monotonic() + runner.timeout
            settled = None
            while time.monotonic() < deadline:
                settled = runner.invoke("ps", "--json", isolated=True)
                session = settled.get("zenoh", {}).get("session", {})
                if (
                    session.get("refreshOperationId") == operation_id
                    and session.get("refreshState") in {"settled", "failed"}
                ):
                    break
                time.sleep(0.05)
            else:
                raise ContractFailure("status refresh operation did not settle")
            require(
                session.get("refreshState") == "settled",
                "isolated endpoint status refresh must settle successfully",
            )

            later = runner.invoke("ps", "--refresh", "--json", isolated=True)
            later_operation_id = later.get("refresh", {}).get("operationId")
            require(
                isinstance(later_operation_id, str)
                and later_operation_id != operation_id,
                "later refresh must receive a distinct operationId",
            )
            deadline = time.monotonic() + runner.timeout
            while time.monotonic() < deadline:
                later_settled = runner.invoke(
                    "ps",
                    "--refresh-operation-id",
                    later_operation_id,
                    "--json",
                    isolated=True,
                )
                later_session = later_settled.get("zenoh", {}).get("session", {})
                if later_session.get("refreshState") in {"settled", "failed"}:
                    break
                time.sleep(0.05)
            else:
                raise ContractFailure("later status refresh operation did not settle")
            require(
                later_session.get("refreshState") == "settled",
                "later isolated endpoint status refresh must settle successfully",
            )

            earlier = runner.invoke(
                "ps",
                "--refresh-operation-id",
                operation_id,
                "--json",
                isolated=True,
            )
            earlier_session = earlier.get("zenoh", {}).get("session", {})
            require(
                earlier_session.get("refreshOperationId") == operation_id
                and earlier_session.get("refreshState") == "settled",
                "a later settlement must not hide the earlier operationId",
            )
            unknown = runner.invoke(
                "ps",
                "--refresh-operation-id",
                "status-refresh:00000000000000000000000000000000",
                "--json",
                isolated=True,
            )
            unknown_session = unknown.get("zenoh", {}).get("session", {})
            require(
                unknown_session.get("refreshState") == "unknown-expired",
                "an unavailable operationId must be typed unknown-expired",
            )
            tailnet = settled.get("tailnet")
            require(
                isinstance(tailnet, dict)
                and tailnet.get("supported") is False
                and tailnet.get("reason") == "TAILNET_STATUS_PRODUCER_UNAVAILABLE",
                "tailnet projection must explicitly report its absent producer",
            )

        checks.append(("live asynchronous status refresh", live_status_refresh_contract))

        def live_doctor_contract() -> None:
            value = runner.invoke("doctor", "--json", isolated=True, exits=(0, 1))
            require(isinstance(value.get("ok"), bool), "doctor.ok must be boolean")
            require(
                runner.last_returncode == (0 if value["ok"] else 1),
                "doctor must exit 1 exactly when ok is false",
            )
            checks_value = value.get("checks")
            require(isinstance(checks_value, list) and checks_value, "doctor.checks must be a non-empty array")
            schema_version = value.get("schemaVersion")
            if schema_version == 1:
                require(runner.doctor_schema != "legacy", "legacy doctor mode received sealed schema v1")
                summary = value.get("summary")
                require(isinstance(summary, dict), "sealed doctor.summary must be an object")
                for status in ("ok", "warn", "fail"):
                    require(isinstance(summary.get(status), int) and summary[status] >= 0,
                            f"sealed doctor.summary.{status} must be a non-negative integer")
                counts = {status: 0 for status in ("ok", "warn", "fail")}
                for item in checks_value:
                    require(isinstance(item, dict), "each doctor check must be an object")
                    require(isinstance(item.get("name"), str), "doctor check name must be a string")
                    require(item.get("status") in counts, "sealed doctor status must be ok, warn, or fail")
                    require(isinstance(item.get("detail"), str), "doctor check detail must be a string")
                    counts[item["status"]] += 1
                    if item["status"] != "ok":
                        action = item.get("action")
                        require(isinstance(action, dict), "non-ok sealed doctor check must have an action")
                        require(isinstance(action.get("command"), str) and action["command"],
                                "doctor action.command must be a non-empty string")
                        require(isinstance(action.get("description"), str) and action["description"],
                                "doctor action.description must be a non-empty string")
                require(summary == counts, "sealed doctor summary must count check statuses exactly")
                require(value["ok"] is (counts["fail"] == 0),
                        "sealed doctor.ok must mean no failed checks")
            else:
                require(runner.doctor_schema != "sealed", "sealed doctor mode requires schemaVersion 1")
                for item in checks_value:
                    require(isinstance(item, dict), "each doctor check must be an object")
                    require(isinstance(item.get("name"), str), "doctor check name must be a string")
                    require(isinstance(item.get("ok"), bool), "legacy doctor check ok must be boolean")
                    require(isinstance(item.get("detail"), str), "doctor check detail must be a string")
                require(value["ok"] is all(item["ok"] for item in checks_value),
                        "legacy doctor.ok must equal the conjunction of check results")

        checks.append(("live read-only doctor schema", live_doctor_contract))

        def live_targets_contract() -> None:
            value = runner.invoke("message", "targets", "--json", isolated=True)
            targets = value.get("targets")
            require(isinstance(targets, list), "targets.targets must be an array")
            for target in targets:
                require(isinstance(target, dict), "each target must be an object")
                kind = target.get("targetKind")
                require(kind in {"agent", "user", "channel_route"},
                        "targets lists delivery promises only: agent, user, or "
                        "channel_route (nodes live in `hyprial host list`, never here)")
                require(isinstance(target.get("targetUri"), str), "targetUri must be a string")
                if kind == "agent":
                    require(target["targetUri"].startswith("agent:"),
                            "agent targetUri must be canonical")
                    require(len(target["targetUri"].split(":")) == 4,
                            "agent targetUri must be the four-segment owner:machine:actor form")
                if kind == "user":
                    require(target["targetUri"].startswith("user:")
                            and len(target["targetUri"].split(":")) == 2,
                            "user targetUri must be user:<owner>")
                if kind == "channel_route":
                    require(target["targetUri"].startswith("route:")
                            and len(target["targetUri"].split(":")) == 3,
                            "channel_route targetUri must be route:<adapter>:<route>")
                require(target.get("deliverable") is True,
                        "every listed target is deliverable by definition")
                require(isinstance(target.get("status"), str), "target status must be a string")

        checks.append(("live read-only target registry schema", live_targets_contract))

        def live_hosts_contract() -> None:
            value = runner.invoke("host", "list", "--json", isolated=True)
            hosts = value.get("hosts")
            require(isinstance(hosts, list), "hosts.hosts must be an array")
            require(len(hosts) >= 1, "a running daemon announces its own node")
            for host in hosts:
                require(isinstance(host, dict), "each host must be an object")
                require(isinstance(host.get("nodeId"), str) and host["nodeId"],
                        "host nodeId must be a non-empty string")
                require(":" not in host["nodeId"],
                        "host nodeId is a bare node id, never a URI")
                require(host.get("status") in {"online", "unknown"},
                        "host status must be online or unknown")
                observed_at = host.get("observedAtMs")
                age_ms = host.get("ageMs")
                require(observed_at is None or isinstance(observed_at, int),
                        "host observedAtMs must be an integer or null")
                require(age_ms is None or isinstance(age_ms, int) and age_ms >= 0,
                        "host ageMs must be a non-negative integer or null")
                require(isinstance(host.get("stale"), bool),
                        "host stale must be a boolean")
                if observed_at is None:
                    require(age_ms is None and host["stale"] is True
                            and host["status"] == "unknown",
                            "missing host evidence must be stale and unknown")
                if isinstance(age_ms, int) and age_ms > 60_000:
                    require(host["status"] == "unknown",
                            "expired host evidence must be unknown")
                require(host.get("lastError") is None
                        or isinstance(host.get("lastError"), str),
                        "host lastError must be null or a string")

        checks.append(("live read-only host list schema", live_hosts_contract))

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hyprial", default=None, help="hyprial executable to test")
    parser.add_argument("--timeout", type=float, default=45.0, help="per-command timeout in seconds")
    parser.add_argument(
        "--isolated-only",
        action="store_true",
        help="skip managed isolated daemon checks (daemon status, doctor, targets)",
    )
    parser.add_argument(
        "--doctor-schema",
        choices=("auto", "sealed", "legacy"),
        default="auto",
        help="require the sealed 71ea88c or legacy doctor JSON shape (default: accept and validate either)",
    )
    args = parser.parse_args()

    if args.hyprial is not None:
        executable_source = "--hyprial"
        executable_value = args.hyprial
    elif "HYPRIAL_BIN" in os.environ:
        executable_source = "HYPRIAL_BIN"
        executable_value = os.environ["HYPRIAL_BIN"]
    else:
        executable_source = "hyprial on PATH"
        executable_value = "hyprial"

    try:
        executable = resolve_hyprial_executable(executable_value, executable_source)
    except ContractFailure as error:
        print(f"RESULT FAIL 0/0: {error}")
        return 1

    runner = Runner(executable, args.timeout, args.doctor_schema)
    failures: list[tuple[str, str]] = []
    checks = build_checks(runner, include_live=not args.isolated_only)
    try:
        for name, check in checks:
            try:
                check()
            except Exception as error:  # report every independent contract
                failures.append((name, str(error)))
                print(f"FAIL {name}: {error}")
            else:
                print(f"PASS {name}")
    finally:
        runner.close()

    passed = len(checks) - len(failures)
    if failures:
        print(f"RESULT FAIL {passed}/{len(checks)}")
        return 1
    print(f"RESULT PASS {passed}/{len(checks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
