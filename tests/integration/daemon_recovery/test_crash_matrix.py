"""Real ``kill -9`` crash matrix for the serving-start onboarding recovery (Z-3).

Decision: daemon-first-z3-crash-matrix-20261116.  Each row runs the unmodified
``hyprial daemon run`` path in a real process on an isolated short-path home,
kills it with SIGKILL at one stage of onboarding, restarts it and reads the
public ``onboarding.status`` plus the durable operation journal.  The identity
port, the issuer profile and both device provisioners are controlled
(``tests/integration/daemon_lifecycle/child_entry.py``); a real issuer and a
real Tailcat sidecar are never contacted, so no row claims anything about them.
The journal is read only after the daemon that wrote it is dead, or by a
read-only snapshot.  Two rows seed terminal receipts into the journal while no
daemon is alive (the previous owner's receipts, a failed initialize); they are
written through the real ``OperationService`` and are never non-terminal, so
the next daemon's reconciliation leaves them as written.  Rows wait on the
``recovery-finished`` record the controlled entry writes when a recovery pass
ends, not on a fixed sleep.
"""

from __future__ import annotations

import json
import os
import signal
import time
from uuid import uuid4

import pytest

from hyprial.daemon.impl.onboarding.operations import OperationService, journal
from hyprial.daemon.impl.request_catalog.onboarding import CallContext, Surface
from hyprial.identity import DeviceRecord, read_device_record, write_device_record

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX unix-socket and signal lifecycle; Windows is not a current target (KP-080)"
)

OWNER = "z2-owner@example.test"
ISSUER = "https://issuer.example.test"
OTHER_OWNER = "z3-other-owner@example.test"
SETTLE_SECONDS = 60


def _records(lifecycle, kind: str) -> list[tuple[str | None, str]]:
    state = journal.read_journal(lifecycle.state / "onboarding")
    records = () if state is None else state.snapshot.records
    return [(r.account_owner, r.state) for r in records if r.kind == kind]


def _wait_for(lifecycle, child, predicate, what: str):
    deadline = time.monotonic() + SETTLE_SECONDS
    while time.monotonic() < deadline:
        assert child.poll() is None, lifecycle.child_log(child)
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"{what} did not settle: {lifecycle.call('onboarding.status')}")


def _status(lifecycle) -> dict:
    status = lifecycle.call("onboarding.status")
    return {key: status.get(key) for key in (
        "phase", "authenticated", "owner", "initialized", "deviceReady", "nextActions",
    )}


def _authenticate(lifecycle, tag: str) -> None:
    begun = lifecycle.call("auth.login.begin", {"intent": "login", "operationKey": f"z3-login-key-{tag}"})
    got = lifecycle.call("auth.operation.get", {"operationId": begun["operationId"]})
    assert got["operation"]["state"] == "authenticated", got


def _bootstrap_context(owner: str) -> CallContext:
    return CallContext(
        surface=Surface("z3-seed-home", "z3-seed-control", "z3-seed-view", "z3-seed-generation"),
        phase="bootstrap",
        caller="local-bootstrap",
        owner=owner,
    )


def _seed_terminal_receipts(lifecycle, owner: str, initialize: str) -> None:
    service = OperationService.open(lifecycle.state / "onboarding")
    context = _bootstrap_context(owner)
    device = service.begin_device(
        context, "z3-seed-device-" + uuid4().hex, uuid4().hex, uuid4().hex
    )
    service.mark_device(context, device.operation_id, "ready")
    accepted = service.accept_initialize(
        context, "z3-seed-init-" + uuid4().hex, uuid4().hex, uuid4().hex,
        promotion=lambda _record: None,
    )
    service.mark_initialize(context, accepted.operation_id, initialize)


def _owner_records(lifecycle, owner: str) -> list:
    state = journal.read_journal(lifecycle.state / "onboarding")
    return [r for r in state.snapshot.records if r.account_owner == owner]


def _recovery_outcomes(lifecycle, child) -> list[str]:
    return [
        r["outcome"] for r in lifecycle.records()
        if r.get("event") == "recovery-finished" and r.get("pid") == child.pid
    ]


def _wait_recovery_finished(lifecycle, child) -> str:
    return _wait_for(
        lifecycle, child, lambda: (_recovery_outcomes(lifecycle, child) or [None])[-1],
        "recovery pass",
    )


def _kill9(lifecycle, child) -> None:
    pid = child.pid
    os.kill(pid, signal.SIGKILL)
    child.wait(timeout=30)
    lifecycle.wait_dead(pid)


def _restart(lifecycle, **environment: str):
    child = lifecycle.spawn(**environment)
    ping = lifecycle.serving(child)
    assert ping["pid"] == child.pid
    return child


def test_kill_while_promoting_is_completed_on_the_next_serving_start(lifecycle):
    child = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, child)
    lifecycle.login_and_initialize("promote1")
    _kill9(lifecycle, child)

    assert _records(lifecycle, "initialize")[-1][1] in {"promoting", "completed"}
    restarted = _restart(lifecycle)
    status = _wait_for(
        lifecycle, restarted,
        lambda: (s := _status(lifecycle))["initialized"] is True and s,
        "initialize recovery",
    )

    assert status["phase"] == "serving" and status["owner"] == OWNER
    assert (status["authenticated"], status["deviceReady"]) == (True, True)
    assert status["nextActions"] == []
    assert _records(lifecycle, "initialize") == [(OWNER, "completed")]


