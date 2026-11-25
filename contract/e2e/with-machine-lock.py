#!/usr/bin/env python3
"""Run one E2E scenario while holding this machine's single E2E lock.

The controlled gate has always *documented* exclusivity -- E2E-004/013 must
own the test App's event stream, and "a colliding manual run surfaces as an
environmental BLOCKED, never a silent pass" is in e2e-macos-credentialed.yml's header.
Nothing enforced it.  CI's ``concurrency:`` group serializes CI against CI and
knows nothing about a person running a scenario by hand on the same host, so
the actual protection was somebody remembering to announce it in chat.  That
happened today: a dispatch came with "this machine will be busy 10-13 minutes",
which is the symptom of a missing lock rather than a substitute for one.

Refusal, not queueing.  A queued second run *eventually runs*, at a moment it
did not choose, and its wall-clock includes the wait -- another number that is
not comparable to any other number while looking exactly like one.  Refusing
immediately keeps "did not run" distinguishable from "ran and was slow", which
is the same three-state discipline the gate applies everywhere else.

Exit codes: this wrapper contributes exactly one of its own, 5 (MACHINE-BUSY),
and passes every other code through untouched.  The scenario vocabulary
(0=PASS 1=FAIL 2=BLOCKED 3=NOT-RUN 4=AWAITING-USER) is what the gate verdict
reads; collapsing any of it here would destroy the distinctions this work
exists to protect.  5 is deliberately not 2: "never started because the
machine was busy" and "ran and hit an environmental block" are different facts
about a release candidate.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

#: Held for the duration of one scenario, not one CI job.  A job-long hold
#: would need a background holder process spanning steps, which reintroduces
#: the orphaned-lock problem that flock does not have: the kernel releases an
#: flock when the holding fd closes, so a crashed or killed holder never
#: leaves a stale lock behind and no reaper is needed.  Between two CI steps
#: nothing is running, so a manual run may legitimately take the machine
#: there -- and the next CI step then records MACHINE-BUSY, which is exactly
#: the signal the header promised and never delivered.
LOCK_PATH_ENV = "HYPRIAL_E2E_LOCK_PATH"
MACHINE_BUSY = 5


def lock_path() -> Path:
    """Where the machine lock lives (overridable so tests need no real host).

    Outside ``~/.hyprial`` on purpose: that tree is hyprial's own state and gets reset
    by scenarios, and a lock must not be removable by the thing it guards.
    """

    configured = os.environ.get(LOCK_PATH_ENV, "").strip()
    if configured:
        return Path(configured)
    return Path.home() / ".hyprial-ops" / "locks" / "e2e-machine.lock"


def _holder_record(label: str) -> str:
    return json.dumps(
        {
            "pid": os.getpid(),
            "label": label,
            "run": os.environ.get("GITHUB_RUN_ID") or "manual",
            "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )


def _describe_holder(stream) -> str:
    """Best-effort description of who holds the lock.

    Advisory only.  The flock is the authority on whether the machine is
    taken; this text merely says who, so a refusal names a culprit instead of
    saying "busy" and leaving the reader to hunt.  It is written after the
    lock is acquired and truncated on release, so a refused caller cannot read
    a previous holder's leftovers -- but if it is ever empty or stale, that
    changes nothing about the refusal itself.  Do not "fix" a missing record
    by adding a pid check: a pidfile is exactly the stale-lock failure mode
    flock avoids.
    """

    try:
        stream.seek(0)
        raw = stream.read().strip()
    except OSError:
        return "holder unknown (record unreadable)"
    return raw or "holder unknown (record empty)"


def _child_exit_code(completed: subprocess.CompletedProcess[bytes]) -> int:
    """The child's code, verbatim; a signal death rendered the shell's way."""

    if completed.returncode < 0:
        return 128 - completed.returncode
    return completed.returncode


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(f"usage: {argv[0]} <label> <command> [args...]", file=sys.stderr)
        return 2
    label = argv[1]
    command = argv[2:]

    path = lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        # Refuse rather than run unlocked: a run that believes it is protected
        # and is not is worse than one that knows it did not start.
        print(f"{label} MACHINE-BUSY: cannot create {path.parent} ({error})")
        return MACHINE_BUSY

    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"{label} MACHINE-BUSY: another E2E run holds {path}; "
                f"this scenario never started. Holder: {_describe_holder(stream)}"
            )
            return MACHINE_BUSY
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(_holder_record(label) + "\n")
            stream.flush()
            # ---- everything the lock covers is this one line ----
            # The scope is the scenario process, start to exit: nothing before
            # it, nothing after it.  Stated this way on purpose -- a lock whose
            # extent has to be reconstructed by reading around it is one nobody
            # can later say is right or wrong, and "what exactly is inside the
            # lock" is the whole difference between covering the contended
            # section and covering the whole run.
            completed = subprocess.run(command, check=False)
            # ---- end of covered region ----
            return _child_exit_code(completed)
        finally:
            try:
                stream.seek(0)
                stream.truncate()
                stream.flush()
            except OSError:
                pass
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
