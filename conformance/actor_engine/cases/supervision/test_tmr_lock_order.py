# Decision record (file header; no inline comments by repository rule):
# - Regression case for native defect N8 (HQ round-1 review asked for a
#   deterministic fail-before test): the timer thread took the timer-table
#   lock and THEN attached to the interpreter, while a Python caller of
#   send_after/cancel_after holds the GIL and then takes the same lock. Once
#   the timer thread holds the lock and waits for the GIL, a GIL-holding
#   caller that blocks on the lock can never yield the GIL: deadlock (spec 12
#   GIL-01 "holding no engine-internal lock" when calling into Python; FREE-01
#   engine state correct without GIL reliance).
# - Determinism: the child sets sys.setswitchinterval(0.5), so the main
#   thread keeps the GIL for up to half a second at a time while it schedules
#   and cancels zero-delay timers in a tight loop. Every timer fire then
#   meets a GIL holder that enters the table lock within that window; with
#   the defect the first such fire deadlocks the process. The child runs in
#   its own interpreter so a deadlock is observed as a timeout of the parent
#   wait, never as a hung suite.
# - Holds for every backend: the reference timer never waits for the GIL
#   while holding an engine lock, so it passes trivially; the native timer is
#   the actual subject.
"""TMR/FREE: timer firing never deadlocks against GIL-holding send_after/cancel_after callers."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from support.helpers import case

SUITE = Path(__file__).resolve().parents[2]
CHILD_SECONDS = 1.5
PARENT_LIMIT = 30.0

CHILD = r"""
import sys, time
sys.path.insert(0, SUITE)
import backends
module = backends.load(BACKEND)
sys.setswitchinterval(0.5)
engine = module.Engine(policies={"fast": (3, 60.0, 0.01, 0.1, 0.0)}, jitter_seed=0,
                       event_sink=lambda event: None, expected_error_type=LookupError)
actor = engine.spawn("lock-order", lambda: (lambda command: None), 4096, "fast")
deadline = time.monotonic() + SECONDS
rounds = 0
while time.monotonic() < deadline:
    pending = engine.send_after(actor, "lock-order", 0.0, rounds)
    engine.cancel_after(pending)
    engine.send_after(actor, "lock-order", 0.0, rounds)
    rounds += 1
sys.setswitchinterval(0.005)
print("ROUNDS", rounds, flush=True)
print("SHUTDOWN", engine.shutdown(10.0), flush=True)
"""


@case("TMR-03")
@case("FREE-01")
def test_tmr_03_timer_fire_never_deadlocks_against_gil_holding_callers(backend_name: str) -> None:
    """A GIL-holding caller of send_after/cancel_after and firing timers make progress together."""
    code = (CHILD.replace("SUITE", repr(str(SUITE))).replace("BACKEND", repr(backend_name))
            .replace("SECONDS", repr(CHILD_SECONDS)))
    try:
        done = subprocess.run([sys.executable, "-c", code], cwd=str(SUITE), capture_output=True,
                              text=True, timeout=PARENT_LIMIT)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"timer firing deadlocked against a GIL-holding send_after/cancel_after caller "
            f"(no exit within {PARENT_LIMIT} s; spec 12 GIL-01, FREE-01)") from exc
    assert done.returncode == 0, done.stderr
    lines = dict(line.split(" ", 1) for line in done.stdout.strip().splitlines() if " " in line)
    assert int(lines["ROUNDS"]) > 0, done.stdout
    assert lines["SHUTDOWN"] == "True", done.stdout
