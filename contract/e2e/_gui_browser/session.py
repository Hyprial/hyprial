"""Processes of one browser-driven E2E run: ready home, daemon, ``hyprial gui``, Node.

Decision record (E2E-032, 2026-11-21):
- The throwaway root sits under ``short_temp_root()`` (AF_UNIX path budget)
  and never under the account HOME; ``tests.support.gui_home`` refuses such a
  target anyway. The owner is committed in the home's settings by that
  helper; ``hyprial gui`` refuses ``HYPRIAL_OWNER`` (GUI_OWNER_OVERRIDE), so
  the daemon and GUI children run without it and only the CLI calls of
  ``IsolatedDaemon`` carry the same owner, which also satisfies its
  no-external-trigger guard. The children get ``HOME``, ``HYPRIAL_HOME`` and
  ``HARNESS_STATE_DIR`` inside that root, ``HYPRIAL_NETWORK_ISOLATED=1``, no
  proxy variables, and ``HYPRIAL_SERVICE_AUTOINSTALL=0`` plus
  ``HYPRIAL_DAEMON_AUTOSTART=0`` so neither the daemon nor ``hyprial gui``
  installs, starts or touches an OS service unit.
  The only inherited ``HYPRIAL_*`` variables are ``HYPRIAL_ACTOR_BACKEND``
  for the daemon (``reference-abi`` where no native engine is built) and
  ``HYPRIAL_BROWSER_EXECUTABLE`` for the Node script.
- The daemon is ``hyprial daemon run`` as a plain child in its own session,
  not ``hyprial init``: init would try the identity issuer and would leave a
  detached daemon. Isolation is still proven by the daemon's own report
  (``IsolatedDaemon.require_network_isolated``), and the IPC, CLI and stop
  paths are the ones every isolated scenario uses.
- ``hyprial gui`` runs with ``--stdin-stop`` so the clean stop is one line on
  its stdin; the startup record's ``handoff_path`` must lie inside the root.
  From a source checkout the launcher builds ``gui/dist`` itself, so the
  prerequisites are Node, npm, ``gui/node_modules`` and a browser.
- The one-time URL is never read here: the Node script reads the private
  ``launch.url`` itself. ``start_gui`` checks ``lstat`` on raw ``handoff_path``
  (regular file, non-symlink, owned by ``geteuid()``, mode ``0600``,
  basename ``launch.url``, resolved inside ``root``) and verifies ``url`` is a
  loopback ``http`` URL matching ``port`` (converting ``urlsplit``/``port``
  ``ValueError`` into ``AssertionError`` so startup-record failures stay on the
  structured E2E failure path). ``scrub`` removes grant fragments and long
  tokens from any child output this module quotes.
- ``node_environment(root)`` isolates ``HOME``, XDG directories,
  ``TMPDIR``/``TMP``/``TEMP``, and ``npm_config_cache`` under ``root`` (0700)
  while preserving ``PLAYWRIGHT_BROWSERS_PATH`` (or an existing host default
  Playwright cache directory) when needed. ``missing_prerequisites`` probes in
  a temporary directory under ``short_temp_root()``, normalizes non-dict JSON
  probe output to ``{}``, and converts probe timeouts, subprocess errors,
  non-zero exits, or missing-browser answers into missing items (BLOCKED).
- Detached PGID tracking and birth-identity fencing: every started process
  group (``daemon``, ``gui``, ``browserScript``) and child-reported PID
  (``host``, ``browser``) records ``process_birth_identity`` at launch or
  live observation time (a background watcher polls ``browser.pid`` while
  Chromium is alive so its birth identity, PGID, and PPID under the Node
  wrapper are captured before exit). A child-reported PID in its own
  detached session under its expected parent (``pgid == pid`` and
  ``ppid == parent_pid`` — both ``chromium.launchServer`` under Node and
  ``hyprial gui``'s ``frontend`` host via ``spawn_kwargs()``) is registered as
  a tracked group in ``self.groups`` so surviving descendants are detected
  and signalled by PGID even if the leader exits.
- Fail-closed signalling and unverified identities: ``_group_state`` and
  ``_pid_state`` fence every ``SIGTERM`` and ``SIGKILL`` against
  ``process_birth_identity`` and ``_group_has_live_members``. ``_group_state``
  requires ``observed == group.birth`` on the group leader before returning
  ``"alive"``; when ``group.birth`` or ``observed`` is ``None`` while live
  members remain in the PGID (including when the leader exited or the birth
  probe is unavailable), it returns ``"unverified"`` and never signals the
  PGID. Reused PIDs/PGIDs (``observed != birth``) are marked ``verified_dead``
  and never signalled. Reported PIDs are validated via ``_valid_pid_int``
  (``1 <= pid <= _MAX_PID``, non-bool ``int``, not runner PID/PGID, catching
  ``ValueError``/``OverflowError``), and unobserved, provenance-mismatched
  (even if already dead), or out-of-range child-reported PIDs are recorded in
  ``self.unverified_identities``, are never signalled, and fail
  ``survivors()`` instead of falsely passing clean. ``_safe_pid_repr`` renders
  oversized integers (``bit_length() > 128``) as ``<oversized-int bits=...>``
  without triggering Python's integer-string conversion limit, and all child
  JSON boundaries catch ``ValueError`` alongside ``json.JSONDecodeError``
  (with ``start_gui`` recording an unverified host identity and raising
  ``AssertionError``).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from _daemon_runner import IsolatedDaemon, sanitized_env, short_temp_root
from hyprial.kernel import (
    darwin_group_has_live_members,
    linux_group_has_live_members,
    process_birth_identity,
)

ROOT = Path(__file__).resolve().parents[3]
GUI_ROOT = ROOT / "gui"
PAGE_MODULE = Path(__file__).with_name("page.mjs")
DAEMON_READY_SECONDS = 60.0
GUI_STARTUP_SECONDS = 300.0
STOP_SECONDS = 30.0
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
LOOPBACK = "127.0.0.1,localhost,::1"
BROWSER_OVERRIDE = "HYPRIAL_BROWSER_EXECUTABLE"
ACTOR_BACKEND = "HYPRIAL_ACTOR_BACKEND"
_SECRET = re.compile(r"(grant=)[^\s\"'&]+|[A-Za-z0-9_\-]{32,}")
_MAX_PID = 2_147_483_647


@dataclass
class TrackedGroup:
    """One started process group fenced by its leader's birth identity."""

    label: str
    pgid: int
    leader_pid: int
    birth: str | None
    verified_dead: bool = False


