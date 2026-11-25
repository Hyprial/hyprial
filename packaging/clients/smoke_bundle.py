#!/usr/bin/env python3
# Decisions (shell-6.md §E.2 M4, M5, M6; packaging/clients/README.md):
# - Black-box smoke of a built `HyprialOS.app` with its bundled runtime: the
#   app binary `Contents/MacOS/hyprial-desktop` is executed directly (never
#   through `open`, which would hand the start to LaunchServices and lose
#   the environment) in its own session, with the isolation environment of
#   verify_runtime.py under a fresh root. `--app-home` leaves HYPRIAL_HOME
#   and HARNESS_STATE_DIR unset so the app's own choice
#   (`<HOME>/.hyprialos`, HOME being the isolated one) is exercised.
#   The smoke's own `daemon stop` then names that home explicitly
#   (HYPRIAL_HOME=<HOME>/.hyprialos): a CLI started without it selects
#   `<HOME>/.hyprial`, which is not the app's home. The top-level
#   `fallbackKilled` is everything any fallback had to stop, the one inside
#   the daemon-stop step included.
# - Refusals before anything starts (BLOCKED): another `hyprial-desktop`
#   already runs (the single-instance plugin would hand our start to it),
#   or the isolated socket path does not fit AF_UNIX.
# - launch: `<state>/gui/daemon/launch.url` must appear within the startup
#   budget and disappear while the 60 s grant TTL has not passed, with the
#   CLI still running; removal before TTL means the webview loaded the page
#   and opened the session (shell-6.md §E.2 M4). The app home is checked by
#   where `launch.url` appears (`<HOME>/.hyprialos/state` with --app-home,
#   else the explicit isolated state directory) and by the unused isolated
#   directories staying empty; the CLI child's own HYPRIAL_HOME is NOT
#   checked, macOS shows no other process's environment. Exactly one CLI
#   child must exist. Unix sockets under the isolated root are listed with
#   their byte length (AF_UNIX evidence for the app home).
# - second-start (optional): the same binary started again with the same
#   environment exits 0 quickly and the first app keeps exactly one CLI.
# - --hold keeps the running app for that many seconds after the launch
#   step and the second start, for a person or a screenshot to look at the
#   (refocused) window.
# - daemon (daemon-first, KP-083; verify_runtime.py header): `hyprial gui`
#   ensures the daemon of the selected home in every state and closing the GUI
#   never stops it, so the daemon of the home used by this run is expected,
#   not a leftover. While the app runs, exactly one daemon must exist for the
#   home: the pid named by the home's own record (`daemon.json` or
#   `bootstrap.json` under the state directory) must be alive and hold the
#   isolated root, and no other process may run `daemon run` on it.
# - stop: `sigterm` (the app stops its child: stdin close, SIGTERM, SIGKILL)
#   or `kill9` (the CLI must see stdin EOF and exit within 5 s). M5 (kill9)
#   then asserts the daemon survived with the same pid and was still the only
#   one, restarts the app with the same app-chosen home and asserts the new
#   app is attached to that same daemon (same pid, still exactly one), then
#   stops the restarted app with SIGTERM.
# - daemon-stop: the expected daemon is stopped with `hyprial daemon stop
#   --json` for that home through the bundled runtime (verify_runtime.
#   stop_daemon) and its exit confirmed; if the pid outlives the bound the
#   fallback kill_carriers acts and the step FAILs as a failed graceful stop.
#   Afterwards no process may hold anything under the isolated root and no
#   process may run the bundle's interpreter (verify_runtime.carriers): the
#   orphans check is exactly as strict as before for everything but that one
#   daemon.
# - integrity: `codesign --verify --deep --strict` of the app and the
#   runtime manifest check of `Contents/Resources/runtime`, after the run.
# - Cleanup always (decision smoke-interrupt-cleanup-20261120): the cleanup
#   runs with SIGINT, SIGTERM and SIGHUP ignored (previous handlers restored
#   after), so a second signal cannot abort it. `main` maps SIGTERM and SIGHUP
#   to an exception (SIGINT is the KeyboardInterrupt) so the `finally` runs
#   for all three; the run then reports runner BLOCKED "interrupted by <SIG>",
#   evidence key `interrupted`, and exits 130 / 143 / 129. Nothing in
#   scripts/build/package-macos-shell.sh reads the value (any non-zero marks
#   the stage failed), 130 is what an interrupted smoke exited before, and
#   143 / 129 are the shell convention 128 + signal. When `carriers` itself
#   fails (lsof missing, ...) the cleanup still stops what it can name
#   without it: first the app processes it started (by handle, before any
#   `ps`, so a failing `ps` cannot skip them), then the processes whose
#   command contains THIS run's absolute bundled interpreter path; the failure
#   is a `cleanup` BLOCKED step, so the run is never PASS. Every process
#   carrying the root is stopped, the root is removed unless --keep. WebKit data the app writes under the real
#   `~/Library/{WebKit,Caches,HTTPStorages}/com.hyprial.hyprialos` is not
#   redirected by HOME (README "App data"); the caller lists and removes it.
# - Never signal a process that is not this run's (decision smoke-foreign-
#   process-20261120; the smoke runs on a release operator's Mac next to the
#   real daemon and the installed app). The earlier fallback selector "pid
#   named in the home's daemon/bootstrap record whose command contains
#   ` daemon run`" was DELETED, not tightened: a record carries no birth, so
#   after pid reuse it could name the operator's daemon, and with the
#   requirement "and the command contains this run's interpreter path" it is a
#   strict subset of the interpreter selector, adding nothing. Every pid from
#   any selector must also satisfy pid > 1 and not be this process or its
#   parent. A pid that cannot be signalled (PermissionError) is never retried
#   and is reported as left; when the process listing itself fails or times
#   out after the handles were stopped, the remainder is unknown and
#   `leftAtExit` is null, never `[]`.
# - A signal the caller ignores (`nohup`, a background job's SIGINT) stays
#   ignored: `main` installs a handler only for a signal whose disposition is
#   not SIG_IGN, so such a run completes as it always did.
# - `ps` here is bounded by verify_runtime.PS_TIMEOUT (its header says why);
#   a timeout is a runner BLOCKED step or, in the cleanup, the fallback.
# - Not closed: a signal arriving between the end of `_drive` and the first
#   `signal.signal(..., SIG_IGN)` of the cleanup can still raise out of the
#   `finally` (a sigmask swap shrinks that window but cannot close it), and
#   one arriving between `Popen` returning and `shells.append` leaves that
#   app unnamed by handle; the interpreter selector still finds it.
"""Launch smoke of a built HyprialOS.app with its bundled runtime (macOS).

Exit status 0 PASS, 1 FAIL, 2 BLOCKED; one verdict line on stdout. An interrupted
run is never PASS and exits 128 + the signal: 130 SIGINT, 143 SIGTERM, 129 SIGHUP.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

HERE = Path(__file__).resolve().parent
GRANT_TTL = 60.0
BINARY = "Contents/MacOS/hyprial-desktop"
INTERRUPTS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
INTERRUPT_STATUS = {"SIGINT": 130, "SIGTERM": 143, "SIGHUP": 129}


def _load_verify() -> Any:
    spec = importlib.util.spec_from_file_location("_hyprialos_verify_runtime", HERE / "verify_runtime.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify = _load_verify()
Step = verify.Step


def _processes() -> list[tuple[int, int, str]]:
    listing = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True, check=False, timeout=verify.PS_TIMEOUT)
    rows = []
    for line in listing.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def running_shells() -> list[int]:
    return sorted(pid for pid, _, text in _processes() if text.split(" ", 1)[0].endswith("/hyprial-desktop") or text.startswith("hyprial-desktop"))


def children(pid: int) -> list[tuple[int, str]]:
    return [(child, text) for child, parent, text in _processes() if parent == pid]


def _wait(predicate: Any, timeout: float, step: float = 0.05) -> float | None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if predicate():
            return time.monotonic() - started
        time.sleep(step)
    return None


def _start_app(app: Path, env: dict[str, str], root: Path, log: Path) -> subprocess.Popen[bytes]:
    with log.open("ab") as sink:
        return subprocess.Popen([str(app / BINARY)], cwd=str(root / "u"), env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, start_new_session=True)


def _await_launch(shell: subprocess.Popen[bytes], launch: Path, startup_timeout: float) -> tuple[float | None, float | None, str]:
    """(seconds until launch.url appeared, seconds until it was consumed, problem)."""

    appeared = _wait(lambda: launch.exists() or shell.poll() is not None, startup_timeout)
    if appeared is None or not launch.exists():
        return None, None, f"launch.url never appeared (app exit {shell.poll()})"
    appeared_at = time.monotonic()
    consumed = _wait(lambda: not launch.exists(), GRANT_TTL)
    if consumed is None or time.monotonic() - appeared_at >= GRANT_TTL:
        return appeared, None, f"launch.url not consumed within the {GRANT_TTL:.0f} s TTL"
    return appeared, consumed, ""


def _one_daemon(found: Any, expect: int | None = None) -> tuple[str | None, int | None]:
    """(problem, pid): exactly one daemon of the home, the same one as ``expect`` when given."""

    problem = found.problem()
    if problem is None and len(found.expected) != 1:
        problem = f"expected exactly one running daemon of the home, found {found.expected}"
    if problem is None and expect is not None and found.expected != [expect]:
        problem = f"the home's daemon is {found.expected}, not the earlier {expect}"
    return problem, (found.expected[0] if len(found.expected) == 1 else None)


def _drive(app: Path, steps: list[Step], evidence: dict[str, Any], held: _Held, *, root_parent: Path, stop: str, app_home: bool, second_start: bool, startup_timeout: float, hold: float = 0.0, daemon_stop_timeout: float = 30.0) -> None:
    app = app.resolve()
    evidence.update({"app": str(app), "stop": stop, "appHome": app_home})

    others = running_shells()
    if others:
        steps.append(Step("preflight", "BLOCKED", f"hyprial-desktop already running: {others}"))
        return
    root = Path(tempfile.mkdtemp(prefix="sm-", dir=root_parent))
    interpreter = str(app / "Contents" / "Resources" / "runtime" / verify.INTERPRETER)
    held.root, held.interpreter, shells = root, interpreter, held.shells
    env = verify.isolated_environment(root)
    if app_home:
        env.pop("HYPRIAL_HOME")
        env.pop("HARNESS_STATE_DIR")
        state = root / "u" / ".hyprialos" / "state"
        expected_home = root / "u" / ".hyprialos"
    else:
        state = root / "s"
        expected_home = root / "h"
    stop_env = {**env, "HYPRIAL_HOME": str(expected_home)} if app_home else env
    socket_bytes = len(os.fsencode(state / "daemon.sock"))
    fits = socket_bytes <= verify.SOCKET_LIMIT
    steps.append(Step("preflight", "PASS" if fits else "BLOCKED", f"no other shell; daemon socket path {socket_bytes} bytes"))
    if not fits:
        return
    log = root / "app.log"
    shell = _start_app(app, env, root, log)
    shells.append(shell)
    evidence["appPid"] = shell.pid
    launch = state / "gui" / "daemon" / "launch.url"
    appeared, consumed, problem = _await_launch(shell, launch, startup_timeout)
    cli = children(shell.pid)
    evidence["cli"] = [{"pid": pid, "command": text.replace(str(app), "<app>")} for pid, text in cli]
    if problem:
        steps.append(Step("launch", "FAIL", problem))
        return
    evidence["launchUrlAppearedSeconds"] = round(appeared or 0.0, 2)
    evidence["launchUrlConsumedAfterSeconds"] = round(consumed or 0.0, 2)
    unused_empty = not app_home or not any((root / "h").iterdir()) and not any((root / "s").iterdir())
    home_ok = len(cli) == 1 and unused_empty
    steps.append(Step("launch", "PASS" if home_ok else "FAIL", f"appeared after {appeared:.2f} s under <root>/{state.relative_to(root)} (home <root>/{expected_home.relative_to(root)}), consumed {consumed:.2f} s later; CLI children {len(cli)}; unused isolated dirs empty {unused_empty}"))
    if not home_ok:
        return
    sockets = subprocess.run(["find", str(root), "-type", "s"], capture_output=True, text=True, check=False).stdout.split()
    evidence["sockets"] = [{"path": path.replace(str(root), "<root>"), "bytes": len(os.fsencode(path)), "bytesUnderRoot": len(os.fsencode(path)) - len(os.fsencode(str(root)))} for path in sockets]
    if second_start:
        with log.open("ab") as sink:
            second = subprocess.run([str(app / BINARY)], cwd=str(root / "u"), env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, timeout=30, check=False)
        still = children(shell.pid)
        ok = second.returncode == 0 and shell.poll() is None and len(still) == 1
        steps.append(Step("second-start", "PASS" if ok else "FAIL", f"second exit {second.returncode}, first alive {shell.poll() is None}, first app children {len(still)}"))
    problem, daemon_pid = _one_daemon(verify.home_daemons(root, state, interpreter))
    steps.append(Step("daemon", "FAIL" if problem else "PASS", problem or f"exactly one daemon of the home, pid {daemon_pid} (record {verify.record_pids(state).get(daemon_pid)}), holds the isolated root"))
    if problem or daemon_pid is None:
        return
    evidence["daemonPid"] = daemon_pid
    if hold > 0:
        time.sleep(hold)
    cli_pid = cli[0][0]
    started = time.monotonic()
    if stop == "kill9":
        os.kill(shell.pid, signal.SIGKILL)
        shell.wait(timeout=10)
        gone = _wait(lambda: not verify._alive(cli_pid), 5.0)
        steps.append(Step("kill9", "PASS" if gone is not None else "FAIL", f"CLI {cli_pid} gone {gone:.2f} s after SIGKILL of the app" if gone is not None else "CLI still running 5 s after SIGKILL of the app"))
        problem, _ = _one_daemon(verify.home_daemons(root, state, interpreter), daemon_pid)
        steps.append(Step("daemon-survives", "FAIL" if problem else "PASS", problem or f"daemon {daemon_pid} still running after the kill -9 and still the only one"))
        restarted = _start_app(app, env, root, log)
        shells.append(restarted)
        evidence["restartedAppPid"] = restarted.pid
        appeared, consumed, problem = _await_launch(restarted, launch, startup_timeout)
        restart_cli = children(restarted.pid)
        if not problem and len(restart_cli) != 1:
            problem = f"the restarted app has {len(restart_cli)} CLI children, expected 1"
        if not problem:
            problem, _ = _one_daemon(verify.home_daemons(root, state, interpreter), daemon_pid)
        steps.append(Step("restart", "FAIL" if problem else "PASS", problem or f"restarted app {restarted.pid} (launch.url appeared after {appeared:.2f} s, consumed {consumed:.2f} s later) is attached to the same daemon {daemon_pid}, still the only one"))
        if problem:
            return
        os.kill(restarted.pid, signal.SIGTERM)
        try:
            status = restarted.wait(timeout=30)
        except subprocess.TimeoutExpired:
            status = None
        steps.append(Step("restart-stop", "PASS" if status is not None else "FAIL", f"restarted app exit {status} after SIGTERM"))
    else:
        os.kill(shell.pid, signal.SIGTERM)
        try:
            status = shell.wait(timeout=30)
        except subprocess.TimeoutExpired:
            status = None
        steps.append(Step("sigterm", "PASS" if status is not None else "FAIL", f"app exit {status} after {time.monotonic() - started:.2f} s"))
    outcome = verify.stop_daemon(root, interpreter, stop_env, daemon_pid, wait=daemon_stop_timeout)
    evidence["daemonStop"] = outcome
    steps.append(verify.daemon_stop_step(outcome, daemon_stop_timeout))
    left_after = _wait(lambda: not verify.carriers(root, command=interpreter), 20.0)
    left = verify.carriers(root, command=interpreter)
    steps.append(Step("orphans", "PASS" if not left else "FAIL", f"no process on the isolated root {left_after:.2f} s after the stops" if not left else f"left running: {left}"))
    signature = subprocess.run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], capture_output=True, text=True, check=False)
    problems, _ = verify.check_manifest(app / "Contents" / "Resources" / "runtime")
    intact = signature.returncode == 0 and not problems
    steps.append(Step("integrity", "PASS" if intact else "FAIL", f"codesign {signature.returncode} {signature.stderr.strip()[-300:]}; runtime {'; '.join(problems[:5]) or 'unchanged'}"))


@dataclass
class _Held:
    """What a run started and must stop: the root, the bundled interpreter and every app process it launched."""

    root: Path | None = None
    interpreter: str = ""
    shells: list[subprocess.Popen[bytes]] = field(default_factory=list)


class Interrupted(Exception):
    """SIGTERM or SIGHUP turned into an exception so the cleanup ``finally`` runs."""

    def __init__(self, signum: int) -> None:
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def _raise_interrupted(signum: int, _frame: Any) -> None:
    raise Interrupted(signum)


@contextlib.contextmanager
def _signals_ignored() -> Iterator[None]:
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in INTERRUPTS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _identified(held: _Held) -> list[int]:
    """Live pids this run can name without ``carriers``: the app processes it started and the processes whose command contains this run's bundled interpreter path; never pid 1, this process or its parent."""

    assert held.root is not None
    found = {shell.pid for shell in held.shells if shell.poll() is None}
    if held.interpreter:
        found |= {pid for pid, text in verify._commands().items() if held.interpreter in text}
    refused = {os.getpid(), os.getppid()}
    return sorted(pid for pid in found if pid > 1 and pid not in refused and verify._alive(pid))


