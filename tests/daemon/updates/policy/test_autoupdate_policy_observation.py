"""Fresh home policy is read on the bounded AutoUpdate effect owner.

Deflake (2026-11-03, workflow wf-ff332cef #implDF): in the timed-out-read
probes the 0.01s/0.1s read timeout doubles as the window in which the
effect-owner thread must reach the patched read; on the CI shard (3-CPU
cgroup quota, -n 2) the TimeoutError can win that race (run 45455), so the
immediately following assert entered.is_set() read an Event that was simply
not scheduled yet. After the raises, the probe now waits for the entered
Event within the CI-declared HYPRIAL_TEST_WAIT_SECONDS budget before
asserting retention; every later assertion (retained entry, release, stop,
drained ledger) is unchanged.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

import hyprial.daemon.impl.updates as updates
from hyprial.daemon.impl.autoupdate.actor import AutoUpdateAuthority
from hyprial.kernel.impl.actor_runtime import AdmissionResult

WAIT_TIMEOUT_SECONDS = float(os.environ.get("HYPRIAL_TEST_WAIT_SECONDS", "5.0"))


def test_fresh_home_switch_is_observed_off_caller_thread(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"autoUpgrade": True}))
    actor = AutoUpdateAuthority(state_dir=tmp_path / "state", hyprial_home=home)
    caller = threading.get_ident()
    observed: list[int] = []
    original = actor._policy._auto_upgrade_enabled

    def on_effect() -> bool:
        observed.append(threading.get_ident())
        return original()

    monkeypatch.setattr(actor._policy, "_auto_upgrade_enabled", on_effect)
    try:
        assert actor._hyprial_home == home
        assert actor._auto_upgrade_enabled() is True
        updates.write_auto_upgrade(False, home)
        assert actor._auto_upgrade_enabled() is False
        assert len(observed) == 2
        assert all(thread != caller for thread in observed)
    finally:
        assert actor.stop(timeout=2)


def test_timed_out_policy_read_retains_effect_until_completion(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"autoUpgrade": True}))
    actor = AutoUpdateAuthority(state_dir=tmp_path / "state", hyprial_home=home)
    entered, release = threading.Event(), threading.Event()

    def held_read() -> bool:
        entered.set()
        assert release.wait(3)
        return True

    monkeypatch.setattr(actor._policy, "_auto_upgrade_enabled", held_read)
    try:
        with pytest.raises(TimeoutError, match="accepted auto-update policy read"):
            actor._auto_upgrade_enabled(timeout=0.01)
        deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
        while not entered.is_set():
            assert time.monotonic() < deadline, "policy read never entered the effect owner"
            time.sleep(0.005)
        assert len(actor._policy_reads) == 1
        release.set()
        assert actor.stop(timeout=2)
        assert actor._policy_reads == {}
    finally:
        release.set()
        actor.stop(timeout=2)


def test_timed_out_policy_read_releases_credit_after_effect_refusal(tmp_path, monkeypatch) -> None:
    actor = AutoUpdateAuthority(state_dir=tmp_path / "state", hyprial_home=tmp_path)
    entered, release = threading.Event(), threading.Event()

    def held_refusal(request):
        entered.set()
        assert release.wait(3)
        return AdmissionResult.OVERLOADED

    monkeypatch.setattr(actor._effects, "submit", held_refusal)
    try:
        with pytest.raises(TimeoutError, match="accepted auto-update policy read"):
            actor._auto_upgrade_enabled(timeout=0.1)
        assert entered.is_set()
        assert len(actor._policy_reads) == 1
        release.set()
        assert actor.stop(timeout=2)
        assert actor._policy_reads == {}
    finally:
        release.set()
        actor.stop(timeout=2)
