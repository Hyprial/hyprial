#!/usr/bin/env python3
# Decisions (shell-6.md §C.3, §C.4, §D.4 6c-3; packaging/clients/README.md):
# - Checks a staged runtime directory, either the staging output or
#   `HyprialOS.app/Contents/Resources/runtime` inside a built bundle (that
#   second use proves the bundler kept every file and its mode).
# - Steps, each PASS / FAIL / BLOCKED, first non-PASS decides the verdict:
#   manifest (every file listed in runtime-manifest.json present with its
#   SHA-256 and mode, nothing extra, no symlink); isolation (a fresh root
#   whose daemon socket path fits AF_UNIX: 103 bytes on macOS, 107 on
#   Linux, else BLOCKED); version (`python3.12 -I -m hyprial.cli version
#   --json` reports the manifest's wheel version); gui-record (`gui
#   --no-open --json --port 0 --stdin-stop`, line 1 is a v1 startup record
#   with a loopback URL whose port is the bound one); handoff (the 0600,
#   caller-owned regular file named by the record holds one line
#   `<origin>/#grant=…`; the grant itself never reaches the evidence);
#   stdin-stop (closing stdin ends the CLI with status 0 and no failure
#   line); children (the record's pids are gone and no process carries the
#   isolated home in its environment); manifest-after (running wrote
#   nothing into the runtime).
# - Daemon-first (KP-083): `hyprial gui` ensures the daemon of the selected
#   home in every state, an ownerless home included, and closing the GUI never
#   stops it (owner's model, not a defect; the product fact is pinned by
#   tests/integration/gui_daemon_first/test_gui_command_ownerless.py). The
#   daemon is a python process in its own session holding `<root>/s/daemon.lock`
#   and publishing its pid in `<root>/s/daemon.json` (serving) or
#   `<root>/s/bootstrap.json` (bootstrap). After `stdin-stop` the step `daemon`
#   treats it as expected only when the pid named by one of those records is
#   alive AND is a carrier of the isolated root; it then runs `daemon stop
#   --json` for that home (same isolated environment) through the bundled
#   runtime and confirms the pid exited within `--daemon-stop-timeout`. It FAILs
#   when a record names a live pid that is not on the home, when a process
#   running `daemon run` on the root is not the recorded one (a second daemon),
#   when more than one recorded daemon is live, when `daemon stop` fails, and
#   when the pid outlives the bound; in the last case `kill_carriers` was the
#   fallback and the step says so (a failure of the graceful stop, never
#   silently accepted). Apart from that one daemon nothing may be left: the
#   `children` check below is exactly as strict as before for every other
#   process, and runs after the daemon was stopped.
# - Isolation recipe (macos-handoff.md red lines): HOME, HYPRIAL_HOME,
#   HARNESS_STATE_DIR, XDG_* and TMPDIR under the fresh root;
#   HYPRIAL_NETWORK_ISOLATED=1, HYPRIAL_USAGE_DISABLE=1,
#   HYPRIAL_PEER_DISCOVERY=0, empty zenoh endpoints, a throwaway node id;
#   every other HYPRIAL_*, HARNESS_* and PYTHON* variable of the caller is
#   dropped; PATH is the one a macOS GUI app gets; loopback bypasses the
#   proxy. No variable in this tree disables launchd installation (none
#   exists); an ownerless `hyprial gui` installs no OS service.
# - "No process is left on the isolated root": macOS 26 shows neither `ps`
#   environments nor the environment part of `sysctl(KERN_PROCARGS2)` for
#   other processes (measured), so on macOS a process counts when it has its
#   working directory or any open file under the root (`lsof +D`; every
#   process the CLI starts inherits a working directory there, and the
#   daemon keeps its lock, socket and logs there). lsof is run by its fixed
#   system path `/usr/sbin/lsof` (decision smoke-lsof-path-20261120), never
#   looked up on the caller's PATH: a PATH without /usr/sbin made the lookup
#   fail and the smoke's own cleanup with it. On Linux the environment,
#   command line and working directory come from `/proc`. Independently of
#   files, a process whose command line names the runtime's interpreter
#   (`command=`) counts too: every process the CLI starts runs it.
# - Every `ps` and `lsof` call is bounded (decision smoke-bounded-waits-
#   20261120): `PS_TIMEOUT` 10 s (a process listing is instantaneous, a
#   loaded Mac stays far below it) and `LSOF_TIMEOUT` 30 s (`+D` walks the
#   whole isolated root, the slowest call). The smoke's cleanup runs with
#   SIGINT/SIGTERM/SIGHUP ignored, so an unbounded wait there could not be
#   interrupted at all. `subprocess.TimeoutExpired` is a SubprocessError and
#   takes the existing paths of any other failure of these calls (runner
#   BLOCKED, or the smoke's cleanup fallback), never PASS.
# - The CLI runs in its own session so a failed run can be stopped as a
#   group (SIGTERM, then SIGKILL). That group is not enough: a process the
#   CLI started in another session can still hold the root, so on every exit
#   `kill_carriers` (also used by smoke_bundle.py) stops everything
#   `carriers` finds before the fresh root is removed (unless --keep).
# - A `version` step that does not finish within its budget is a FAIL (the
#   runtime hangs), not BLOCKED.
# - `kill_carriers` stays a fallback only; what it had to stop is recorded in
#   the evidence (`fallbackKilled`).
"""Verify a staged HyprialOS runtime by running its interpreter in isolation.

Exit status 0 PASS, 1 FAIL, 2 BLOCKED; one verdict line on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import queue
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PASS, FAIL, BLOCKED = 0, 1, 2
MANIFEST_NAME = "runtime-manifest.json"
INTERPRETER = "bin/python3.12"
PREFIX = ("-I", "-m", "hyprial.cli")
GUI_ARGS = ("gui", "--no-open", "--json", "--port", "0", "--stdin-stop")
APP_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
LOOPBACK = "127.0.0.1,localhost,::1"
PASSTHROUGH = ("LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "SHELL")
SOCKET_LIMIT = 103 if sys.platform == "darwin" else 107
RECORD_NAMES = ("daemon.json", "bootstrap.json")
DAEMON_COMMAND = " daemon run"
LSOF = "/usr/sbin/lsof"
PS_TIMEOUT = 10.0
LSOF_TIMEOUT = 30.0


@dataclass(frozen=True)
class Step:
    name: str
    status: str
    detail: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_manifest(runtime: Path) -> tuple[list[str], dict[str, Any]]:
    """Problems of the runtime against its manifest (empty when it matches)."""

    try:
        manifest = json.loads((runtime / MANIFEST_NAME).read_text(encoding="utf-8"))
        listed = manifest["files"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [f"unreadable {MANIFEST_NAME}: {error}"], {}
    problems = []
    present = {}
    for path in sorted(runtime.rglob("*")):
        relative = path.relative_to(runtime).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            problems.append(f"symlink {relative}")
        elif stat.S_ISREG(mode) and relative != MANIFEST_NAME:
            present[relative] = path
        elif not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
            problems.append(f"special file {relative}")
    for relative in sorted(set(listed) - set(present)):
        problems.append(f"missing {relative}")
    for relative in sorted(set(present) - set(listed)):
        problems.append(f"extra {relative}")
    for relative in sorted(set(present) & set(listed)):
        path = present[relative]
        mode = f"{stat.S_IMODE(path.stat().st_mode):04o}"
        if mode != listed[relative].get("mode"):
            problems.append(f"mode {relative} {mode} != {listed[relative].get('mode')}")
        elif _sha256(path) != listed[relative].get("sha256"):
            problems.append(f"sha256 {relative}")
    return problems, manifest


def isolated_environment(root: Path) -> dict[str, str]:
    """The scrubbed environment of every interpreter run under ``root``."""

    for name in ("u", "h", "s", "t", "xc", "xs", "xd", "xk", "xr"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
        (root / name).chmod(0o700)
    env = {key: os.environ[key] for key in PASSTHROUGH if key in os.environ}
    env.update({
        "PATH": APP_PATH, "HOME": str(root / "u"), "HYPRIAL_HOME": str(root / "h"),
        "HARNESS_STATE_DIR": str(root / "s"), "TMPDIR": str(root / "t"),
        "XDG_CONFIG_HOME": str(root / "xc"), "XDG_STATE_HOME": str(root / "xs"),
        "XDG_DATA_HOME": str(root / "xd"), "XDG_CACHE_HOME": str(root / "xk"),
        "XDG_RUNTIME_DIR": str(root / "xr"),
        "HYPRIAL_NETWORK_ISOLATED": "1", "HYPRIAL_USAGE_DISABLE": "1", "HYPRIAL_PEER_DISCOVERY": "0",
        "HYPRIAL_ZENOH_LISTEN": "", "HYPRIAL_ZENOH_CONNECT": "",
        "HYPRIAL_NODE_ID": f"verify-runtime-{uuid.uuid4().hex[:12]}",
        "NO_PROXY": LOOPBACK, "no_proxy": LOOPBACK,
    })
    return env


def validate_record(record: Any) -> list[str]:
    """Problems of a startup record against contract/cli/gui-startup-record.v1.json."""

    if not isinstance(record, dict) or record.get("ok") is not True:
        return [f"not a startup record: {str(record)[:300]}"]
    problems = []
    if record.get("record_version") != 1:
        problems.append(f"record_version {record.get('record_version')!r} != 1")
    host, port, url = record.get("host"), record.get("port"), record.get("url")
    if host not in ("127.0.0.1", "::1"):
        problems.append(f"host {host!r} is not a loopback literal")
    if not isinstance(port, int) or not 0 < port < 65536:
        problems.append(f"port {port!r} is not a bound port")
    try:
        parts = urlsplit(url if isinstance(url, str) else "")
        loopback = parts.hostname is not None and ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        parts, loopback = None, False
    if parts is None or parts.scheme != "http" or not loopback or parts.port != port or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        problems.append(f"url {url!r} is not http://<loopback>:{port}[/]")
    if not isinstance(record.get("handoff_path"), str):
        problems.append("handoff_path missing")
    return problems


def validate_handoff(path: Path, origin: str) -> list[str]:
    try:
        info = path.lstat()
    except OSError as error:
        return [f"handoff unreadable: {error}"]
    problems = []
    if not stat.S_ISREG(info.st_mode):
        problems.append("handoff is not a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        problems.append(f"handoff mode {stat.S_IMODE(info.st_mode):04o} != 0600")
    if info.st_uid != os.getuid():
        problems.append("handoff is not owned by the caller")
    if info.st_size > 4096:
        problems.append("handoff larger than 4 KiB")
    if problems:
        return problems
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or not lines[0].startswith(origin.rstrip("/") + "/#grant=") or len(lines[0]) <= len(origin.rstrip("/") + "/#grant="):
        problems.append("handoff is not one line <origin>/#grant=<grant>")
    return problems


def _first_line(process: subprocess.Popen[str], timeout: float) -> str | None:
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline() if process.stdout else ""), daemon=True).start()
    try:
        return lines.get(timeout=timeout)
    except queue.Empty:
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    probe = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False, timeout=PS_TIMEOUT)
    return not probe.stdout.strip().startswith("Z")


def carriers(root: Path, command: str | None = None) -> list[int]:
    """Live pids (other than this one) holding a working directory, file or environment under ``root``, or running ``command``."""

    root = Path(root)
    found: set[int] = set()
    if command:
        listing = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, check=False, timeout=PS_TIMEOUT)
        for line in listing.stdout.splitlines():
            head, _, text = line.strip().partition(" ")
            if head.isdigit() and command in text:
                found.add(int(head))
    if sys.platform == "darwin":
        listing = subprocess.run([LSOF, "-nP", "-Fp", "+D", str(root)], capture_output=True, text=True, check=False, timeout=LSOF_TIMEOUT)
        found |= {int(line[1:]) for line in listing.stdout.splitlines() if line.startswith("p") and line[1:].isdigit()}
    else:
        marker = str(root).encode()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                data = (entry / "environ").read_bytes() + b"\0" + (entry / "cmdline").read_bytes()
                cwd = os.readlink(entry / "cwd").encode()
            except OSError:
                continue
            if marker in data or cwd.startswith(marker):
                found.add(int(entry.name))
    return sorted(pid for pid in found if pid != os.getpid() and _alive(pid))


@dataclass(frozen=True)
class HomeDaemons:
    """The daemon processes seen on one isolated home."""

    expected: list[int]
    foreign: list[int]
    extra: list[int]
    records: dict[int, str]

    def problem(self) -> str | None:
        if self.foreign:
            return f"the home's record names pid(s) {self.foreign} that do not hold the isolated home"
        if self.extra:
            return f"daemon process(es) {self.extra} hold the isolated home but are not the one its record names"
        if len(self.expected) > 1:
            return f"more than one recorded daemon is running: {self.expected}"
        return None


def record_pids(state_dir: Path) -> dict[int, str]:
    """pid -> record file name for the daemon records under ``state_dir``."""

    found: dict[int, str] = {}
    for name in RECORD_NAMES:
        try:
            record = json.loads((Path(state_dir) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = record.get("pid") if isinstance(record, dict) else None
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            found.setdefault(pid, name)
    return found


def _commands() -> dict[int, str]:
    listing = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, check=False, timeout=PS_TIMEOUT)
    found = {}
    for line in listing.stdout.splitlines():
        head, _, text = line.strip().partition(" ")
        if head.isdigit():
            found[int(head)] = text
    return found


def home_daemons(root: Path, state_dir: Path, interpreter: str) -> HomeDaemons:
    """Classify the daemons of the home: expected (record pid that holds the root), foreign, extra."""

    held = set(carriers(root, command=interpreter))
    records = record_pids(state_dir)
    live = {pid for pid in records if _alive(pid)}
    commands = _commands()
    return HomeDaemons(
        expected=sorted(pid for pid in live if pid in held),
        foreign=sorted(pid for pid in live if pid not in held),
        extra=sorted(pid for pid in held if pid not in live and DAEMON_COMMAND in commands.get(pid, "")),
        records=records,
    )


def stop_daemon(root: Path, interpreter: str, env: dict[str, str], pid: int, *, wait: float = 30.0, command_timeout: float = 60.0) -> dict[str, Any]:
    """``daemon stop --json`` for the home of ``env`` through the bundled runtime; confirm ``pid`` exited.

    The fallback (:func:`kill_carriers`) runs only when the pid outlives ``wait``;
    ``graceful`` is then false and ``fallbackKilled`` lists what it had to stop.
    """

    outcome: dict[str, Any] = {"pid": pid}
    try:
        stop = subprocess.run([interpreter, *PREFIX, "daemon", "stop", "--json"], cwd=env["HOME"], env=env, capture_output=True, text=True, timeout=command_timeout, check=False)
        outcome.update(exit=stop.returncode, stdout=stop.stdout[-1000:], stderr=stop.stderr[-1000:])
    except subprocess.TimeoutExpired:
        outcome["exit"] = None
    started = time.monotonic()
    while _alive(pid) and time.monotonic() - started < wait:
        time.sleep(0.1)
    exited = not _alive(pid)
    outcome["exitedAfterSeconds"] = round(time.monotonic() - started, 2) if exited else None
    outcome["graceful"] = exited and outcome["exit"] == 0
    outcome["fallbackKilled"] = []
    if not exited:
        outcome["fallbackKilled"] = carriers(root, command=interpreter)
        kill_carriers(root, command=interpreter)
    return outcome


def daemon_stop_step(outcome: dict[str, Any], wait: float) -> Step:
    pid = outcome["pid"]
    if outcome["graceful"]:
        return Step("daemon-stop", "PASS", f"`daemon stop` exit 0, daemon {pid} gone {outcome['exitedAfterSeconds']:.2f} s later, no fallback")
    if outcome["fallbackKilled"] or outcome["exitedAfterSeconds"] is None:
        return Step("daemon-stop", "FAIL", f"graceful stop failed: `daemon stop` exit {outcome['exit']}, daemon {pid} still running {wait:.0f} s later; the fallback kill_carriers had to stop {outcome['fallbackKilled']}")
    return Step("daemon-stop", "FAIL", f"`daemon stop` exit {outcome['exit']} although daemon {pid} is gone (stdout {outcome.get('stdout', '')[-200:]!r})")


def _stop_group(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 10)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def kill_carriers(root: Path, command: str | None = None, *, wait: float = 10.0) -> None:
    """SIGTERM, then SIGKILL, every process :func:`carriers` finds, in whatever session it runs."""

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in carriers(root, command=command):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not carriers(root, command=command):
                return
            time.sleep(0.05)


def _verdict(steps: list[Step]) -> tuple[int, str]:
    for status, code in (("FAIL", FAIL), ("BLOCKED", BLOCKED)):
        for step in steps:
            if step.status == status:
                return code, f"{status} {step.name} {step.detail}"
    return PASS, "PASS"


def run(runtime: Path, *, root_parent: Path, startup_timeout: float, stop_timeout: float, keep: bool, version_timeout: float = 120.0, daemon_stop_timeout: float = 30.0) -> tuple[int, str, dict[str, Any]]:
    """Run every step; return (exit status, verdict line, evidence)."""

    runtime = Path(runtime).resolve()
    steps: list[Step] = []
    evidence: dict[str, Any] = {"runtime": str(runtime)}

    def finish() -> tuple[int, str, dict[str, Any]]:
        code, line = _verdict(steps)
        evidence["steps"] = [step.__dict__ for step in steps]
        evidence["verdict"] = line
        return code, line, evidence

    problems, manifest = check_manifest(runtime)
    steps.append(Step("manifest", "FAIL" if problems else "PASS", "; ".join(problems[:10]) or f"{len(manifest['files'])} files match"))
    if problems:
        return finish()
    root = Path(tempfile.mkdtemp(prefix="vr-", dir=root_parent))
    gui: subprocess.Popen[str] | None = None
    try:
        socket_path = root / "s" / "daemon.sock"
        fits = len(os.fsencode(socket_path)) <= SOCKET_LIMIT
        steps.append(Step("isolation", "PASS" if fits else "BLOCKED", f"daemon socket path {len(os.fsencode(socket_path))} bytes, limit {SOCKET_LIMIT}"))
        if not fits:
            return finish()
        env = isolated_environment(root)
        interpreter = str(runtime / INTERPRETER)
        outside = root / "u"
        try:
            version = subprocess.run([interpreter, *PREFIX, "version", "--json"], cwd=outside, env=env, capture_output=True, text=True, timeout=version_timeout, check=False)
        except subprocess.TimeoutExpired:
            steps.append(Step("version", "FAIL", f"`version --json` did not finish within {version_timeout:.0f} s"))
            return finish()
        try:
            reported = json.loads(version.stdout)
        except json.JSONDecodeError:
            reported = {}
        expected = manifest["wheel"]["version"]
        evidence["version"] = {"returncode": version.returncode, "stdout": version.stdout[-2000:], "stderr": version.stderr[-2000:]}
        version_ok = version.returncode == 0 and reported.get("ok") is True and reported.get("packageVersion") == expected
        steps.append(Step("version", "PASS" if version_ok else "FAIL", f"packageVersion {reported.get('packageVersion')!r}, wheel {expected!r}"))
        if not version_ok:
            return finish()
        stderr_path = root / "gui.stderr"
        with stderr_path.open("wb") as stderr:
            gui = subprocess.Popen([interpreter, *PREFIX, *GUI_ARGS], cwd=outside, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True, start_new_session=True)
        started = time.monotonic()
        line = _first_line(gui, startup_timeout)
        evidence["startup_seconds"] = round(time.monotonic() - started, 2)
        try:
            record = json.loads(line or "")
        except json.JSONDecodeError:
            record = None
        evidence["record"] = record
        problems = validate_record(record) if line else [f"no line within {startup_timeout:.0f} s"]
        if problems:
            evidence["gui_stderr"] = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        steps.append(Step("gui-record", "FAIL" if problems else "PASS", "; ".join(problems) or f"v1 record, {record['url']}"))
        if problems:
            return finish()
        handoff_problems = validate_handoff(Path(record["handoff_path"]), record["url"])
        steps.append(Step("handoff", "FAIL" if handoff_problems else "PASS", "; ".join(handoff_problems) or "0600 one-line launch URL"))
        if handoff_problems:
            return finish()
        assert gui.stdin is not None and gui.stdout is not None
        gui.stdin.close()
        try:
            status = gui.wait(timeout=stop_timeout)
            rest = gui.stdout.read()
        except subprocess.TimeoutExpired:
            status, rest = None, ""
        evidence["after_record"] = rest[-2000:]
        stopped = status == 0 and '"ok":false' not in rest.replace(" ", "")
        steps.append(Step("stdin-stop", "PASS" if stopped else "FAIL", f"exit {status} within {stop_timeout:.0f} s" + ("" if stopped else f"; stdout after record {rest[-300:]!r}")))
        if not stopped:
            return finish()
        found = home_daemons(root, root / "s", interpreter)
        evidence["daemons"] = {"expected": found.expected, "foreign": found.foreign, "extra": found.extra, "records": {str(pid): name for pid, name in found.records.items()}}
        problem = found.problem()
        if problem:
            steps.append(Step("daemon", "FAIL", problem))
            return finish()
        if not found.expected:
            steps.append(Step("daemon", "PASS", "no daemon of the home remained"))
        else:
            daemon_pid = found.expected[0]
            outcome = stop_daemon(root, interpreter, env, daemon_pid, wait=daemon_stop_timeout)
            evidence["daemonStop"] = outcome
            stopped_step = daemon_stop_step(outcome, daemon_stop_timeout)
            steps.append(Step("daemon", stopped_step.status, f"expected daemon {daemon_pid} (record {found.records[daemon_pid]}, holds the isolated home); {stopped_step.detail}"))
            if stopped_step.status != "PASS":
                return finish()
        pids = [pid for pid in (record.get("frontend_pid"), record.get("backend_pid")) if isinstance(pid, int)]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (any(_alive(pid) for pid in pids) or carriers(root, command=interpreter)):
            time.sleep(0.2)
        left = [pid for pid in pids if _alive(pid)] + carriers(root, command=interpreter)
        steps.append(Step("children", "FAIL" if left else "PASS", f"left running: {sorted(set(left))}" if left else f"record pids {pids} gone, no process on the isolated home"))
        if left:
            return finish()
        problems, _ = check_manifest(runtime)
        steps.append(Step("manifest-after", "FAIL" if problems else "PASS", "; ".join(problems[:10]) or "runtime unchanged by the run"))
        return finish()
    except (OSError, subprocess.SubprocessError) as error:
        steps.append(Step("runner", "BLOCKED", str(error)))
        return finish()
    finally:
        _stop_group(gui)
        evidence["fallbackKilled"] = carriers(root, command=str(runtime / INTERPRETER))
        kill_carriers(root, command=str(runtime / INTERPRETER))
        evidence["leftAtExit"] = carriers(root, command=str(runtime / INTERPRETER))
        if keep:
            evidence["kept"] = str(root)
        else:
            shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--root-parent", type=Path, default=Path(tempfile.gettempdir()), help="directory for the fresh isolated root (keep it short: AF_UNIX path limit)")
    parser.add_argument("--startup-timeout", type=float, default=200.0)
    parser.add_argument("--stop-timeout", type=float, default=60.0)
    parser.add_argument("--daemon-stop-timeout", type=float, default=30.0, help="seconds the daemon of the home has to exit after `daemon stop`")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    code, line, evidence = run(args.runtime, root_parent=args.root_parent, startup_timeout=args.startup_timeout, stop_timeout=args.stop_timeout, keep=args.keep, daemon_stop_timeout=args.daemon_stop_timeout)
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