class _Stopped(NamedTuple):
    stopped: list[int]
    left: list[int]
    unsignalled: list[int]
    error: str | None


def _stop_held(held: _Held, seen: set[int], unsignalled: set[int], wait: float) -> None:
    for shell in held.shells:
        if shell.poll() is not None:
            continue
        seen.add(shell.pid)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                shell.send_signal(sig)
                shell.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
            except (ProcessLookupError, PermissionError) as error:
                if isinstance(error, PermissionError):
                    unsignalled.add(shell.pid)
                break


def _stop_identified(held: _Held, wait: float = 10.0) -> _Stopped:
    """SIGTERM then SIGKILL to what :func:`_identified` names, never to anything else; handles first, then ``ps``."""

    seen: set[int] = set()
    unsignalled: set[int] = set()
    _stop_held(held, seen, unsignalled, wait)
    left: list[int] = []
    error = None
    try:
        left = _identified(held)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            targets = [pid for pid in left if pid not in unsignalled]
            if not targets:
                break
            seen |= set(targets)
            for pid in targets:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    unsignalled.add(pid)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                left = _identified(held)
                if all(pid in unsignalled for pid in left):
                    break
                time.sleep(0.05)
    except (OSError, subprocess.SubprocessError) as problem:
        error = str(problem)
    left = sorted({*left, *(shell.pid for shell in held.shells if shell.poll() is None)})
    return _Stopped(sorted(seen - unsignalled), left, sorted(unsignalled & set(left)), error)


