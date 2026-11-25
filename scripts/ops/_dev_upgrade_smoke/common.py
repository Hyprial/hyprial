"""Shared primitives for the dev-upgrade smoke instrument.

Decisions (plan section 4.4, PR-3):
- ``Runner`` is the only subprocess boundary.  ``Runner.run`` waits for a
  command; ``Runner.spawn`` starts a long-lived child (the ``hyprial gui``
  check) in its own session with a stdin pipe, a stdout pipe and stderr in a
  file, and returns a ``ChildProcess``.  Tests replace the runner, so they fake
  both.
- Every ``ChildProcess`` method returns at once (``read_line`` selects with a
  zero timeout, ``poll`` never waits): every wait of the GUI check is a loop on
  the script's ``Clock``, which tests replace.
- ``signal_group`` signals the child's own process group (its pid, since the
  child leads a new session) and only while ``poll`` still reports it running:
  an unreaped child keeps its pid, so the group can never belong to another
  process.  ``group_alive`` and ``kill_residual_group`` check and clean up
  residual descendants in that group after the root exits.
- The startup record is checked against the consumer checks of
  ``contract/cli/gui-startup-record.v1.json``: the required keys, version 1, a
  loopback ``host`` that is the host of ``url``, ``port`` the port of ``url``,
  an absolute ``handoff_path``, and, for the daemon driver, ``backend_pid`` null
  and ``backend_owned`` false.  When ``--gui-port`` is not 0 the record must
  name that port.
- The handoff file is only ``lstat``-ed (never opened, never followed): a
  regular file owned by the caller with mode 0600 (preserving special bits via
  ``stat.S_IMODE``), as the contract requires.
- ``redact_auth_material`` removes device-authorization URLs and one-time codes
  across snake_case, space-separated, camelCase (``verificationUri``,
  ``userCode``), and human ``code: <CODE>`` output lines while preserving
  ``BOOTSTRAP_AUTH_OUTCOME_UNKNOWN`` lines that carry no secret URL or code.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import select
import shlex
import stat as stat_module
import subprocess
import time
import tomllib
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Sequence

SCHEMA = "hyprial-dev-upgrade-smoke/v2"
MAX_LOG_BYTES = 2 * 1024 * 1024
PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
NOT_RUN = "NOT_RUN"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_ROLLBACK_FAILED = 3
EXIT_BLOCKED = 4

AUTH_MARKERS = (
    "BOOTSTRAP_AUTH_OUTCOME_UNKNOWN",
    "onboarding.device",
    "verification_uri",
    "verification uri",
    "verificationuri",
    "user_code",
    "user code",
    "usercode",
)

_AUTH_CODE_LINE = re.compile(r"^\s*code\s*:\s*\S+", re.IGNORECASE)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(text: str) -> Any | None:
    """Parse JSON even when a CLI wrapped it in a leading note line."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return None


def install_origin_path(value: Any) -> Path | None:
    if not isinstance(value, str):
        return None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "file":
        return None
    if parsed.netloc not in ("", "localhost"):
        return None
    return Path(urllib.parse.unquote(parsed.path))


def same_file(left: Path | None, right: Path | None) -> bool:
    if left is None or right is None:
        return False
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.realpath(left) == os.path.realpath(right)


def bounded(text: str) -> str:
    if len(text) <= MAX_LOG_BYTES:
        return text
    half = MAX_LOG_BYTES // 2
    return text[:half] + "\n...[log truncated]...\n" + text[-half:]


def redact_auth_material(text: str) -> str:
    """Keep auth state labels but remove URLs and one-time codes from logs."""
    lines: list[str] = []
    for line in text.splitlines():
        lowered = line.lower()
        if "http://" in lowered or "https://" in lowered:
            lines.append("[redacted: URL from device-authorization output]")
            continue
        has_secret_marker = any(
            marker != "BOOTSTRAP_AUTH_OUTCOME_UNKNOWN" and marker in lowered
            for marker in AUTH_MARKERS
        )
        if has_secret_marker or _AUTH_CODE_LINE.match(line):
            lines.append("[redacted: device-authorization output]")
            continue
        lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