@dataclass
class TrackedPid:
    """One child-reported PID fenced by its birth identity."""

    label: str
    pid: int
    birth: str | None
    verified_dead: bool = False


class Blocked(Exception):
    """A prerequisite of the run is missing on this machine; never a pass."""


def scrub(text: str) -> str:
    """``text`` without grant fragments or long opaque tokens."""

    return _SECRET.sub(lambda match: f"{match.group(1)}<redacted>" if match.group(1) else "<redacted>",
                       text)


def child_environment(root: Path) -> dict[str, str]:
    """The isolated environment of the daemon and ``hyprial gui`` children."""

    env = {key: value for key, value in sanitized_env(dict(os.environ)).items()
           if (key == ACTOR_BACKEND or not key.startswith(("HYPRIAL_", "HARNESS_")))
           and key not in PROXY_KEYS}
    env.update({
        "HOME": str(root / "user"),
        "HYPRIAL_HOME": str(root / "home"),
        "HARNESS_STATE_DIR": str(root / "state"),
        "HYPRIAL_NETWORK_ISOLATED": "1",
        "HYPRIAL_ZENOH_LISTEN": "",
        "HYPRIAL_ZENOH_CONNECT": "",
        "HYPRIAL_PEER_DISCOVERY": "0",
        "HYPRIAL_SERVICE_AUTOINSTALL": "0",
        "HYPRIAL_DAEMON_AUTOSTART": "0",
        "NO_PROXY": LOOPBACK,
        "no_proxy": LOOPBACK,
    })
    return env


