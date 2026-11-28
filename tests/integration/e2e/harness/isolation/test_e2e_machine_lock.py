"""One machine, one E2E run -- enforced, and without losing any distinction.

The controlled gate documented exclusivity long before anything enforced it:
CI's ``concurrency:`` group serializes CI against CI and cannot see a person
running a scenario by hand on the same host.  The real protection was somebody
announcing the collision in chat.

These run the wrapper as a real subprocess against a real ``flock``.  A test
that stubbed the locking would prove only that the stub agrees with itself,
and the properties worth pinning here -- that the kernel drops the lock when
the holder dies, that a refusal never starts the child -- are properties of
the kernel, not of the wrapper's control flow.

Decision (2026-11-26): every test here carries ``posix_only("fcntl")`` rather
than the file being listed in ``scripts/ci/windows/posix-only.json``.  All six
run ``with-machine-lock.py``, which imports ``fcntl`` at module level, so
none of them can even start on Windows.  The per-test marks keep that visible
test by test: each of the ten cases is reported as a skip naming ``fcntl``
instead of the whole file vanishing from the collection, and a test added
later that does not run the wrapper is not silently skipped with the rest.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
WRAPPER = ROOT / "contract" / "e2e" / "with-machine-lock.py"
MACHINE_BUSY = 5


def _wait_for_holder(lock: Path, timeout: float = 10.0) -> None:
    """Block until the background holder has taken the lock and said so.

    Polls for the record rather than the file: the wrapper creates the file
    before it acquires, so file existence alone would let a test race ahead of
    the flock and observe a machine that is not yet held.
    """

    deadline = time.monotonic() + timeout
    while True:
        try:
            if lock.read_text(encoding="utf-8").strip():
                return
        except FileNotFoundError:
            pass
        assert time.monotonic() < deadline, "holder never recorded itself"
        time.sleep(0.05)


def _run(lock: Path, label: str, *command: str) -> subprocess.CompletedProcess[str]:
    """Invoke the wrapper, and fail loudly if it ever waits for the lock.

    The timeout is not defensive tidiness.  A wrapper that queued instead of
    refusing would block for as long as the holder lives, and nothing here
    imposes a per-test deadline, so the suite would stall instead of
    reporting -- and a stall is not a test result: it stops the gate without
    naming the property that broke, the same "did not run vs ran and was
    slow" confusion the lock itself exists to prevent.

    With the timeout, removing ``LOCK_NB`` from the wrapper turns this test
    red in 20s instead.  That was measured, not assumed.
    """

    try:
        return subprocess.run(
            [sys.executable, str(WRAPPER), label, *command],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "HYPRIAL_E2E_LOCK_PATH": str(lock)},
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{label} waited for the machine lock instead of refusing; "
            "the wrapper must never queue"
        )


@pytest.mark.posix_only("fcntl")
@pytest.mark.parametrize("code", [0, 1, 2, 3, 4])
def test_every_scenario_exit_code_passes_through_verbatim(
    tmp_path: Path, code: int
) -> None:
    """The gate reads 0/1/2/3/4 as five different facts about a candidate.

    A wrapper that normalised any of them -- non-zero to 1, or AWAITING-USER
    to BLOCKED -- would erase exactly the distinctions the lock work exists to
    protect, and would do it invisibly, since the run would still look like it
    reported something.
    """

    result = _run(
        tmp_path / "e2e.lock",
        "E2E-000",
        sys.executable,
        "-c",
        f"raise SystemExit({code})",
    )

    assert result.returncode == code


@pytest.mark.posix_only("fcntl")
def test_a_second_run_is_refused_and_its_child_never_starts(tmp_path: Path) -> None:
    """Refusal, not queueing: "did not run" must stay visible as itself.

    A queued run eventually runs, at a moment it did not choose, with a
    wall-clock that includes the wait -- a number that is not comparable to
    any other number while looking exactly like one.
    """

    lock = tmp_path / "e2e.lock"
    witness = tmp_path / "child-ran"
    holder = subprocess.Popen(
        [
            sys.executable,
            str(WRAPPER),
            "E2E-013",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
        env={**os.environ, "HYPRIAL_E2E_LOCK_PATH": str(lock)},
    )
    try:
        _wait_for_holder(lock)

        refused = _run(
            lock,
            "E2E-004",
            sys.executable,
            "-c",
            f"open({str(witness)!r}, 'w').write('ran')",
        )
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert refused.returncode == MACHINE_BUSY
    assert not witness.exists(), "a refused run must not execute its scenario"
    # The refusal has to name a culprit; "busy" alone leaves the reader to
    # hunt for which of several possible runs is holding the host.
    assert "MACHINE-BUSY" in refused.stdout
    assert '"label": "E2E-013"' in refused.stdout


@pytest.mark.posix_only("fcntl")
def test_the_lock_is_released_when_the_scenario_finishes(tmp_path: Path) -> None:
    lock = tmp_path / "e2e.lock"

    first = _run(lock, "E2E-006", sys.executable, "-c", "pass")
    second = _run(lock, "E2E-008", sys.executable, "-c", "pass")

    assert (first.returncode, second.returncode) == (0, 0)


@pytest.mark.posix_only("fcntl")
def test_a_killed_holder_leaves_no_stale_lock(tmp_path: Path) -> None:
    """The reason this is an flock and not a pidfile.

    The kernel drops an flock when the holding descriptor closes, so a holder
    that is killed -- a CI step timing out, a laptop losing power mid-run --
    never wedges the host.  A pidfile would need a reaper, and a reaper is a
    second thing that can be wrong about whether the machine is free.
    """

    lock = tmp_path / "e2e.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            str(WRAPPER),
            "E2E-012",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
        env={**os.environ, "HYPRIAL_E2E_LOCK_PATH": str(lock)},
    )
    _wait_for_holder(lock)
    holder.kill()
    holder.wait(timeout=10)

    after = _run(lock, "E2E-014", sys.executable, "-c", "pass")

    assert after.returncode == 0


@pytest.mark.posix_only("fcntl")
def test_the_holder_record_says_who_and_from_which_run(tmp_path: Path) -> None:
    lock = tmp_path / "e2e.lock"
    dumped = tmp_path / "record.json"

    result = _run(
        lock,
        "E2E-009",
        sys.executable,
        "-c",
        f"open({str(dumped)!r},'w').write(open({str(lock)!r}).read())",
    )

    assert result.returncode == 0
    record = json.loads(dumped.read_text(encoding="utf-8"))
    assert record["label"] == "E2E-009"
    assert record["run"]  # "manual" outside CI, the run id inside it
    assert isinstance(record["pid"], int)
    # Truncated on release, so the next refusal cannot quote a dead holder.
    assert lock.read_text(encoding="utf-8").strip() == ""


@pytest.mark.posix_only("fcntl")
def test_a_signal_killed_scenario_is_reported_the_shell_way(tmp_path: Path) -> None:
    """128+N, not -N: the gate records this number and int() would keep it.

    A negative code written to a ``.code`` file parses fine and renders as
    "FAIL (unexpected exit -9)", which reads like a scenario verdict rather
    than a killed process.
    """

    result = _run(
        tmp_path / "e2e.lock",
        "E2E-000",
        sys.executable,
        "-c",
        "import os, signal; os.kill(os.getpid(), signal.SIGKILL)",
    )

    assert result.returncode == 128 + signal.SIGKILL
