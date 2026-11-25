"""Deletion reserves the actor across stop, external commit and settlement.

Tests use isolated actor/effect executors and synthetic workers; no SDK,
platform credentials, configuration files or user daemon are accessed.
"""

from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time

import pytest

from hyprial.daemon.impl.adapters.lark.runtime.commands import _BeginRemoval, _Reconcile
from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.kernel import ChannelConfiguration
from tests.daemon.adapters.lark.lifecycle.test_lark_lifecycle_guards import (
    _Launcher,
    _SpawnGatedLauncher,
    _gateway,
)


def test_stop_keeps_reservation_until_matching_commit() -> None:
    launcher = _Launcher()
    channels = ChannelConfiguration(gateways=(_gateway("app"),))
    runtime = AdapterRuntime(launcher, channels)
    try:
        runtime.start("app")
        runtime.begin_removal("app", "private-removal-proof")
        runtime.begin_removal("app", "private-removal-proof")
        row = runtime.status("app")[0]
        assert row["processRunning"] is False
        assert row["desired"] is False
        assert "private-removal-proof" not in str(row)
        assert row["lifecycleAgeSeconds"] >= 0
        for action in (
            lambda: runtime.start("app"),
            lambda: runtime.remove("app"),
            lambda: runtime.reload_gateways(channels),
            lambda: runtime.restore(()),
            lambda: runtime.begin_removal("app", "another-proof"),
            lambda: runtime.finish_removal("app", "another-proof", committed=True),
        ):
            with pytest.raises(RuntimeError, match="removal"):
                action()
        runtime.reconcile()
        assert launcher.calls == 1
        runtime.finish_removal("app", "private-removal-proof", committed=True)
        assert runtime.gateway_names == ()
        assert runtime.status("app") == ()
        with pytest.raises(RuntimeError, match="not configured"):
            runtime.start("app")
    finally:
        runtime.stop()


def test_rollback_preserves_configuration_and_allows_explicit_start() -> None:
    launcher = _Launcher()
    runtime = AdapterRuntime(launcher, ChannelConfiguration(gateways=(_gateway("app"),)))
    try:
        runtime.begin_removal("app", "proof")
        runtime.finish_removal("app", "proof", committed=False)
        assert runtime.gateway_names == ("app",)
        assert runtime.start("app")
    finally:
        runtime.stop()


def test_unsettled_stop_cannot_expire_or_release_reservation() -> None:
    clock = [0.0]
    entered = threading.Event()
    release = threading.Event()
    launcher = _Launcher()
    runtime = AdapterRuntime(
        launcher,
        ChannelConfiguration(gateways=(_gateway("app"),)),
        clock=lambda: clock[0],
        start_deadline_seconds=1,
    )
    try:
        runtime.start("app")
        process = launcher.processes[0]
        original_stop = process.stop

        def stop(timeout: float = 5) -> None:
            entered.set()
            assert release.wait(5)
            original_stop(timeout)

        process.stop = stop
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(runtime.begin_removal, "app", "proof")
            try:
                assert entered.wait(2)
                clock[0] = 100
                result: Future[int] = Future()
                runtime._submit(_Reconcile(result), result, 2)
                assert not pending.done()
                for committed in (False, True):
                    with pytest.raises(RuntimeError, match="unsettled"):
                        runtime.finish_removal("app", "proof", committed=committed)
                with pytest.raises(RuntimeError, match="removal"):
                    runtime.start("app")
            finally:
                release.set()
            pending.result(timeout=2)
        runtime.finish_removal("app", "proof", committed=True)
        assert launcher.calls == 1
    finally:
        release.set()
        runtime.stop()


def test_failed_stop_refuses_commit_but_can_abort_after_settlement() -> None:
    launcher = _Launcher()
    runtime = AdapterRuntime(launcher, ChannelConfiguration(gateways=(_gateway("app"),)))
    try:
        runtime.start("app")
        process = launcher.processes[0]
        original_stop = process.stop

        def fail_stop(timeout: float = 5) -> None:
            raise RuntimeError("synthetic stop failure")

        process.stop = fail_stop
        with pytest.raises(RuntimeError, match="synthetic stop failure"):
            runtime.begin_removal("app", "proof")
        with pytest.raises(RuntimeError, match="not stopped"):
            runtime.finish_removal("app", "proof", committed=True)
        process.stop = original_stop
        runtime.finish_removal("app", "proof", committed=False)
        assert runtime.remove("app")
    finally:
        runtime.stop()


def test_caller_timeout_preserves_reservation_and_late_stop_settles() -> None:
    entered = threading.Event()
    release = threading.Event()
    launcher = _Launcher()
    runtime = AdapterRuntime(launcher, ChannelConfiguration(gateways=(_gateway("app"),)))
    try:
        runtime.start("app")
        process = launcher.processes[0]
        original_stop = process.stop

        def stop(timeout: float = 5) -> None:
            entered.set()
            assert release.wait(5)
            original_stop(timeout)

        process.stop = stop
        result: Future[bool] = Future()
        with pytest.raises(RuntimeError, match="timed out"):
            runtime._submit(_BeginRemoval("lost-reply", "app", "proof", result), result, 0.02)
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="unresolved"):
            runtime.begin_removal("app", "proof")
        with pytest.raises(RuntimeError, match="unsettled"):
            runtime.finish_removal("app", "proof", committed=False)
        release.set()
        deadline = time.monotonic() + 2
        while True:
            try:
                runtime.begin_removal("app", "proof")
                break
            except RuntimeError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        runtime.finish_removal("app", "proof", committed=True)
        assert launcher.calls == 1
    finally:
        release.set()
        runtime.stop()


def test_reservation_survives_actor_generation_restart() -> None:
    from hyprial.kernel import ActorState

    launcher = _Launcher()
    runtime = AdapterRuntime(launcher, ChannelConfiguration(gateways=(_gateway("app"),)))
    try:
        runtime.start("app")
        runtime.begin_removal("app", "proof")
        cell = runtime._cell
        assert cell is not None
        assert cell.tell(object()).value == "accepted"
        deadline = time.monotonic() + 3
        while True:
            snapshot = cell.snapshot()
            if snapshot.generation > 1 and snapshot.state is ActorState.RUNNING:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with pytest.raises(RuntimeError, match="removal"):
            runtime.start("app")
        runtime.begin_removal("app", "proof")
        runtime.finish_removal("app", "proof", committed=True)
        assert runtime.gateway_names == ()
        assert launcher.calls == 1
    finally:
        runtime.stop()


def test_inflight_restore_prevents_removal_before_stop_is_submitted() -> None:
    from hyprial.daemon.impl.adapters.lark.runtime.commands import _Restore

    launcher = _SpawnGatedLauncher("app")
    runtime = AdapterRuntime(launcher, ChannelConfiguration(gateways=(_gateway("app"),)))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            restored: Future = Future()
            pending = pool.submit(runtime._submit, _Restore(("app",), restored), restored, 5)
            try:
                assert launcher.entered.wait(2)
                with pytest.raises(RuntimeError, match="lifecycle is in progress"):
                    runtime.begin_removal("app", "proof")
                assert not runtime._authority.removals
            finally:
                launcher.release.set()
            pending.result(timeout=2)
        runtime.begin_removal("app", "proof")
        runtime.finish_removal("app", "proof", committed=True)
    finally:
        launcher.release.set()
        runtime.stop()