class Clock:
    """The one time source of the script's waits; tests replace it."""

    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def absolute_path(value: Path | str) -> Path:
    """Lexical absolute path, fixed once so every command names the same home."""
    return Path(os.path.abspath(Path(value).expanduser()))


@dataclasses.dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_seconds: float = 0.0

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


class ChildProcess:
    """A spawned child whose every method returns at once."""

    def __init__(self, process: Any) -> None:
        self.process = process
        self.pid = int(process.pid)
        self._buffer = b""
        self._eof = False

    def read_line(self) -> str | None:
        stream = self.process.stdout
        while b"\n" not in self._buffer and not self._eof and stream is not None:
            ready, _, _ = select.select([stream], [], [], 0)
            if not ready:
                break
            chunk = os.read(stream.fileno(), 65536)
            if chunk:
                self._buffer += chunk
            else:
                self._eof = True
        if b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            return line.decode("utf-8", "replace") + "\n"
        if self._eof and self._buffer:
            line, self._buffer = self._buffer, b""
            return line.decode("utf-8", "replace")
        return None

    def poll(self) -> int | None:
        return self.process.poll()

    def close_stdin(self) -> None:
        stream = self.process.stdin
        if stream is None or stream.closed:
            return
        try:
            stream.close()
        except OSError:
            pass

    def signal_group(self, number: int) -> bool:
        if self.poll() is not None:
            return False
        try:
            os.killpg(self.pid, number)
        except ProcessLookupError:
            return False
        return True

    def group_alive(self) -> bool:
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def kill_residual_group(self, number: int) -> bool:
        try:
            os.killpg(self.pid, number)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def close(self) -> None:
        self.close_stdin()
        stream = self.process.stdout
        if stream is not None and not stream.closed:
            stream.close()