def _default_playwright_cache() -> Path | None:
    if sys.platform == "darwin":
        candidates = [Path.home() / "Library" / "Caches" / "ms-playwright"]
    else:
        candidates = []
        xdg_cache = os.environ.get("XDG_CACHE_HOME", "").strip()
        if xdg_cache:
            candidates.append(Path(xdg_cache).expanduser() / "ms-playwright")
        candidates.append(Path.home() / ".cache" / "ms-playwright")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return None


def node_environment(root: Path) -> dict[str, str]:
    """The Node browser script's isolated environment under ``root``."""

    env = {key: value for key, value in sanitized_env(dict(os.environ)).items()
           if (key == BROWSER_OVERRIDE or not key.startswith(("HYPRIAL_", "HARNESS_")))
           and key not in PROXY_KEYS}
    isolated_dirs = {
        "HOME": root / "user",
        "XDG_CONFIG_HOME": root / "xdg-config",
        "XDG_CACHE_HOME": root / "xdg-cache",
        "XDG_DATA_HOME": root / "xdg-data",
        "XDG_STATE_HOME": root / "xdg-state",
        "TMPDIR": root / "tmp",
        "TMP": root / "tmp",
        "TEMP": root / "tmp",
        "npm_config_cache": root / "npm-cache",
    }
    for path in set(isolated_dirs.values()):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    if not env.get("PLAYWRIGHT_BROWSERS_PATH", "").strip():
        default_cache = _default_playwright_cache()
        if default_cache is not None:
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(default_cache)
    env.update({key: str(path) for key, path in isolated_dirs.items()})
    env.update({"NO_PROXY": LOOPBACK, "no_proxy": LOOPBACK})
    return env


def missing_prerequisites() -> list[str]:
    """What this machine lacks for a browser-driven run; empty when ready."""

    missing = [f"{tool} not on PATH" for tool in ("node", "npm") if shutil.which(tool) is None]
    if not (GUI_ROOT / "node_modules" / "playwright-core" / "package.json").is_file():
        missing.append("gui/node_modules is not installed (npm --prefix gui ci)")
    if missing:
        return missing
    with tempfile.TemporaryDirectory(prefix="e2e032-probe-", dir=short_temp_root()) as probe_dir:
        probe_root = Path(probe_dir)
        try:
            probe = subprocess.run(
                ["node", str(PAGE_MODULE), "--probe"],
                cwd=ROOT,
                env=node_environment(probe_root),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as error:
            missing.append(f"browser probe failed: {scrub(f'{type(error).__name__}: {error}')}")
            return missing
    lines = [line for line in probe.stdout.splitlines() if line.strip()]
    try:
        parsed = json.loads(lines[-1]) if lines else {}
    except (json.JSONDecodeError, ValueError):
        parsed = {}
    answer = parsed if isinstance(parsed, dict) else {}
    if probe.returncode != 0:
        detail = answer.get("error") or probe.stderr[-500:] or probe.stdout[-500:] or f"exit {probe.returncode}"
        missing.append(f"browser probe exited {probe.returncode}: {scrub(str(detail))}")
    elif answer.get("browser") is not True:
        detail = answer.get("error") or probe.stderr[-500:] or probe.stdout[-500:] or "unexpected browser probe output"
        missing.append(f"no browser: {scrub(str(detail))}")
    return missing


def _valid_pid_int(value: Any) -> int | None:
    if type(value) is not int or value < 1 or value > _MAX_PID:
        return None
    if value == os.getpid() or value == os.getpgrp():
        return None
    return value


def _parse_pid_text(raw: str) -> int | None:
    if not re.fullmatch(r"\d+", raw):
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return _valid_pid_int(parsed)


def _safe_pid_repr(value: Any) -> str:
    if type(value) is int and value.bit_length() > 128:
        return f"<oversized-int bits={value.bit_length()}>"
    try:
        return scrub(repr(value))[:80]
    except (ValueError, OverflowError):
        return f"<{type(value).__name__} unrepresentable>"


def _alive(pid: int) -> bool:
    valid_pid = _valid_pid_int(pid)
    if valid_pid is None:
        return False
    try:
        os.kill(valid_pid, 0)
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except PermissionError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(valid_pid)], capture_output=True, text=True,
                           timeout=10, check=False).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _safe_getpgid(pid: int) -> int | None:
    valid_pid = _valid_pid_int(pid)
    if valid_pid is None:
        return None
    try:
        return os.getpgid(valid_pid)
    except (OSError, OverflowError, ValueError):
        return None


