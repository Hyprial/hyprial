"""Function-level contract of the root conftest's ``_remove_tmp_path`` retry.

Decision (2026-11-26, PR #78 gate run 45792478153): the suite reclaims a passed
test's ``tmp_path`` from ``pytest_runtest_protocol``, and on a loaded runner a
background thread of the test that had just passed was still writing inside
that tree, so ``shutil.rmtree`` failed with ``ENOTEMPTY`` and the xdist worker
died. ``_remove_tmp_path`` now retries ``rmtree`` for a bounded window on
``ENOTEMPTY``/``EBUSY``/``EEXIST`` only. This module pins every edge of that
decision, because the two ways to get it wrong are both silent: waiting on the
suite's routine path (more than 30,000 cleanups per run) and swallowing an
error that should have failed the worker. Each case asserts the outcome *and*
the number of ``rmtree`` and sleep calls. The clock and the sleep are replaced,
so no test here waits in real time.

Decision (2026-11-26): the scripted ``rmtree`` hands the conftest's real
``onexc`` to the scripted ``os.rmdir``, the way ``shutil._rmtree_safe_fd``
does, so the callback path under test is the real callback and not a copy.
"""

from __future__ import annotations

import errno
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

import tests.conftest as root_conftest

PATH = Path("/nowhere/reclaim/unit")
BACKOFF: tuple[float, ...] = root_conftest._RMDIR_RETRY_BACKOFF
ATTEMPTS = len(BACKOFF) + 1
WINDOW = sum(BACKOFF)
RETRYABLE = (errno.ENOTEMPTY, errno.EBUSY, errno.EEXIST)
FATAL = (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC, errno.EIO)
Callback = Callable[..., object]


def oserror(code: int, filename: str = str(PATH)) -> OSError:
    error = OSError(code, errno.errorcode[code], filename)
    assert error.errno == code
    assert error.filename == filename
    return error


class ScriptedRmtree:
    """``shutil.rmtree`` replacement for ``PATH`` only; every other path goes to the real one.

    ``shutil.rmtree`` is process-wide, and the root conftest itself calls it after the test
    function returned (its isolated-home teardown, then this test's own reclamation) before
    ``monkeypatch`` restores the attribute. Scripting only ``PATH`` keeps those calls real.
    The stdlib strips ``direntry``/``dir_fd`` before it calls a user ``onexc``, so the callback
    sees exactly three positional arguments.
    """

    def __init__(self, *errors: BaseException) -> None:
        self.errors = errors
        self.calls: list[Path] = []
        self.pass_through = 0
        self.real = staticmethod(shutil.rmtree)

    def __call__(self, path, *args, **kwargs) -> None:
        if os.fspath(path) != str(PATH):
            self.pass_through += 1
            self.real(path, *args, **kwargs)
            return
        self.calls.append(Path(path))
        index = len(self.calls) - 1
        if index < len(self.errors):
            raise self.errors[index]

    @property
    def count(self) -> int:
        return len(self.calls)


class ThroughOnexc(ScriptedRmtree):
    """Fails by calling the handed ``onexc`` for ``PATH``, the way ``shutil._rmtree_safe_fd`` does.

    ``os.rmdir`` is looked up when the callback runs, not when the fake is built, so the test
    may replace ``os.rmdir`` after installing the fake and the conftest ``onexc`` retries the
    replacement.
    """

    def __call__(self, path, *args, onexc: Callback | None = None, **kwargs) -> None:
        if os.fspath(path) != str(PATH):
            self.pass_through += 1
            self.real(path, *args, **kwargs)
            return
        assert onexc is not None
        self.calls.append(Path(path))
        onexc(os.rmdir, path, None)


class ScriptedRmdir:
    """``os.rmdir`` replacement: raises the scripted errors in order for ``PATH`` only.

    The real ``shutil.rmtree`` calls ``os.rmdir`` by *name* with ``dir_fd=``; those calls, and
    every other path, go to the real ``os.rmdir``. Only the conftest ``onexc`` retries
    ``os.rmdir(PATH)``, which is the call this class scripts.
    """

    def __init__(self, *errors: BaseException) -> None:
        self.errors = errors
        self.calls = 0
        self.real = staticmethod(os.rmdir)

    def __call__(self, path, *args, **kwargs) -> None:
        if kwargs.get("dir_fd") is not None or os.fspath(path) != str(PATH):
            self.real(path, *args, **kwargs)
            return
        if self.calls < len(self.errors):
            error = self.errors[self.calls]
            self.calls += 1
            raise error
        self.real(path, *args, **kwargs)