def _cleanup(held: _Held, evidence: dict[str, Any], keep: bool) -> str | None:
    """Stop everything the run left, remove the root; the failure message when ``carriers`` could not be used."""

    if held.root is None:
        return None
    root, interpreter = held.root, held.interpreter
    for shell in held.shells:
        if shell.poll() is None:
            try:
                os.kill(shell.pid, signal.SIGTERM)
                shell.wait(timeout=30)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
    earlier = evidence.get("daemonStop", {}).get("fallbackKilled", [])
    failure = None
    try:
        evidence["fallbackKilled"] = sorted({*earlier, *verify.carriers(root, command=interpreter)})
        verify.kill_carriers(root, command=interpreter)
        evidence["leftAtExit"] = verify.carriers(root, command=interpreter)
    except (OSError, subprocess.SubprocessError) as error:
        failure = f"carriers failed ({error}); stopped what the run could name by pid"
        outcome = _stop_identified(held)
        evidence["fallbackKilled"] = sorted({*earlier, *outcome.stopped})
        evidence["leftAtExit"] = None if outcome.error else outcome.left
        if outcome.error:
            failure += f"; naming the remaining processes failed ({outcome.error}), so what is still running is unknown"
        if outcome.unsignalled:
            failure += f"; could not be signalled: {outcome.unsignalled}"
        if outcome.left:
            failure += f"; still running: {outcome.left}"
    if keep:
        evidence["kept"] = str(root)
    else:
        shutil.rmtree(root, ignore_errors=True)
    return failure