def _process_ppid(pid: int) -> int | None:
    valid_pid = _valid_pid_int(pid)
    if valid_pid is None:
        return None
    if sys.platform.startswith("linux"):
        try:
            stat_text = Path(f"/proc/{valid_pid}/stat").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        closing = stat_text.rfind(")")
        fields = stat_text[closing + 2:].split() if closing >= 0 else []
        if len(fields) <= 1:
            return None
        try:
            return int(fields[1])
        except ValueError:
            return None
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(valid_pid)],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _group_has_live_members(pgid: int) -> bool:
    valid_pgid = _valid_pid_int(pgid)
    if valid_pgid is None:
        return False
    try:
        os.killpg(valid_pgid, 0)
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except PermissionError:
        return True
    if sys.platform.startswith("linux"):
        if linux_group_has_live_members(valid_pgid) is False:
            return False
    elif sys.platform == "darwin":
        if darwin_group_has_live_members(valid_pgid) is False:
            return False
    return True


def _group_state(group: TrackedGroup) -> str:
    if group.verified_dead or not _group_has_live_members(group.pgid):
        group.verified_dead = True
        return "dead"
    if group.birth is None:
        return "unverified"
    observed = process_birth_identity(group.leader_pid)
    if observed == group.birth:
        return "alive"
    if observed is not None and observed != group.birth:
        group.verified_dead = True
        return "dead"
    return "unverified"


def _pid_state(tracked: TrackedPid) -> str:
    if tracked.verified_dead or not _alive(tracked.pid):
        tracked.verified_dead = True
        return "dead"
    if tracked.birth is None:
        return "unverified"
    observed = process_birth_identity(tracked.pid)
    if observed == tracked.birth:
        return "alive"
    if observed is not None and observed != tracked.birth:
        tracked.verified_dead = True
        return "dead"
    if not _alive(tracked.pid):
        tracked.verified_dead = True
        return "dead"
    return "unverified"


def _signal_group(pgid: int, signum: int) -> None:
    valid_pgid = _valid_pid_int(pgid)
    if valid_pgid is None:
        return
    try:
        os.killpg(valid_pgid, signum)
    except (ProcessLookupError, PermissionError, OverflowError, ValueError):
        pass


def _signal_pid(pid: int, signum: int) -> None:
    valid_pid = _valid_pid_int(pid)
    if valid_pid is None:
        return
    try:
        os.kill(valid_pid, signum)
    except (ProcessLookupError, PermissionError, OverflowError, ValueError):
        pass


