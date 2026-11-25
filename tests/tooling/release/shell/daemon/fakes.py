"""A fake bundled runtime and a fake ``HyprialOS.app`` for the daemon-aware verifier and smoke.

The fake ``bin/python3.12`` runs :data:`CLI` with this test's interpreter. It
answers ``version --json``, ``gui --no-open --json --port 0 --stdin-stop`` and
``daemon stop --json`` the way the real CLI does after the daemon-first change:
``gui`` ensures a detached daemon (own session, lock file held, pid published in
``<state>/daemon.json``) and closing the GUI never stops it; ``daemon stop``
asks the daemon recorded for the home to exit and, like the real CLI, refuses
(``HYPRIAL_HOME_NOT_INITIALIZED``) a home that does not exist; the home is
``HYPRIAL_HOME``, else ``<HOME>/.hyprial``, and the fake app sets
``<HOME>/.hyprialos`` for its child the way the shell does. Behaviour is switched by a
``mode`` file next to the interpreter (the runtime is run with a scrubbed
environment); ``consume-after`` makes ``gui`` remove ``launch.url`` that many
seconds after writing it, like a webview that loaded the page.

Modes: ``daemon`` (one daemon, reused when one is already recorded), ``stubborn``
(its daemon ignores SIGTERM), ``second`` (an extra process running ``daemon run``
on the root that no record names), ``unrecorded`` (a daemon whose record is never
written), ``foreign`` (the record names a live process outside the root),
``stale`` (the record names a dead pid, no daemon), ``stray`` (a daemon plus a
leftover that is not a daemon), ``fresh`` (every ``gui`` starts a new daemon
instead of attaching), ``none`` (no daemon at all).

Decision: portable-process-cleanup-20261119.  Every daemon record carries the
``ps -o lstart=`` birth of its pid (``birth``), and the fake ``daemon stop``
signals the recorded pid only while that birth still matches, as the real
record's ``birthIdentity`` fences a reused pid.  ``stop_foreign`` stops the one
process a test started outside the root by pid, birth and command together;
``reap`` finds whatever still holds the root or the test's runtime after the
run through the verifier's own ``carriers`` (``/proc`` on Linux, ``/usr/sbin/lsof``
on macOS, no PATH adjustment), kills only what it recorded with its birth, and fails the test when
anything was left.  Nothing here reads ``/proc``.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path

from tests.support.process_table import Ledger, table
from tests.tooling.release.shell.support import LOCK, load

VERSION = "0.0.1+gccccccc"

DAEMON = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
state = Path(sys.argv[1])
record = state / "daemon.json"
if sys.argv[2] == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    def leave(*_):
        record.unlink(missing_ok=True)
        os._exit(0)
    signal.signal(signal.SIGTERM, leave)
lock = open(state / "daemon.lock", "w")
if sys.argv[2] != "unrecorded":
    temporary = state / ".daemon.json.tmp"
    born = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
    temporary.write_text(json.dumps({"pid": os.getpid(), "birth": born, "socket": str(state / "daemon.sock")}) + "\n")
    os.replace(temporary, record)
while True:
    time.sleep(0.2)
'''

CLI = r'''
import json, os, signal, subprocess, sys, threading, time
from pathlib import Path
here = Path(sys.argv[0]).resolve().parent
mode = (here / "mode").read_text().strip() if (here / "mode").exists() else "daemon"
args = sys.argv[1:]
assert args[:3] == ["-I", "-m", "hyprial.cli"], args
command = args[3:]
home = Path(os.environ["HYPRIAL_HOME"]) if "HYPRIAL_HOME" in os.environ else Path(os.environ["HOME"]) / ".hyprial"
state = Path(os.environ["HARNESS_STATE_DIR"]) if "HARNESS_STATE_DIR" in os.environ else home / "state"
record_path = state / "daemon.json"
DAEMON_CODE = (here / "daemon_code.txt").read_text()


def record():
    try:
        return json.loads(record_path.read_text())
    except (OSError, ValueError):
        return {}


def recorded_pid():
    return record().get("pid")


def birth(pid):
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return "Z" not in subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout


def spawn_daemon(flavour):
    state.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([sys.executable, "-c", DAEMON_CODE, str(state), flavour, str(here / "python3.12"), "daemon", "run"], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=os.environ["HOME"])
    deadline = time.monotonic() + 20
    while flavour != "unrecorded" and recorded_pid() != process.pid and time.monotonic() < deadline:
        time.sleep(0.02)
    return process


def ensure_daemon():
    if mode == "none":
        return
    if mode == "stale":
        state.mkdir(parents=True, exist_ok=True)
        record_path.write_text('{"pid": 2147483000}\n')
        return
    if mode == "foreign":
        state.mkdir(parents=True, exist_ok=True)
        other = subprocess.Popen(["/bin/sleep", "120"], cwd="/", env={"PATH": "/usr/bin:/bin"}, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        record_path.write_text(json.dumps({"pid": other.pid, "birth": birth(other.pid)}) + "\n")
        (here.parent.parent / "foreign.pid").write_text(str(other.pid), encoding="utf-8")
        return
    existing = recorded_pid()
    if existing is not None and alive(existing) and mode != "fresh":
        return
    spawn_daemon({"stubborn": "stubborn", "unrecorded": "unrecorded"}.get(mode, "normal"))
    if mode == "second":
        spawn_daemon("unrecorded")
    if mode == "stray":
        subprocess.Popen(["/bin/sleep", "120"], start_new_session=True, cwd=os.environ["HOME"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if command == ["version", "--json"]:
    print(json.dumps({"ok": True, "packageVersion": "VERSION"}))
    raise SystemExit(0)

if command == ["daemon", "stop", "--json"]:
    if not home.is_dir():
        print(json.dumps({"ok": False, "code": "HYPRIAL_HOME_NOT_INITIALIZED", "error": f"HYPRIAL home does not exist or is not a directory: {home}"}))
        raise SystemExit(1)
    pid = recorded_pid()
    if pid is None:
        print(json.dumps({"ok": False, "stateDir": str(state)}))
        raise SystemExit(1)
    if not record().get("birth") or record()["birth"] != birth(pid):
        print(json.dumps({"ok": False, "stateDir": str(state), "error": "the recorded pid is not the process that wrote the record"}))
        raise SystemExit(1)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    print(json.dumps({"ok": True, "stateDir": str(state), "pid": pid}))
    raise SystemExit(0)

assert command == ["gui", "--no-open", "--json", "--port", "0", "--stdin-stop"], command
ensure_daemon()
runtime = state / "gui" / "daemon"
runtime.mkdir(parents=True, exist_ok=True)
handoff = runtime / "launch.url"
handoff.unlink(missing_ok=True)
fd = os.open(handoff, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, b"http://127.0.0.1:41234/#grant=abc\n")
os.close(fd)
consume = here / "consume-after"
if consume.exists():
    threading.Timer(float(consume.read_text()), lambda: handoff.unlink(missing_ok=True)).start()
record = {"ok": True, "record_version": 1, "url": "http://127.0.0.1:41234/", "host": "127.0.0.1", "port": 41234, "handoff_path": str(handoff), "mode": "normal", "backend_driver": "daemon", "frontend_pid": os.getpid(), "backend_pid": None}
print(json.dumps(record), flush=True)
sys.stdin.read()
handoff.unlink(missing_ok=True)
os._exit(0)
'''.replace("VERSION", VERSION)

APP = r'''
import os, signal, subprocess, sys
from pathlib import Path
contents = Path(sys.argv[0]).resolve().parents[1]
interpreter = contents / "Resources" / "runtime" / "bin" / "python3.12"
instance = Path(os.environ["HOME"]) / ".fake-instance"
if instance.exists():
    try:
        os.kill(int(instance.read_text()), 0)
        raise SystemExit(0)
    except (ValueError, ProcessLookupError):
        pass
instance.write_text(str(os.getpid()))
environment = dict(os.environ)
if "HYPRIAL_HOME" not in environment:
    app_home = Path(environment["HOME"]) / ".hyprialos"
    app_home.mkdir(mode=0o700, exist_ok=True)
    environment["HYPRIAL_HOME"] = str(app_home)
    environment.pop("HARNESS_STATE_DIR", None)
child = subprocess.Popen([str(interpreter), "-I", "-m", "hyprial.cli", "gui", "--no-open", "--json", "--port", "0", "--stdin-stop"], env=environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)


def leave(*_):
    child.stdin.close()
    child.wait(timeout=10)
    instance.unlink(missing_ok=True)
    os._exit(0)


signal.signal(signal.SIGTERM, leave)
child.wait()
instance.unlink(missing_ok=True)
'''


def runtime(base: Path, mode: str = "daemon", *, consume_after: float | None = None) -> Path:
    """A staged-runtime-shaped directory (with a valid manifest) whose interpreter is the fake CLI."""

    stage = load("stage_runtime")
    target = base / "runtime"
    (target / "bin").mkdir(parents=True)
    (target / "lib").mkdir()
    (target / "bin" / "fake.py").write_text(CLI, encoding="utf-8")
    (target / "bin" / "daemon_code.txt").write_text(DAEMON, encoding="utf-8")
    interpreter = target / "bin" / "python3.12"
    interpreter.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "$(dirname "$0")/fake.py" "$@"\n', encoding="utf-8")
    interpreter.chmod(0o755)
    (target / "bin" / "mode").write_text(mode, encoding="utf-8")
    if consume_after is not None:
        (target / "bin" / "consume-after").write_text(str(consume_after), encoding="utf-8")
    lock = stage.load_lock(LOCK)
    facts = {"file": f"hyprial-{VERSION}-py3-none-any.whl", "version": VERSION, "sha256": "0" * 64, "sourceCommit": "c" * 40}
    manifest = stage.build_manifest(target, lock, "aarch64-apple-darwin", facts)
    (target / "runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return target


def app(base: Path, mode: str = "daemon") -> Path:
    """A fake ``HyprialOS.app`` whose bundled runtime is :func:`runtime` and whose binary starts the CLI like the shell."""

    bundle = base / "HyprialOS.app"
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    built = runtime(base, mode, consume_after=0.3)
    built.rename(contents / "Resources" / "runtime")
    binary = contents / "MacOS" / "hyprial-desktop"
    binary.write_text(f"#!{sys.executable}\n{APP}", encoding="utf-8")
    binary.chmod(0o755)
    return bundle


def stop_foreign(pid: int) -> None:
    """Stop the foreign ``sleep`` a test started (its pid comes from the evidence): by pid, birth and command, never anything else."""

    import signal

    row = table().get(pid)
    if row is None or row.command != "/bin/sleep 120":
        return
    ledger = Ledger()
    ledger.record(pid)
    ledger.signal(pid, signal.SIGKILL)
    assert not ledger.stop(kill=10), f"the foreign process {pid} is still running"


def foreign_pids(base: Path) -> list[int]:
    """The foreign ``sleep`` the fake CLI of ``runtime(base)`` started, read from the file it wrote next to itself."""

    marker = base / "foreign.pid"
    try:
        return [int(marker.read_text(encoding="utf-8"))]
    except (OSError, ValueError):
        return []


@contextlib.contextmanager
def foreign_guard(base: Path) -> Iterator[None]:
    """Whatever happens in the body, the foreign process of ``base`` is stopped afterwards (by pid, birth and command)."""

    try:
        yield
    finally:
        for pid in foreign_pids(base):
            stop_foreign(pid)


def reap(root: Path, base: Path) -> None:
    """After a run: nothing may still hold ``root`` or run from ``base``; what does is killed (by birth) and fails the test."""

    import signal

    ledger = Ledger()
    found = load("verify_runtime").carriers(root, command=str(base))
    for pid in found:
        ledger.record(pid)
    for pid in found:
        ledger.signal(pid, signal.SIGKILL)
    survivors = ledger.stop(kill=10)
    assert found == [] and survivors == [], f"processes left after the run: {found}, still running after SIGKILL: {survivors}"


def alive(pid: int) -> bool:
    return load("verify_runtime")._alive(pid)