def test_kill_after_authenticated_before_initialize_is_auto_completed(lifecycle):
    child = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, child)
    _authenticate(lifecycle, "authent1")
    _kill9(lifecycle, child)
    assert json.loads((lifecycle.home / "settings.json").read_text())["owner"] == OWNER
    assert not (lifecycle.home / "device.json").exists()

    restarted = _restart(lifecycle)
    status = _wait_for(
        lifecycle, restarted,
        lambda: (s := _status(lifecycle))["initialized"] is True and s,
        "auto-complete",
    )

    assert (status["deviceReady"], status["nextActions"]) == (True, [])
    assert _records(lifecycle, "device") == [(OWNER, "ready")]
    assert _records(lifecycle, "initialize") == [(OWNER, "completed")]
    for method in ("onboarding.device.ensure", "onboarding.initialize"):
        with pytest.raises(Exception) as refused:
            lifecycle.call(method, {"operationKey": "z3-outside-caller-key"})
        assert getattr(refused.value, "code", None) == "CALLER_NOT_AUTHORIZED", refused.value


def test_a_failed_auto_complete_is_truthful_and_the_next_start_retries(lifecycle):
    child = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, child)
    _authenticate(lifecycle, "failed01")
    _kill9(lifecycle, child)

    failing = _restart(lifecycle, Z3_SERVING_DEVICE="fail")
    _wait_for(lifecycle, failing, lambda: _records(lifecycle, "device") == [(OWNER, "failed")], "failed receipt")
    status = _status(lifecycle)
    assert (status["phase"], status["authenticated"]) == ("serving", True)
    assert (status["initialized"], status["deviceReady"]) == (False, False)
    assert status["nextActions"] == ["device.ensure"]
    assert _records(lifecycle, "initialize") == []
    assert lifecycle.ping()["pid"] == failing.pid
    _kill9(lifecycle, failing)

    retried = _restart(lifecycle)
    status = _wait_for(
        lifecycle, retried,
        lambda: (s := _status(lifecycle))["initialized"] is True and s,
        "retry on the next start",
    )
    assert (status["deviceReady"], status["nextActions"]) == (True, [])
    assert sorted(_records(lifecycle, "device")) == [(OWNER, "failed"), (OWNER, "ready")]


def test_owner_rewritten_after_kill_never_completes_the_previous_owners_receipt(lifecycle):
    child = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, child)
    lifecycle.login_and_initialize("rewrite1")
    _kill9(lifecycle, child)
    settings = json.loads((lifecycle.home / "settings.json").read_text())
    settings.update({"owner": OTHER_OWNER, "identityMode": "casdoor", "identityIssuer": ISSUER})
    (lifecycle.home / "settings.json").write_text(json.dumps(settings))

    restarted = _restart(lifecycle)
    assert _wait_recovery_finished(lifecycle, restarted) == "initialized"
    status = _status(lifecycle)

    assert (status["phase"], status["owner"]) == ("serving", OTHER_OWNER)
    assert (status["initialized"], status["deviceReady"]) == (True, True)
    previous = [(r.state) for r in _owner_records(lifecycle, OWNER) if r.kind == "initialize"]
    assert previous and "completed" not in previous, previous
    assert _records(lifecycle, "initialize").count((OTHER_OWNER, "completed")) == 1
    assert read_device_record(lifecycle.home).owner == OTHER_OWNER
    assert restarted.poll() is None


def test_account_switch_then_kill_is_completed_for_the_new_owner(lifecycle):
    child = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, child)
    _authenticate(lifecycle, "switch01")
    _kill9(lifecycle, child)
    write_device_record(
        lifecycle.home,
        DeviceRecord("z2-node-device", OTHER_OWNER, "nodekey:previous-server", "nodekey:previous-client"),
    )
    _seed_terminal_receipts(lifecycle, OTHER_OWNER, "completed")
    previous = _owner_records(lifecycle, OTHER_OWNER)
    assert len(previous) == 2

    restarted = _restart(lifecycle)
    assert _wait_recovery_finished(lifecycle, restarted) == "initialized"
    status = _status(lifecycle)

    assert (status["owner"], status["initialized"], status["deviceReady"]) == (OWNER, True, True)
    assert status["nextActions"] == []
    assert read_device_record(lifecycle.home).owner == OWNER
    assert _records(lifecycle, "initialize").count((OWNER, "completed")) == 1
    assert _owner_records(lifecycle, OTHER_OWNER) == previous


def test_failed_initialize_receipt_is_followed_by_a_fresh_initialize_on_restart(lifecycle):
    child = lifecycle.spawn()
    lifecycle.wait_phase({"bootstrap"}, child)
    _authenticate(lifecycle, "initfail")
    _kill9(lifecycle, child)
    write_device_record(
        lifecycle.home,
        DeviceRecord("z2-node-device", OWNER, "nodekey:test-server", "nodekey:test-client"),
    )
    _seed_terminal_receipts(lifecycle, OWNER, "failed")
    failed = [r for r in _owner_records(lifecycle, OWNER) if r.kind == "initialize"]
    assert [r.state for r in failed] == ["failed"]

    restarted = _restart(lifecycle)
    assert _wait_recovery_finished(lifecycle, restarted) == "initialized"
    status = _status(lifecycle)

    assert (status["initialized"], status["deviceReady"], status["nextActions"]) == (True, True, [])
    assert sorted(_records(lifecycle, "initialize")) == [(OWNER, "completed"), (OWNER, "failed")]
    assert [r for r in _owner_records(lifecycle, OWNER) if r.state == "failed"] == failed