def _stop_child(process: subprocess.Popen[Any], tracked_group: TrackedGroup, seconds: float) -> None:
    try:
        process.wait(timeout=seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if _group_state(tracked_group) != "alive":
        return
    _signal_group(tracked_group.pgid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    if _group_state(tracked_group) == "alive":
        _signal_group(tracked_group.pgid, signal.SIGKILL)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _validated_handoff_path(raw_path: Any, root: Path) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise AssertionError("the GUI startup record did not provide handoff_path")
    candidate = Path(raw_path)
    if candidate.name != "launch.url":
        raise AssertionError(f"unexpected GUI handoff filename: {candidate.name!r}")
    try:
        info = candidate.lstat()
    except OSError as error:
        raise AssertionError(f"the GUI handoff file cannot be lstat'd: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AssertionError("the GUI handoff file must be a regular non-symlink file")
    if info.st_uid != os.geteuid():
        raise AssertionError("the GUI handoff file is not owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise AssertionError(f"the GUI handoff file mode must be 0o600, got {oct(stat.S_IMODE(info.st_mode))}")
    resolved = candidate.resolve()
    if resolved.name != "launch.url" or not resolved.is_relative_to(root.resolve()):
        raise AssertionError("the GUI handoff file is outside the throwaway root")
    return resolved


def _validated_loopback_origin(raw_url: Any, reported_port: Any) -> str:
    if not isinstance(raw_url, str) or not raw_url:
        raise AssertionError("the GUI startup record did not provide a url")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as error:
        raise AssertionError(f"GUI url could not be parsed: {scrub(str(error))}") from error
    if parsed.scheme != "http":
        raise AssertionError(f"GUI url scheme must be http, got {parsed.scheme!r}")
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise AssertionError(f"GUI url hostname is not loopback: {parsed.hostname!r}")
    if not isinstance(reported_port, int) or isinstance(reported_port, bool) or reported_port <= 0:
        raise AssertionError(f"GUI startup record port is invalid: {reported_port!r}")
    if port != reported_port:
        raise AssertionError(f"GUI url port {port!r} != startup record port {reported_port!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _probe_pid_file(pid_file: Path) -> tuple[str, str | None, int | None, int | None] | None:
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    pid_int = _parse_pid_text(raw)
    if pid_int is None:
        return (raw, None, None, None)
    return (
        raw,
        process_birth_identity(pid_int),
        _safe_getpgid(pid_int),
        _process_ppid(pid_int),
    )


class GuiSession:
    """One throwaway root with its daemon, ``hyprial gui`` host and browser script."""

    def __init__(self, executable: Path, root: Path, owner: str) -> None:
        self.root = root
        self.owner = owner
        self.executable = executable
        self.env = child_environment(root)
        self.daemon = IsolatedDaemon(executable, root, "unused")
        self.daemon.env = {**self.env, "HYPRIAL_OWNER": owner}
        self.daemon_process: subprocess.Popen[Any] | None = None
        self.gui_process: subprocess.Popen[str] | None = None
        self.node_process: subprocess.Popen[str] | None = None
        self.groups: list[int] = []
        self.reported_pids: dict[str, int] = {}
        self._tracked_groups: list[TrackedGroup] = []
        self._tracked_pids: dict[str, TrackedPid] = {}
        self.unverified_identities: list[str] = []
        self._daemon_group: TrackedGroup | None = None
        self._gui_group: TrackedGroup | None = None
        self._node_group: TrackedGroup | None = None
        self.startup: dict[str, Any] = {}
        self._gui_lines: queue.Queue[str | None] = queue.Queue()
        self.gui_stdout: list[str] = []

    def _register_group(self, label: str, pid: int) -> TrackedGroup:
        group = TrackedGroup(label, pid, pid, process_birth_identity(pid))
        self._tracked_groups.append(group)
        self.groups.append(pid)
        return group

    def _record_child_reported_pid(
        self,
        label: str,
        pid: int,
        birth: str | None,
        pgid: int | None,
        ppid: int | None,
        parent_pid: int | None,
    ) -> None:
        valid_pid = _valid_pid_int(pid)
        if valid_pid is None:
            self.unverified_identities.append(f"invalid {label} pid {_safe_pid_repr(pid)}")
            return
        if birth is not None and pgid == valid_pid and parent_pid is not None and ppid == parent_pid:
            self._tracked_groups.append(TrackedGroup(label, valid_pid, valid_pid, birth))
            self.groups.append(valid_pid)
            self.reported_pids[label] = valid_pid
            return
        if birth is not None and parent_pid is not None and pgid == parent_pid:
            self._tracked_pids[label] = TrackedPid(label, valid_pid, birth)
            self.reported_pids[label] = valid_pid
            return
        self.unverified_identities.append(
            f"unverified {label} pid {valid_pid} (pgid={pgid!r}, ppid={ppid!r}, birth={birth!r})"
        )

    def _record_frontend_pid(self, frontend_pid: Any) -> None:
        valid_pid = _valid_pid_int(frontend_pid)
        if valid_pid is None:
            self.unverified_identities.append(f"invalid host pid {_safe_pid_repr(frontend_pid)}")
            return
        gui_pid = self.gui_process.pid if self.gui_process is not None else None
        pgid = _safe_getpgid(valid_pid)
        ppid = _process_ppid(valid_pid) if pgid == valid_pid else None
        self._record_child_reported_pid(
            "host",
            valid_pid,
            process_birth_identity(valid_pid),
            pgid,
            ppid,
            gui_pid,
        )

    def _record_browser_pid(
        self, raw: str, birth: str | None, pgid: int | None, ppid: int | None
    ) -> None:
        browser_pid = _parse_pid_text(raw)
        if browser_pid is None:
            self.unverified_identities.append(f"invalid browser pid {_safe_pid_repr(raw)}")
            return
        node_pid = self.node_process.pid if self.node_process is not None else None
        self._record_child_reported_pid("browser", browser_pid, birth, pgid, ppid, node_pid)

    def prepare_home(self) -> dict[str, Any]:
        """Prepare the GUI-ready home with the helper's own CLI."""

        (self.root / "user").mkdir(mode=0o700)
        completed = subprocess.run(
            [sys.executable, "-m", "tests.support.gui_home", "prepare", "--home",
             str(self.root / "home"), "--owner", self.owner, "--state-dir",
             str(self.root / "state"), "--json"],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=120, check=False)
        if completed.returncode != 0:
            raise AssertionError(f"gui_home prepare exited {completed.returncode}: "
                                 f"{scrub(completed.stdout[-1000:] + completed.stderr[-1000:])}")
        try:
            summary = json.loads(completed.stdout)
        except (json.JSONDecodeError, ValueError) as error:
            raise AssertionError(
                f"gui_home prepare returned invalid JSON: {scrub(str(error))}"
            ) from error
        if not isinstance(summary, dict):
            raise AssertionError("gui_home prepare did not return a JSON object")
        return {"owner": summary.get("owner"), "files": len(summary.get("writtenFiles")
                                                             or summary.get("written_files") or [])}

    def start_daemon(self) -> int:
        """Start ``hyprial daemon run`` as a child and prove it reports isolation."""

        try:
            settings = json.loads((self.root / "home" / "settings.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as error:
            raise AssertionError(
                f"prepared home settings.json could not be parsed: {scrub(str(error))}"
            ) from error
        if settings.get("owner") != self.owner:
            raise AssertionError("the prepared home did not commit the scenario owner")
        self.daemon.require_no_external_triggers()
        log = (self.root / "daemon-child.log").open("w", encoding="utf-8")
        self.daemon_process = subprocess.Popen(
            [str(self.executable), "daemon", "run"], cwd=ROOT, env=self.env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
        log.close()
        self._daemon_group = self._register_group("daemon", self.daemon_process.pid)
        socket_path = self.root / "state" / "daemon.sock"
        deadline = time.monotonic() + DAEMON_READY_SECONDS
        while not socket_path.exists():
            if self.daemon_process.poll() is not None:
                raise AssertionError(f"daemon exited {self.daemon_process.returncode} before its "
                                     f"socket: {self.log_tail('daemon-child.log')}")
            if time.monotonic() > deadline:
                raise AssertionError(f"daemon socket missing after {DAEMON_READY_SECONDS:g}s")
            time.sleep(0.1)
        self.daemon.require_network_isolated()
        return self.daemon_process.pid

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.daemon.request(method, params)

    def command(self, *args: str) -> dict[str, Any]:
        return self.daemon.command(*args)

    def _pump(self, stream: Any) -> None:
        for line in stream:
            self.gui_stdout.append(line)
            self._gui_lines.put(line)
        self._gui_lines.put(None)

    def start_gui(self) -> dict[str, Any]:
        """Start ``hyprial gui`` and return its startup record (no URL, no grant)."""

        log = (self.root / "gui-child.log").open("w", encoding="utf-8")
        self.gui_process = subprocess.Popen(
            [str(self.executable), "gui", "--no-open", "--json", "--port", "0", "--stdin-stop"],
            cwd=ROOT, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            text=True, start_new_session=True)
        log.close()
        self._gui_group = self._register_group("gui", self.gui_process.pid)
        threading.Thread(target=self._pump, args=(self.gui_process.stdout,), daemon=True).start()
        deadline = time.monotonic() + GUI_STARTUP_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"hyprial gui published no startup record within "
                                     f"{GUI_STARTUP_SECONDS:g}s: {self.log_tail('gui-child.log')}")
            try:
                line = self._gui_lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(f"hyprial gui exited {self.gui_process.wait()} before its "
                                     f"startup record: {scrub(''.join(self.gui_stdout)[-2000:])} "
                                     f"{self.log_tail('gui-child.log')}")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            except ValueError as error:
                self.unverified_identities.append(
                    "unverified host pid: startup record integer exceeded string conversion limit"
                )
                raise AssertionError(
                    f"hyprial gui startup record could not be parsed: {scrub(str(error))}"
                ) from error
            if isinstance(payload, dict) and payload.get("ok") is True and "handoff_path" in payload:
                break
        handoff = _validated_handoff_path(payload.get("handoff_path"), self.root)
        origin = _validated_loopback_origin(payload.get("url"), payload.get("port"))
        if payload.get("backend_driver") != "daemon":
            raise AssertionError(f"hyprial gui did not select the daemon driver: "
                                 f"{payload.get('backend_driver')!r}")
        if "frontend_pid" in payload and payload["frontend_pid"] is not None:
            self._record_frontend_pid(payload["frontend_pid"])
        self.startup = {"handoffPath": str(handoff), "origin": origin,
                        "mode": payload.get("mode"), "driver": payload.get("backend_driver")}
        return self.startup

    def run_browser(self, script: Path, config: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Run one Node browser script; its last stdout line is its JSON result."""

        pid_file = self.root / "browser.pid"
        config_path = self.root / "browser-config.json"
        config_path.write_text(json.dumps({**config, "pidFile": str(pid_file)}), encoding="utf-8")
        self.node_process = subprocess.Popen(
            ["node", str(script), str(config_path)], cwd=ROOT, env=node_environment(self.root),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        self._node_group = self._register_group("browserScript", self.node_process.pid)
        observed: list[tuple[str, str | None, int | None, int | None]] = []
        stop_event = threading.Event()

        def _watch_pid_file() -> None:
            while not stop_event.is_set():
                if pid_file.is_file():
                    snap = _probe_pid_file(pid_file)
                    if snap is not None:
                        observed.append(snap)
                        return
                stop_event.wait(0.05)

        watcher = threading.Thread(target=_watch_pid_file, daemon=True)
        watcher.start()
        try:
            stdout, stderr = self.node_process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if _group_state(self._node_group) == "alive":
                _signal_group(self._node_group.pgid, signal.SIGKILL)
            stdout, stderr = self.node_process.communicate(timeout=10)
            stderr = f"browser script exceeded {timeout:g}s\n{stderr}"
        finally:
            stop_event.set()
            watcher.join(timeout=5.0)
            snap = observed[-1] if observed else (_probe_pid_file(pid_file) if pid_file.is_file() else None)
            if snap is not None:
                self._record_browser_pid(*snap)
        lines = [line for line in stdout.splitlines() if line.strip()]
        try:
            result = json.loads(lines[-1]) if lines else None
        except (json.JSONDecodeError, ValueError):
            result = None
        if not isinstance(result, dict):
            raise AssertionError(f"browser script gave no JSON result (exit "
                                 f"{self.node_process.returncode}): {scrub(stderr[-3000:])}")
        if stderr.strip():
            result.setdefault("stderrTail", scrub(stderr[-1500:]))
        return result

    def log_tail(self, name: str, size: int = 3000) -> str:
        path = self.root / name
        if not path.is_file():
            return f"<no {name}>"
        return scrub(path.read_text(encoding="utf-8", errors="replace")[-size:])

    def daemon_log_matches(self, *needles: str) -> dict[str, int]:
        """Line counts of the throwaway daemon's own logs, total and per needle.

        The daemon writes JSONL under ``<state>/logs``; the child's own stdout
        is ``daemon-child.log``. ``lines`` is the positive control that the
        logs were read at all.
        """

        logs = self.root / "state" / "logs"
        paths = [self.root / "daemon-child.log",
                 *(sorted(logs.rglob("*.jsonl")) if logs.is_dir() else [])]
        counts = {"files": 0, "lines": 0, **{needle: 0 for needle in needles}}
        for path in paths:
            if not path.is_file():
                continue
            counts["files"] += 1
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                counts["lines"] += 1
                for needle in needles:
                    counts[needle] += needle in line
        return counts

    def _reap_children(self) -> None:
        for process in (self.node_process, self.gui_process, self.daemon_process):
            if process is not None and process.poll() is None:
                try:
                    process.wait(timeout=0)
                except subprocess.TimeoutExpired:
                    pass

    def _still_waiting(self) -> bool:
        return any(_group_state(group) in ("alive", "unverified") for group in self._tracked_groups) or any(
            _pid_state(tracked) in ("alive", "unverified") for tracked in self._tracked_pids.values()
        )

    def _wait_all_gone(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._reap_children()
            if not self._still_waiting():
                return
            time.sleep(0.1)
        self._reap_children()

    def stop(self) -> None:
        """Stop the browser script, the GUI host and the daemon, then sweep verified groups and pids."""

        if self.node_process is not None and self.node_process.poll() is None and self._node_group is not None:
            if _group_state(self._node_group) == "alive":
                _signal_group(self._node_group.pgid, signal.SIGTERM)
            _stop_child(self.node_process, self._node_group, 10)
        if self.gui_process is not None and self.gui_process.poll() is None and self._gui_group is not None:
            try:
                assert self.gui_process.stdin is not None
                self.gui_process.stdin.write("shutdown\n")
                self.gui_process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            _stop_child(self.gui_process, self._gui_group, STOP_SECONDS)
        if self.daemon_process is not None and self.daemon_process.poll() is None and self._daemon_group is not None:
            try:
                self.daemon.stop()
                self.daemon.wait_stopped(timeout=STOP_SECONDS)
            finally:
                _stop_child(self.daemon_process, self._daemon_group, STOP_SECONDS)
        self._reap_children()
        alive_groups = [group for group in self._tracked_groups if _group_state(group) == "alive"]
        alive_pids = [tracked for tracked in self._tracked_pids.values() if _pid_state(tracked) == "alive"]
        if alive_groups or alive_pids:
            for group in alive_groups:
                _signal_group(group.pgid, signal.SIGTERM)
            for tracked in alive_pids:
                _signal_pid(tracked.pid, signal.SIGTERM)
            self._wait_all_gone(5.0)
            for group in self._tracked_groups:
                if _group_state(group) == "alive":
                    _signal_group(group.pgid, signal.SIGKILL)
            for tracked in self._tracked_pids.values():
                if _pid_state(tracked) == "alive":
                    _signal_pid(tracked.pid, signal.SIGKILL)
            self._wait_all_gone(5.0)

    def force_kill_survivors(self, timeout: float = 5.0) -> list[str]:
        """SIGKILL any verified-alive surviving process groups and reported pids."""

        for group in self._tracked_groups:
            if _group_state(group) == "alive":
                _signal_group(group.pgid, signal.SIGKILL)
        for tracked in self._tracked_pids.values():
            if _pid_state(tracked) == "alive":
                _signal_pid(tracked.pid, signal.SIGKILL)
        self._wait_all_gone(timeout)
        return self.survivors()

    def survivors(self) -> list[str]:
        """Every surviving or unverified started process group, reported pid, or identity."""

        self._reap_children()
        found: list[str] = []
        for group in self._tracked_groups:
            state = _group_state(group)
            if state == "alive":
                found.append(f"group {group.pgid}")
            elif state == "unverified":
                found.append(f"unverified group {group.pgid}")
        for name, tracked in self._tracked_pids.items():
            state = _pid_state(tracked)
            if state == "alive":
                found.append(f"{name} {tracked.pid}")
            elif state == "unverified":
                found.append(f"unverified {name} {tracked.pid}")
        found.extend(self.unverified_identities)
        return found

    def exit_codes(self) -> dict[str, int | None]:
        return {name: process.returncode for name, process in
                (("daemon", self.daemon_process), ("gui", self.gui_process),
                 ("browserScript", self.node_process)) if process is not None}


__all__ = ["Blocked", "GuiSession", "TrackedGroup", "TrackedPid", "missing_prerequisites", "scrub"]
