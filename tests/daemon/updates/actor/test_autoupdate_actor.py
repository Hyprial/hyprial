import json
import subprocess
import threading
import time
from datetime import datetime

from hyprial.daemon.impl.autoupdate.actor import AutoUpdateAuthority


def test_stale_run_completion_cannot_release_new_active_update(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"autoUpgrade": True}))
    entered, release = threading.Event(), threading.Event()
    calls = []
    completions = []

    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 2:
            entered.set()
            assert release.wait(3)
        return subprocess.CompletedProcess(command, 0)

    actor = AutoUpdateAuthority(
        state_dir=tmp_path / "state", hyprial_home=home, runner=runner
    )
    original = actor._effects._complete

    def complete(event):
        if event.operation_id.startswith("run:"):
            completions.append(event)
        return original(event)

    actor._effects._complete = complete
    try:
        actor.start()
        assert actor.trigger("first")
        _wait(
            lambda: actor.status()["lastExitCode"] == 0 and not actor.status()["active"]
        )
        assert actor.trigger("second")
        assert entered.wait(1)
        actor._runtime.tell(actor._handle, completions[0])
        _wait(
            lambda: (
                actor._runtime.snapshot(actor._handle).queued == 0
                and actor._runtime.snapshot(actor._handle).in_flight == 0
            )
        )
        assert actor.status()["active"]
        assert not actor.trigger("third")
    finally:
        release.set()
        assert actor.stop(timeout=2)


def test_trigger_before_start_is_queued_without_running_child(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"autoUpgrade": True}))
    calls = []
    actor = AutoUpdateAuthority(
        state_dir=tmp_path / "state",
        hyprial_home=home,
        runner=lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )
    try:
        assert actor.trigger("queued")
        _wait(
            lambda: (
                actor._runtime.snapshot(actor._handle).queued == 0
                and actor._runtime.snapshot(actor._handle).in_flight == 0
            )
        )
        assert calls == []
        actor.start()
        _wait(lambda: len(calls) == 1 and not actor.status()["active"])
    finally:
        assert actor.stop(timeout=2)


def _wait(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_default_off_actor_does_not_spawn_or_write_update_artifacts(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    calls = []
    actor = AutoUpdateAuthority(
        state_dir=tmp_path / "state",
        hyprial_home=home,
        runner=lambda *args, **kwargs: calls.append(args),
    )
    try:
        actor.start()
        assert actor.trigger("test")
        _wait(lambda: actor.status()["lastSkipReason"] == "disabled")
        assert actor.status()["autoUpgradeEnabled"] is False
        assert calls == []
        assert not (tmp_path / "state").exists()
    finally:
        assert actor.stop(timeout=2)


def test_stalled_upgrade_does_not_block_status_or_admit_duplicate_run(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"autoUpgrade": True}))
    entered, release = threading.Event(), threading.Event()

    def runner(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return subprocess.CompletedProcess(args[0], 0)

    actor = AutoUpdateAuthority(
        state_dir=tmp_path / "state", hyprial_home=home, runner=runner
    )
    try:
        actor.start()
        assert actor.trigger("test")
        assert entered.wait(1)
        assert actor.status()["active"]
        assert not actor.trigger("duplicate")
        assert not actor.stop(timeout=0.01)
    finally:
        release.set()
        assert actor.stop(timeout=2)


def test_calendar_catches_clock_jump_without_repeated_runs(tmp_path):
    clock = [datetime(2026, 9, 27, 3, 16, 59)]
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"autoUpgrade": True}))
    calls = []
    actor = AutoUpdateAuthority(
        state_dir=tmp_path / "state",
        hyprial_home=home,
        now=lambda: clock[0],
        calendar_recheck_seconds=0.01,
        runner=lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )
    try:
        actor.start()
        _wait(lambda: actor.status()["nextRunAt"] is not None)
        clock[0] = datetime(2026, 9, 27, 3, 20)
        _wait(
            lambda: (
                actor.status()["lastTrigger"] == "schedule"
                and not actor.status()["active"]
            )
        )
        assert len(calls) == 1
    finally:
        assert actor.stop(timeout=2)
