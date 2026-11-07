"""Upgrade compatibility: a <=0.5.5.3.3 launcher still hands its lock over.

The old interpreter that finishes ``hyprial upgrade`` (or the autoupdate
child) runs the preloaded 0.5.5.3.3 launcher: it holds the identity flock and
passes the descriptor to the newly installed ``daemon run`` through
``pass_fds`` + ``HYPRIAL_IDENTITY_TRANSACTION_FD``.  A new daemon that only
acquired on a fresh open file description would block on the lock its own
inherited descriptor holds, wait out its budget and exit busy -- leaving the
upgraded machine with no daemon.  Remove with the adopt_legacy branch (0.5.5.4).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from hyprial.identity import IdentityTransactionLock
from hyprial.kernel import ProcessFileLock, lock_exclusive
from tests.daemon.home.guard.support import (
    _daemon_environment,
    _short_temp_root,
    _stop_daemon,
    _wait_for,
)

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="legacy launchers never passed a descriptor on Windows"
)

LEGACY_FD_ENV = "HYPRIAL_IDENTITY_TRANSACTION_FD"


def test_daemon_run_adopts_a_legacy_launchers_held_descriptor() -> None:
    with tempfile.TemporaryDirectory(prefix="hyprial-lg-", dir=_short_temp_root()) as raw:
        root = Path(raw)
        home = root / "home"
        home.mkdir()
        state = root / "state"
        lock_path = home / ".identity-transaction.lock"
        # Exactly what the 0.5.5.3.3 launcher does before spawning.
        with lock_path.open("a+b") as held:
            lock_exclusive(held.fileno(), blocking=False)
            environment = _daemon_environment(home, state, "legacy-handoff")
            environment[LEGACY_FD_ENV] = str(held.fileno())
            environment["HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT"] = "3"
            process = subprocess.Popen(
                [sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(held.fileno(),),
            )
            try:
                _wait_for(
                    lambda: (state / "daemon.sock").exists() or process.poll() is not None,
                    timeout=60.0,
                )
                assert process.poll() is None, "upgraded daemon exited instead of adopting"
                assert (state / "daemon.sock").exists()
            except BaseException:
                _stop_daemon(process)
                raise
        try:
            # Launcher closed its copy and the daemon released at ready: free.
            _wait_for(lambda: ProcessFileLock(lock_path).probe() == "free", timeout=15.0)
        finally:
            _stop_daemon(process)


def test_adopt_legacy_ignores_a_descriptor_that_is_not_the_home_lock(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / ".identity-transaction.lock"
    lock_path.touch()
    with (tmp_path / "other").open("a+b") as other:
        environ = {LEGACY_FD_ENV: str(other.fileno())}
        assert IdentityTransactionLock.adopt_legacy(tmp_path, environ) is None
        assert LEGACY_FD_ENV not in environ  # never leaks to descendants
    assert IdentityTransactionLock.adopt_legacy(tmp_path, {LEGACY_FD_ENV: "x"}) is None
    assert IdentityTransactionLock.adopt_legacy(tmp_path, {}) is None


def test_adopt_legacy_refuses_a_descriptor_another_holder_locked(tmp_path: Path) -> None:
    holder = IdentityTransactionLock.acquire(tmp_path, timeout=0.0)
    try:
        lock_path = tmp_path / ".identity-transaction.lock"
        with lock_path.open("a+b") as stranger:
            environ = {LEGACY_FD_ENV: str(stranger.fileno())}
            assert IdentityTransactionLock.adopt_legacy(tmp_path, environ) is None
    finally:
        holder.close()
