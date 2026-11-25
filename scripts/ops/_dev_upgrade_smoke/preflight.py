"""Read-only baseline and throwaway-isolated candidate checks.

Decisions (plan section 4.1, owner answers Q3, Q5 and Q6, 2026-11-20):
- The home is an input: ``--home`` > ``HYPRIAL_HOME`` > ``~/.hyprial`` (state:
  ``--state-dir`` > ``HARNESS_STATE_DIR`` > ``<home>/state``), resolved before the
  first service command because hyprial derives the unit from it.  The unit's
  ``Environment=`` is read only on the legacy systemctl control, as the
  ``baseline.unit-home`` / ``baseline.unit-state`` cross-check it always was.
- ``toolchain.systemctl`` exists only on the legacy control.
- ``HYPRIAL_DAEMON_AUTOSTART=0`` in the caller's environment is refused
  (``env.autostart``, exit 2).
- On macOS (``sys.platform == "darwin"``), the isolated preflight root uses
  ``dir="/tmp"`` with a short ``"hsm-"`` prefix because ``$TMPDIR`` under
  ``/var/folders/...`` easily exceeds the 103-byte ``AF_UNIX`` ``sun_path``
  limit.  ``cold_start`` checks both the raw and resolved ``daemon.sock`` UTF-8
  byte lengths against the platform limit (103 on macOS, 107 elsewhere) before
  creating ``cold-home``, ``cold-state``, ``cold-user``, ``cold-home/node-id``,
  ``cold-start.log``, or spawning ``daemon run --bootstrap``, recording
  ``BLOCKED`` when exceeded.
- After the socket bound guard passes, ``cold_start`` creates ``cold-home``,
  ``cold-state``, and ``cold-user`` (mode ``0o700``), writes the throwaway
  ``cold-home/node-id`` with mode ``0o600``, sets
  ``HYPRIAL_NETWORK_ISOLATED="1"`` in the child environment, and records a
  redacted ``outputExcerpt`` capped at 500 characters on ``FAIL``.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .common import (
    BLOCKED,
    EXIT_USAGE,
    FAIL,
    NOT_RUN,
    PASS,
    classify_cold_start,
    daemon_fields,
    direct_url_wheel,
    inspect_wheel,
    install_origin_path,
    load_json,
    receipt_wheel,
    redact_auth_material,
    same_file,
)


def allocate_isolated_root() -> Path:
    """Allocate a throwaway isolated preflight root within platform AF_UNIX bounds."""
    if sys.platform == "darwin":
        return Path(tempfile.mkdtemp(prefix="hsm-", dir="/tmp"))
    return Path(tempfile.mkdtemp(prefix="hyprial-smoke-isolated-"))


class PreflightMixin:
    def resolve_wheels(self) -> bool:
        self.new_info, self.new_problems = inspect_wheel(
            self.new_wheel, self.options.new_wheel_sha256
        )
        self.pass_if(
            "wheel.new",
            not self.new_problems and "version" in self.new_info,
            f"{self.new_wheel} version={self.new_info.get('version')}",
            "; ".join(self.new_problems) or "new wheel has no version",
            self.new_info,
        )
        if self.options.expect_commit:
            actual = self.new_info.get("sourceCommit")
            matches = isinstance(actual, str) and actual.startswith(
                self.options.expect_commit
            )
            self.pass_if(
                "wheel.source-commit",
                matches,
                f"sourceCommit={actual}",
                f"sourceCommit {actual!r} does not start with "
                f"{self.options.expect_commit!r}",
            )
        elif self.new_info.get("sourceCommit"):
            self.check(
                "wheel.source-commit",
                NOT_RUN,
                f"sourceCommit={self.new_info['sourceCommit']} (no --expect-commit)",
            )

        if self.options.old_wheel:
            self.old_wheel = Path(self.options.old_wheel).expanduser()
        elif self.tool_dir is not None:
            candidate = receipt_wheel(self.tool_dir) or direct_url_wheel(self.tool_dir)
            if candidate is not None:
                self.old_wheel = candidate
        if self.old_wheel is None:
            self.check(
                "wheel.old",
                FAIL,
                "cannot derive the installed wheel; pass --old-wheel",
            )
            return False
        self.old_info, self.old_problems = inspect_wheel(
            self.old_wheel, self.options.old_wheel_sha256
        )
        old_ok = not self.old_problems and "version" in self.old_info
        self.pass_if(
            "wheel.old",
            old_ok,
            f"{self.old_wheel} version={self.old_info.get('version')}",
            "; ".join(self.old_problems) or "old wheel has no version",
            self.old_info,
        )
        if old_ok and same_file(self.old_wheel, self.new_wheel):
            self.check(
                "wheel.distinct",
                FAIL,
                "old and new wheel resolve to the same file; nothing to smoke-test",
            )
        return old_ok

    def prepare(self) -> bool:
        if not self.resolve_paths():
            missing = [
                f"{name} ({value if value is not None else 'unresolved'})"
                for name, value in (
                    ("tool_dir", self.tool_dir),
                    ("bin_dir", self.bin_dir),
                    ("live_cli", self.live_cli),
                )
                if value is None or (name == "live_cli" and not value.exists())
            ]
            if self.python is None or not self.python.exists():
                tried = "; ".join(self.python_tried) or "no --python and no tool dir"
                missing.append(f"python (tried: {tried}; pass --python)")
            self.check("paths.resolve", FAIL, f"missing: {', '.join(missing)}")
            return False
        self.check(
            "paths.resolve",
            PASS,
            f"tool_dir={self.tool_dir} bin_dir={self.bin_dir} python={self.python}",
        )
        uv_version = self.run_command("uv-version", [self.options.uv, "--version"], timeout=15)
        self.pass_if(
            "toolchain.uv",
            uv_version.returncode == 0,
            uv_version.stdout.strip() or "uv available",
            f"uv failed: {uv_version.stderr.strip()[:300]}",
        )
        if not self.wheelhouses or any(not item.is_dir() for item in self.wheelhouses):
            self.check(
                "wheelhouse",
                FAIL,
                "one --wheelhouse directory is required and must exist",
            )
            return False
        self.check(
            "wheelhouse",
            PASS,
            " ".join(str(item) for item in self.wheelhouses),
        )

        refusal = self.autostart_refusal()
        if refusal is not None:
            self.check("env.autostart", FAIL, refusal)
            self.refusal_code = EXIT_USAGE
            return False
        self.home = self._resolve_home({})
        self.state_dir = self._resolve_state_dir({})
        code = self.select_service("service-status-baseline")
        if code is not None:
            self.refusal_code = code
            return False
        if not self.service.baseline():
            return False
        if self.home is None or self.state_dir is None:
            self.check("baseline.home", FAIL, "could not resolve HYPRIAL_HOME/state dir")
        else:
            self.check(
                "baseline.home",
                PASS,
                f"HYPRIAL_HOME={self.home} HARNESS_STATE_DIR={self.state_dir}",
            )

        version_payload, error = self.live_version()
        if version_payload is None:
            self.check("baseline.version", FAIL, error or "version unavailable")
        else:
            self.baseline["version"] = {
                key: version_payload.get(key)
                for key in ("packageVersion", "localVersion", "installOrigin")
            }
            self.check(
                "baseline.version",
                PASS,
                f"packageVersion={version_payload.get('packageVersion')} "
                f"installOrigin={version_payload.get('installOrigin')}",
                self.baseline["version"],
            )
            origin = install_origin_path(version_payload.get("installOrigin"))
            if origin is None:
                self.check(
                    "baseline.origin",
                    FAIL,
                    "installed origin is not a file:// wheel URL",
                )
            else:
                self.installed_origin = origin
                self.check(
                    "baseline.origin",
                    PASS,
                    f"installed origin={origin}",
                )
        if self.service.daemon_expected():
            daemon_payload, daemon_error = self.daemon_status(label="baseline-daemon")
            if daemon_payload is None:
                self.baseline["daemon"] = {"error": daemon_error}
                self.check(
                    "baseline.daemon",
                    NOT_RUN,
                    daemon_error or "daemon status unavailable",
                )
            else:
                fields = daemon_fields(daemon_payload)
                self.baseline["daemon"] = {
                    key: fields.get(key)
                    for key in ("running", "pid", "version", "nodeId", "socket")
                }
                expected = self.baseline.get("version", {}).get("packageVersion")
                if isinstance(expected, str) and fields.get("version") not in (None, expected):
                    self.check(
                        "baseline.daemon",
                        FAIL,
                        f"daemon version {fields.get('version')} != installed {expected}",
                    )
                else:
                    self.check(
                        "baseline.daemon",
                        PASS,
                        f"running={fields.get('running')} pid={fields.get('pid')} "
                        f"version={fields.get('version')}",
                        self.baseline["daemon"],
                    )
        else:
            self.check(
                "baseline.daemon",
                NOT_RUN,
                "the service is not running; no daemon to inspect",
            )
        if not self.resolve_wheels():
            return False
        if self.installed_origin is not None and self.old_wheel is not None:
            self.pass_if(
                "baseline.origin",
                same_file(self.installed_origin, self.old_wheel),
                "recorded install origin matches the rollback wheel",
                f"install origin {self.installed_origin} != rollback wheel "
                f"{self.old_wheel}",
            )
        if self.options.expect_host:
            expected = {self.hostname.lower(), socket.getfqdn().lower()}
            self.pass_if(
                "host",
                self.options.expect_host.lower() in expected,
                f"host={self.hostname}",
                f"--expect-host {self.options.expect_host!r} does not match "
                f"{sorted(expected)!r}",
            )
        self.prepared = True
        return self.status() != FAIL

    def isolated_preflight(self) -> None:
        target_dir = "/tmp" if sys.platform == "darwin" else tempfile.gettempdir()
        try:
            root = allocate_isolated_root()
        except OSError as error:
            self.check(
                "isolated.workdir",
                BLOCKED,
                f"cannot allocate isolated preflight dir under {target_dir}: {error}",
                {
                    "dir": target_dir,
                    "error": f"{type(error).__name__}: {error}",
                },
            )
            return
        self.isolated["root"] = str(root)
        try:
            env = self.isolated_environment(root)
            install = self.install_wheel("isolated-install", self.new_wheel, env=env)
            self.pass_if(
                "isolated.install",
                install.returncode == 0,
                "candidate installed into throwaway uv tool root",
                f"isolated install rc={install.returncode}: "
                f"{install.stderr.strip()[:400]}",
            )
            if install.returncode != 0:
                return
            binary = root / "bin" / "hyprial"
            if not binary.exists():
                binary = root / "tools" / "hyprial" / "bin" / "hyprial"
            self.isolated["cli"] = str(binary)
            if not binary.exists():
                self.check(
                    "isolated.version",
                    FAIL,
                    f"isolated CLI not found under {root}",
                )
                return
            version_result = self.run_command(
                "isolated-version",
                [str(binary), "version", "--json"],
                timeout=30,
                env=env,
            )
            version_payload = load_json(version_result.stdout)
            version_ok = (
                version_result.returncode == 0
                and isinstance(version_payload, dict)
                and version_payload.get("packageVersion")
                == self.new_info.get("version")
            )
            self.isolated["version"] = (
                {
                    key: version_payload.get(key)
                    for key in ("packageVersion", "installOrigin")
                }
                if isinstance(version_payload, dict)
                else {}
            )
            self.pass_if(
                "isolated.version",
                version_ok,
                f"packageVersion={self.new_info.get('version')}",
                f"version rc={version_result.returncode} "
                f"payload={version_payload!r}",
                self.isolated["version"],
            )
            surface_checks = (
                ("isolated.help", [str(binary), "--help"]),
                ("isolated.gui-help", [str(binary), "gui", "--help"]),
                ("isolated.daemon-help", [str(binary), "daemon", "--help"]),
                ("isolated.daemon-run-help", [str(binary), "daemon", "run", "--help"]),
            )
            for name, argv in surface_checks:
                result = self.run_command(name, argv, timeout=30, env=env)
                self.pass_if(
                    name,
                    result.returncode == 0,
                    "help exit 0",
                    f"help rc={result.returncode}: {result.stderr.strip()[:300]}",
                )
            if self.options.cold_start_timeout > 0:
                self.cold_start(binary, root)
            else:
                self.check(
                    "isolated.cold-start",
                    NOT_RUN,
                    "not requested; an empty home stops at device authorization",
                )
        finally:
            if not self.options.keep_workdir:
                shutil.rmtree(root, ignore_errors=True)
            else:
                self.isolated["kept"] = str(root)

    def cold_start(self, binary: Path, root: Path) -> None:
        home = root / "cold-home"
        state = root / "cold-state"
        user = root / "cold-user"
        socket_path = state / "daemon.sock"
        resolved_socket = state.resolve() / "daemon.sock"
        raw_bytes = len(str(socket_path).encode("utf-8"))
        resolved_bytes = len(str(resolved_socket).encode("utf-8"))
        offending_socket = socket_path if raw_bytes >= resolved_bytes else resolved_socket
        socket_bytes = max(raw_bytes, resolved_bytes)
        limit = 103 if sys.platform == "darwin" else 107
        if socket_bytes > limit:
            self.check(
                "isolated.cold-start",
                BLOCKED,
                f"AF_UNIX socket path is {socket_bytes} bytes (limit {limit}): "
                f"{offending_socket}",
                {
                    "socketPath": str(offending_socket),
                    "rawSocketPath": str(socket_path),
                    "resolvedSocketPath": str(resolved_socket),
                    "rawSocketPathBytes": raw_bytes,
                    "resolvedSocketPathBytes": resolved_bytes,
                    "socketPathBytes": socket_bytes,
                    "socketPathLimit": limit,
                },
            )
            return
        for path in (home, state, user):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        node_id = home / "node-id"
        node_id.write_text("smoke-cold-start\n", encoding="utf-8")
        os.chmod(node_id, 0o600)
        env = self.isolated_environment(root)
        env.update(
            {
                "HOME": str(user),
                "HYPRIAL_HOME": str(home),
                "HARNESS_STATE_DIR": str(state),
                "HYPRIAL_NETWORK_ISOLATED": "1",
            }
        )
        log_path = root / "cold-start.log"
        process: subprocess.Popen[bytes] | None = None
        try:
            with log_path.open("wb") as handle:
                process = subprocess.Popen(
                    [str(binary), "daemon", "run", "--bootstrap"],
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
                deadline = self.clock.monotonic() + self.options.cold_start_timeout
                ready: dict[str, Any] | None = None
                while self.clock.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    result = self.run_command(
                        "cold-start-status",
                        [str(binary), "daemon", "status", "--json"],
                        timeout=8,
                        env=env,
                    )
                    payload = load_json(result.stdout)
                    if result.returncode == 0 and isinstance(payload, dict):
                        fields = daemon_fields(payload)
                        if fields.get("running") is True:
                            ready = fields
                            break
                    self.clock.sleep(1)
            output = log_path.read_text(encoding="utf-8", errors="replace")
            if ready is not None:
                self.check(
                    "isolated.cold-start",
                    PASS,
                    f"daemon ready pid={ready.get('pid')} version={ready.get('version')}",
                )
            elif classify_cold_start(process.returncode or 0, output) == BLOCKED:
                self.check(
                    "isolated.cold-start",
                    BLOCKED,
                    "device authorization required for an empty home; not a package failure",
                )
            else:
                excerpt = redact_auth_material(output).strip()[-500:]
                self.check(
                    "isolated.cold-start",
                    FAIL,
                    f"cold start did not become ready (rc={process.returncode})",
                    {"outputExcerpt": excerpt} if excerpt else {},
                )
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
            log_path.unlink(missing_ok=True)
