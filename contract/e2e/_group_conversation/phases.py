# Decision notes (E2E-031, 2026-11-19):
# - One disposable daemon under a short temp root (Unix socket path budget),
#   its own home, state and socket; a home equal to ~/.hyprial is refused.
# - The verdict counts only the assertions this run was asked to drive.  The
#   mock-model stage GC-11 is opt-in (``--harness pi``, which the hosted job
#   of mock-model-harness-e2e.yml passes); without it GC-11 is printed
#   NOT_RUN and left out of the verdict.  A requested stage whose harness is missing is
#   BLOCKED, never a pass.
# - Teardown always runs (teardown_guard), then the runner lists every live
#   process whose command line names its temp root; any survivor fails GC-12.
"""E2E-031: group conversations on a single isolated node."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import time
from pathlib import Path

from _daemon_runner import (
    IsolatedDaemon,
    hyprial_executable,
    scenario_tempdir,
    short_temp_root,
    teardown_guard,
)
from .cli import SessionHeartbeat, owner

from . import steps
from .fixture import BLOCKED, FAIL, NOT_RUN, PASS, Report, evidence

SCENARIO = "E2E-031"
NONCE = f"{os.getpid():x}{int(time.time()):x}"
AGENTS = ("A", "B", "C")
LEFTOVER_WAIT = 15.0

BASE = (
    ("GC-01", "cli:message group create/show/list", ("GC-00",), steps.gc01_create),
    ("GC-00b", "fixture: session.register + heartbeat thread", ("GC-00",), steps.sessions),
    ("GC-02", "cli:owner message send --conversation; durable inbox rows", ("GC-00b", "group"),
     steps.gc02_owner_send),
    ("GC-03", "cli:agent-session message reply; message history", ("GC-02",), steps.gc03_turns),
    ("GC-04", "cli:members add; agent-session message send --mention", ("GC-03",),
     steps.gc04_mention),
    ("GC-05", "cli:agent-session message reply default @author vs --no-mention", ("GC-04",),
     steps.gc05_reply_needs_mention),
    ("GC-06", "durable inbox rows and pending list of the unmentioned member", ("GC-04",),
     steps.gc06_unmentioned),
    ("GC-07", "cli:members remove; owner send; removed member's history read", ("GC-04",),
     steps.gc07_remove),
    ("GC-08", "daemon stop/init between send and turn; durable rows; restart pass log",
     ("GC-07",), steps.gc08_restart),
    ("GC-09", "cli:message conversation list / history for the 1:1 conversation",
     ("GC-02",), steps.gc09_one_to_one),
    ("GC-10", "cli:message history --conversation <g>", ("GC-08",), steps.gc10_history),
)


def leftovers(root: Path) -> list[str]:
    """Live processes whose command line names this scenario's temp root."""

    listing = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                             timeout=10, check=False)
    marker = str(root)
    return [line.strip() for line in listing.stdout.splitlines()
            if marker in line and str(os.getpid()) != line.split(maxsplit=1)[0]]


def alive(pids: list[int]) -> list[str]:
    """The recorded daemon pids that still name a live process."""

    found = []
    for pid in pids:
        listing = subprocess.run(["ps", "-o", "pid=,stat=,command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=10, check=False)
        line = listing.stdout.strip()
        if line and not line.split()[1].startswith("Z"):
            found.append(line)
    return found


def check_visible(pids: list[int]) -> None:
    """Positive control for GC-12: the live daemon must show up in ``alive``."""

    if not pids or not alive(pids):
        raise AssertionError(f"the owned daemon pid is not observable: pids={pids}")


def run(harness: str | None) -> int:
    report = Report(SCENARIO)
    executable = hyprial_executable()
    stack = contextlib.ExitStack()
    with stack:
        provider = None
        if harness is not None:
            from _fake_provider import FakeProvider

            from .harness import REPLY, missing_harness

            missing = missing_harness()
            if missing:
                report.record("GC-11", BLOCKED, "pi headless worker + mock model", missing)
                harness = None
            else:
                provider = stack.enter_context(FakeProvider(reply=REPLY,
                                                            reply_to_harness_requests=True))
        with scenario_tempdir(SCENARIO.lower(), directory=short_temp_root()) as root:
            daemon = IsolatedDaemon(executable, root / "d", f"e2e-gc-{NONCE[:8]}")
            if provider is not None:
                from .harness import provider_environment

                daemon.env.update(provider_environment(provider, daemon.env))
            if daemon.env.get("HYPRIAL_HOME") == str(Path.home() / ".hyprial"):
                raise AssertionError("refusing to run against the production home")
            heartbeat = SessionHeartbeat(daemon)
            with teardown_guard(heartbeat.stop, daemon.stop, daemon.wait_stopped,
                                record=lambda error: evidence("GC-CLEANUP", error=error)):
                daemon.start()
                world = steps.World(daemon=daemon, owner=owner(daemon), nonce=NONCE,
                                    heartbeat=heartbeat)
                world.remember_pid()
                report.run("GC-12a", "ps: the running daemon is visible to the leftover check", (),
                           lambda: check_visible(world.daemon_pids))
                report.run("GC-00", "fixture: agents", (),
                           lambda: steps.setup(world, AGENTS))
                for step, driven, needs, body in BASE:
                    if "group" in needs:
                        needs = tuple(item for item in needs if item != "group")
                        if not steps.ensure_group(world):
                            report.record(step, NOT_RUN, driven, "no group to continue with")
                            continue
                    report.run(step, driven, needs, lambda body=body: body(world))
                if harness is not None and provider is not None:
                    from .harness import gc11_harness_turn

                    report.run("GC-11", "pi headless workers + mock model (harness_read/harness_reply)",
                               ("GC-00",), lambda: gc11_harness_turn(
                                   daemon, provider, root, world.owner, NONCE))
                world.remember_pid()
            deadline = time.monotonic() + LEFTOVER_WAIT
            pids = list(world.daemon_pids) if "world" in locals() else []
            survivors = leftovers(root) + alive(pids)
            while survivors and time.monotonic() < deadline:
                time.sleep(0.5)
                survivors = leftovers(root) + alive(pids)
            evidence("GC-12-pids", daemonPids=pids)
            evidence("GC-12", survivors=survivors)
            report.record("GC-12", FAIL if survivors else PASS, "ps after teardown",
                          json.dumps(survivors)[:2000] if survivors else "")
    if harness is None and "GC-11" not in report.outcomes:
        print(f"RESULT {NOT_RUN} {SCENARIO} GC-11 driven=opt-in (--harness pi); "
              "not part of this verdict", flush=True)
    verdict, code = report.summary()
    print(f"RESULT {verdict.split()[0]} {SCENARIO} group-conversation {verdict.split(' ', 1)[1]}",
          flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=("pi",), default=None,
                        help="also run GC-11 with real headless Pi workers and the mock model")
    args = parser.parse_args(argv)
    try:
        return run(args.harness)
    except Exception as error:  # noqa: BLE001 - the final RESULT line always prints
        print(f"RESULT FAIL {SCENARIO} group-conversation {error}"[:4000], flush=True)
        return 1


__all__ = ["main"]