class StepClock:
    """``time.monotonic`` replacement: each call reports ``step`` seconds more than the last."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 1000.0
        self.step = step
        self.calls = 0

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        self.calls += 1
        return value


class Sleeps:
    """``time.sleep`` replacement: records the requested durations, never waits."""

    def __init__(self) -> None:
        self.durations: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)


class Rig:
    def __init__(self, rmtree: ScriptedRmtree, sleeps: Sleeps, clock: StepClock) -> None:
        self.rmtree = rmtree
        self.sleeps = sleeps
        self.clock = clock

    @property
    def slept(self) -> float:
        return sum(self.sleeps.durations)


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch) -> Callable[[ScriptedRmtree, StepClock], Rig]:
    def _install(rmtree: ScriptedRmtree, clock: StepClock | None = None) -> Rig:
        sleeps, clock = Sleeps(), clock or StepClock()
        monkeypatch.setattr(root_conftest.shutil, "rmtree", rmtree)
        monkeypatch.setattr(root_conftest, "_remove_tmp_path_sleep", sleeps)
        monkeypatch.setattr(root_conftest, "_remove_tmp_path_clock", clock)
        return Rig(rmtree, sleeps, clock)

    return _install


def test_r0_a_first_try_success_removes_the_tree_once_and_never_sleeps(
    tmp_path: Path, install: Callable[[ScriptedRmtree, StepClock], Rig]
) -> None:
    target = tmp_path / "passed0"
    (target / "state").mkdir(parents=True)
    (target / "state" / "runtime-capabilities.json").write_text("{}", encoding="utf-8")

    class Counting(ScriptedRmtree):
        def __call__(self, path, *args, **kwargs) -> None:
            self.calls.append(Path(path))
            self.real(path, *args, **kwargs)

    rig = install(Counting())

    root_conftest._remove_tmp_path(target)

    assert not target.exists()
    assert rig.rmtree.calls == [target]
    assert rig.sleeps.durations == []
    assert rig.clock.calls == 1


def test_r0_the_window_is_bounded_to_one_second_and_seven_retries() -> None:
    assert len(BACKOFF) == 7
    assert 0.5 <= WINDOW <= 1.0
    assert list(BACKOFF) == sorted(BACKOFF)
    assert ATTEMPTS == 8


@pytest.mark.parametrize("code", RETRYABLE)
def test_r1_and_r4_transient_errors_are_retried_until_rmtree_succeeds(
    install: Callable[[ScriptedRmtree, StepClock], Rig], code: int
) -> None:
    rig = install(ScriptedRmtree(oserror(code), oserror(code)))

    root_conftest._remove_tmp_path(PATH)

    assert rig.rmtree.count == 3
    assert rig.sleeps.durations == list(BACKOFF[:2])


@pytest.mark.parametrize("code", RETRYABLE)
def test_r2_and_r4_a_persistent_error_is_raised_as_is_after_the_configured_attempts(
    install: Callable[[ScriptedRmtree, StepClock], Rig], code: int
) -> None:
    last = oserror(code, str(PATH / "state"))
    rig = install(ScriptedRmtree(*(oserror(code) for _ in range(ATTEMPTS - 1)), last))

    with pytest.raises(OSError) as excinfo:
        root_conftest._remove_tmp_path(PATH)

    assert excinfo.value is last
    assert excinfo.value.errno == code
    assert excinfo.value.filename == str(PATH / "state")
    assert rig.rmtree.count == ATTEMPTS
    assert rig.sleeps.durations == list(BACKOFF)
    assert rig.slept == pytest.approx(WINDOW)


@pytest.mark.parametrize("code", FATAL)
def test_r3_an_error_outside_the_retry_set_is_raised_at_once(
    install: Callable[[ScriptedRmtree, StepClock], Rig], code: int
) -> None:
    error = oserror(code, str(PATH / "secret"))
    rig = install(ScriptedRmtree(error))

    with pytest.raises(OSError) as excinfo:
        root_conftest._remove_tmp_path(PATH)

    assert excinfo.value is error
    assert excinfo.value.errno == code
    assert excinfo.value.filename == str(PATH / "secret")
    assert rig.rmtree.count == 1
    assert rig.sleeps.durations == []
    assert rig.clock.calls == 1


def test_r5_a_missing_path_stays_silent(
    install: Callable[[ScriptedRmtree, StepClock], Rig]
) -> None:
    rig = install(ScriptedRmtree(FileNotFoundError(errno.ENOENT, "No such file or directory", str(PATH))))

    root_conftest._remove_tmp_path(PATH)

    assert rig.rmtree.count == 1
    assert rig.sleeps.durations == []


@pytest.mark.parametrize("code", FATAL)
def test_r6_an_oserror_raised_inside_onexc_is_raised_as_is(
    monkeypatch: pytest.MonkeyPatch, install: Callable[[ScriptedRmtree, StepClock], Rig], code: int
) -> None:
    error = oserror(code, str(PATH / "state"))
    rig = install(ThroughOnexc())
    rmdir = ScriptedRmdir(error)
    monkeypatch.setattr(root_conftest.os, "rmdir", rmdir)

    with pytest.raises(OSError) as excinfo:
        root_conftest._remove_tmp_path(PATH)

    assert excinfo.value is error
    assert excinfo.value.errno == code
    assert rmdir.calls == 1
    assert rig.rmtree.count == 1
    assert rig.sleeps.durations == []


def test_r6b_a_transient_error_inside_onexc_is_retried_like_one_from_rmtree(
    monkeypatch: pytest.MonkeyPatch, install: Callable[[ScriptedRmtree, StepClock], Rig]
) -> None:
    rig = install(ThroughOnexc())
    rmdir = ScriptedRmdir(oserror(errno.ENOTEMPTY), oserror(errno.ENOTEMPTY))
    monkeypatch.setattr(root_conftest.os, "rmdir", rmdir)

    root_conftest._remove_tmp_path(PATH)

    assert rig.rmtree.count == 3
    assert rmdir.calls == 2
    assert rig.sleeps.durations == list(BACKOFF[:2])


def test_r3b_an_oserror_without_an_errno_is_raised_at_once(
    install: Callable[[ScriptedRmtree, StepClock], Rig]
) -> None:
    error = OSError("Cannot call rmtree on a symbolic link")
    rig = install(ScriptedRmtree(error))

    with pytest.raises(OSError) as excinfo:
        root_conftest._remove_tmp_path(PATH)

    assert excinfo.value is error
    assert excinfo.value.errno is None
    assert rig.rmtree.count == 1
    assert rig.sleeps.durations == []


def test_r7_when_the_clock_runs_out_the_last_error_is_raised_as_is(
    install: Callable[[ScriptedRmtree, StepClock], Rig]
) -> None:
    errors = tuple(oserror(errno.ENOTEMPTY, str(PATH / "state")) for _ in range(ATTEMPTS))
    clock = StepClock(step=WINDOW / 3)
    rig = install(ScriptedRmtree(*errors), clock)

    with pytest.raises(OSError) as excinfo:
        root_conftest._remove_tmp_path(PATH)

    assert excinfo.value is errors[2]
    assert excinfo.value.errno == errno.ENOTEMPTY
    assert excinfo.value.filename == str(PATH / "state")
    assert rig.rmtree.count == 3
    assert rig.sleeps.durations == list(BACKOFF[:2])
    assert rig.slept < WINDOW


def test_r7b_a_window_that_outlives_every_backoff_still_stops_at_the_last_attempt(
    install: Callable[[ScriptedRmtree, StepClock], Rig]
) -> None:
    errors = tuple(oserror(errno.ENOTEMPTY, str(PATH / "state")) for _ in range(ATTEMPTS))
    clock = StepClock(step=WINDOW / 10_000)
    rig = install(ScriptedRmtree(*errors), clock)

    with pytest.raises(OSError) as excinfo:
        root_conftest._remove_tmp_path(PATH)

    assert excinfo.value is errors[-1]
    assert rig.rmtree.count == ATTEMPTS
    assert rig.sleeps.durations == list(BACKOFF)