class Runner:
    """The only subprocess boundary; tests replace this object."""

    def spawn(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        stderr_path: Path,
    ) -> ChildProcess:
        command = [str(item) for item in argv]
        with stderr_path.open("ab") as stderr:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                env=env,
                start_new_session=True,
                restore_signals=True,
            )
        return ChildProcess(process)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 60.0,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CommandResult:
        started = time.monotonic()
        command = [str(item) for item in argv]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=cwd,
            )
            return CommandResult(
                command,
                completed.returncode,
                completed.stdout,
                completed.stderr,
                duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout or ""
            stderr = error.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            return CommandResult(
                command,
                124,
                stdout,
                stderr,
                timed_out=True,
                duration_seconds=time.monotonic() - started,
            )
        except OSError as error:
            return CommandResult(
                command,
                127,
                "",
                f"{type(error).__name__}: {error}",
                duration_seconds=time.monotonic() - started,
            )


class Recorder:
    def __init__(self, root: Path):
        self.root = root
        self.logs = root / "logs"
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.commands: list[dict[str, Any]] = []

    def command(
        self,
        label: str,
        result: CommandResult,
        *,
        redact: bool = False,
    ) -> None:
        index = len(self.commands) + 1
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "command"
        log_path = self.logs / f"{index:02d}-{slug}.log"
        body = (
            f"$ {' '.join(shlex.quote(item) for item in result.argv)}\n"
            f"returncode: {result.returncode}\n"
            f"timed_out: {result.timed_out}\n"
            f"duration_seconds: {result.duration_seconds:.3f}\n"
            "--- stdout ---\n"
            f"{result.stdout}\n"
            "--- stderr ---\n"
            f"{result.stderr}\n"
        )
        if redact:
            body = redact_auth_material(body)
        log_path.write_text(bounded(body), encoding="utf-8")
        self.commands.append(
            {
                "label": label,
                "argv": result.argv,
                "returncode": result.returncode,
                "timedOut": result.timed_out,
                "durationSeconds": round(result.duration_seconds, 3),
                "log": str(log_path),
            }
        )

    def write_json(self, name: str, value: Any) -> None:
        path = self.root / name
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclasses.dataclass
class Check:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = dataclasses.field(default_factory=dict)


def inspect_wheel(path: Path, expected_sha256: str | None) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    if not path.is_file():
        return {"path": str(path)}, [f"wheel is not a regular file: {path}"]
    summary: dict[str, Any] = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if expected_sha256:
        expected = expected_sha256.lower()
        summary["expectedSha256"] = expected
        if summary["sha256"] != expected:
            problems.append(
                f"sha256 {summary['sha256']} does not match expected {expected}"
            )
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        return summary, problems + [f"wheel is not a zip archive: {error}"]

    with archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            problems.append(f"wheel has a corrupt member: {bad_member}")
        names = archive.namelist()
        summary["members"] = len(names)
        metadata_names = sorted(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        if len(metadata_names) != 1:
            problems.append(f"expected one .dist-info/METADATA, got {metadata_names}")
        else:
            metadata = archive.read(metadata_names[0]).decode("utf-8", "replace")
            match = re.search(r"^Version:\s*(\S+)\s*$", metadata, re.MULTILINE)
            if match is None:
                problems.append("METADATA has no Version field")
            else:
                summary["version"] = match.group(1)

        release_names = sorted(name for name in names if name.endswith("_gui/release.json"))
        summary["releaseEntries"] = release_names
        if len(release_names) == 1:
            try:
                release = json.loads(
                    archive.read(release_names[0]).decode("utf-8", "replace")
                )
            except json.JSONDecodeError as error:
                problems.append(f"release.json is not JSON: {error}")
            else:
                if isinstance(release, dict):
                    summary["sourceCommit"] = release.get("sourceCommit")

        native = sorted(
            name
            for name in names
            if name.startswith("hyprial/_core/")
            and name.endswith((".so", ".pyd", ".dylib"))
        )
        summary["nativeFiles"] = native
    return summary, problems


def build_install_command(
    uv: str,
    wheel: Path,
    python: Path,
    wheelhouses: Sequence[Path],
) -> list[str]:
    command = [
        uv,
        "tool",
        "install",
        "--force",
        "--reinstall",
        "--offline",
        "--no-index",
    ]
    for wheelhouse in wheelhouses:
        command.extend(["--find-links", str(wheelhouse)])
    command.extend(["--python", str(python), str(wheel)])
    return command


def parse_receipt(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    tool = data.get("tool")
    return tool if isinstance(tool, dict) else {}


def receipt_wheel(tool_dir: Path) -> Path | None:
    receipt = parse_receipt(tool_dir / "uv-receipt.toml")
    requirements = receipt.get("requirements")
    if not isinstance(requirements, list):
        return None
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        value = requirement.get("path")
        if isinstance(value, str) and value.endswith(".whl"):
            return Path(value)
    return None


def direct_url_wheel(tool_dir: Path) -> Path | None:
    candidates = sorted(
        tool_dir.glob("lib/python*/site-packages/hyprial-*.dist-info/direct_url.json")
    )
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        path = install_origin_path(data.get("url"))
        if path is not None:
            return path
    return None


def receipt_python(tool_dir: Path) -> Path | None:
    receipt = parse_receipt(tool_dir / "uv-receipt.toml")
    python = receipt.get("python")
    return Path(python) if isinstance(python, str) else None


def daemon_fields(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    daemon = payload.get("daemon")
    return daemon if isinstance(daemon, dict) else payload


def daemon_ready(payload: Any, expected_version: str) -> tuple[bool, str, dict[str, Any]]:
    fields = daemon_fields(payload)
    if not fields:
        return False, "daemon status payload has no daemon object", {}
    if fields.get("running") is not True:
        return False, "daemon.running is not true", fields
    lifecycle = fields.get("lifecycle")
    if isinstance(lifecycle, dict) and lifecycle.get("crashed") is True:
        return False, "daemon lifecycle reports crashed=true", fields
    pid = fields.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False, f"daemon.pid is not a positive integer: {pid!r}", fields
    version = fields.get("version")
    if version != expected_version:
        return False, f"daemon.version {version!r} != expected {expected_version!r}", fields
    return True, "daemon is running with the expected version", fields


def classify_cold_start(returncode: int, output: str) -> str:
    lowered = output.lower()
    if any(marker.lower() in lowered for marker in AUTH_MARKERS):
        return BLOCKED
    if returncode == 0:
        return FAIL
    return FAIL


GUI_RECORD_REQUIRED = (
    "ok",
    "record_version",
    "mode",
    "url",
    "host",
    "port",
    "handoff_path",
    "backend_driver",
    "frontend_pid",
    "backend_pid",
    "assets_pid",
)


def safe_gui_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    keys = (
        "ok",
        "code",
        "error",
        "record_version",
        "mode",
        "url",
        "host",
        "port",
        "handoff_path",
        "handoff_note",
        "backend_driver",
        "backend_endpoint",
        "backend_owned",
        "backend_pid",
        "backend_principal",
        "frontend_pid",
        "assets_pid",
        "installed",
        "assets",
        "source_commit",
    )
    summary = {key: payload[key] for key in keys if key in payload}
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("runtime"), str):
        summary["runtime"] = data["runtime"]
    return summary


def validate_gui_record(payload: Any, expected_port: int = 0) -> list[str]:
    if not isinstance(payload, dict):
        return ["record is not a JSON object"]
    problems: list[str] = []
    missing = [key for key in GUI_RECORD_REQUIRED if key not in payload]
    if missing:
        problems.append(f"required keys missing: {', '.join(missing)}")
    if payload.get("ok") is not True:
        problems.append(
            f"ok is not true (code={payload.get('code')!r} "
            f"error={str(payload.get('error'))[:200]!r})"
        )
    version = payload.get("record_version")
    if type(version) is not int or version != 1:
        problems.append(f"record_version must be 1, got {version!r}")
    if payload.get("backend_driver") != "daemon":
        problems.append(
            f"backend_driver must be 'daemon', got {payload.get('backend_driver')!r}"
        )
    if payload.get("backend_pid") is not None:
        problems.append(
            f"backend_pid must be null for the daemon driver, got "
            f"{payload.get('backend_pid')!r}"
        )
    if payload.get("backend_owned") is not False:
        problems.append(
            f"backend_owned must be false, got {payload.get('backend_owned')!r}"
        )
    port = payload.get("port")
    port_ok = type(port) is int and 1 <= port <= 65535
    if not port_ok:
        problems.append(f"port must be an integer in 1..65535, got {port!r}")
    elif expected_port and port != expected_port:
        problems.append(f"port {port} is not the requested --gui-port {expected_port}")
    host = payload.get("host")
    if host not in ("127.0.0.1", "::1"):
        problems.append(f"host must be a loopback literal, got {host!r}")
    url = payload.get("url")
    parsed = urllib.parse.urlparse(url) if isinstance(url, str) else None
    try:
        url_port = parsed.port if parsed is not None else None
    except ValueError:
        url_port = None
    if port_ok and url_port != port:
        problems.append(f"url {url!r} does not carry port {port}")
    url_host = parsed.hostname if parsed is not None else None
    if parsed is None or parsed.scheme != "http" or url_host != host:
        problems.append(f"url {url!r} is not http on the record host {host!r}")
    handoff = payload.get("handoff_path")
    if not isinstance(handoff, str) or not handoff or not Path(handoff).is_absolute():
        problems.append(f"handoff_path must be an absolute path, got {handoff!r}")
    return problems


def handoff_stat(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as error:
        return {"path": str(path), "error": str(error)}
    return {
        "path": str(path),
        "regular": stat_module.S_ISREG(info.st_mode),
        "ownerMatches": info.st_uid == os.getuid(),
        "mode": oct(stat_module.S_IMODE(info.st_mode)),
        "size": info.st_size,
        "mtime": dt.datetime.fromtimestamp(
            info.st_mtime, dt.timezone.utc
        ).isoformat(),
    }


def handoff_problems(info: dict[str, Any]) -> list[str]:
    if "error" in info:
        return [f"cannot stat the handoff file: {info['error']}"]
    problems: list[str] = []
    if info.get("regular") is not True:
        problems.append("not a regular file")
    if info.get("ownerMatches") is not True:
        problems.append("owner is not the current user")
    if info.get("mode") != "0o600":
        problems.append(f"mode is {info.get('mode')}, not 0o600")
    return problems