def run(app: Path, *, root_parent: Path, stop: str, app_home: bool, second_start: bool, startup_timeout: float, keep: bool, hold: float = 0.0, daemon_stop_timeout: float = 30.0) -> tuple[int, str, dict[str, Any]]:
    steps: list[Step] = []
    evidence: dict[str, Any] = {}
    held = _Held()
    try:
        _drive(app, steps, evidence, held, root_parent=root_parent, stop=stop, app_home=app_home, second_start=second_start, startup_timeout=startup_timeout, hold=hold, daemon_stop_timeout=daemon_stop_timeout)
    except (OSError, subprocess.SubprocessError) as error:
        steps.append(Step("runner", "BLOCKED", str(error)))
    except (KeyboardInterrupt, Interrupted) as error:
        name = "SIGINT" if isinstance(error, KeyboardInterrupt) else str(error)
        evidence["interrupted"] = name
        steps.append(Step("runner", "BLOCKED", f"interrupted by {name}"))
    finally:
        with _signals_ignored():
            failure = _cleanup(held, evidence, keep)
    if failure:
        steps.append(Step("cleanup", "BLOCKED", failure))
    code, line = verify._verdict(steps)
    evidence["steps"] = [item.__dict__ for item in steps]
    evidence["verdict"] = line
    return code, line, evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--root-parent", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--stop", choices=("sigterm", "kill9"), default="sigterm")
    parser.add_argument("--app-home", action="store_true", help="leave HYPRIAL_HOME unset: the app picks <isolated HOME>/.hyprialos")
    parser.add_argument("--second-start", action="store_true")
    parser.add_argument("--startup-timeout", type=float, default=200.0)
    parser.add_argument("--hold", type=float, default=0.0, help="seconds to keep the app running after the launch step")
    parser.add_argument("--daemon-stop-timeout", type=float, default=30.0, help="seconds the daemon of the home has to exit after `daemon stop`")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    handlers = {signal.SIGINT: signal.default_int_handler, signal.SIGTERM: _raise_interrupted, signal.SIGHUP: _raise_interrupted}
    previous = {sig: signal.signal(sig, handler) for sig, handler in handlers.items() if signal.getsignal(sig) != signal.SIG_IGN}
    try:
        code, line, evidence = run(args.app, root_parent=args.root_parent, stop=args.stop, app_home=args.app_home, second_start=args.second_start, startup_timeout=args.startup_timeout, keep=args.keep, hold=args.hold, daemon_stop_timeout=args.daemon_stop_timeout)
    except (KeyboardInterrupt, Interrupted) as error:
        name = "SIGINT" if isinstance(error, KeyboardInterrupt) else str(error)
        print(f"BLOCKED runner interrupted by {name}")
        return INTERRUPT_STATUS[name]
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(line)
    return INTERRUPT_STATUS[evidence["interrupted"]] if "interrupted" in evidence else code


if __name__ == "__main__":
    raise SystemExit(main())
