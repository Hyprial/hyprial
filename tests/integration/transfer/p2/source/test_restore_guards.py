"""Composition guards cannot be weakened by degraded activity policy."""

import pytest
from dataclasses import replace

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.harnesses.runtime.transfer_ports import StartsHeld
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessRestoreProjection
from hyprial.daemon.impl.transfer.p2.model import Binding, Role
from hyprial.kernel import HarnessLaunchSpec
from tests.integration.actor_preservation.lifecycle.test_actor_preserved_restore_threshold_states import (
    HOUR_MS, _app, _close,
)


def reserve(app):
    agent = app.agents.get("worker")
    store = app._transfer_p2._store
    store.prepare("move", Binding(Role.SOURCE, "alice", "node", "worker",
                                  agent.entity_token, "other-node", "target", "a" * 64))
    start = store.journal.begin("move", step="quiesce", fingerprint="fp", actor="worker")
    store.journal.finish("move", expected=start.state, receipt="arranged-receipt",
                         entity_token=agent.entity_token)


def test_degraded_policy_cannot_clear_transfer_suppression(tmp_path, monkeypatch):
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    restored = []
    try:
        reserve(app)
        app._restore_policy_degraded = "injected policy failure"
        monkeypatch.setattr(app, "_restore_policy_or_degraded", lambda: None)
        app._harnesses.restore = lambda specs: (
            restored.extend(specs) or HarnessRestoreProjection(len(specs), len(specs), 0, 0)
        )
        app._restore_harnesses()
        app._retry_restore_eligibility()
        assert not restored
        assert app._harnesses.eligibility[-1].suppressed
        # Even an explicit activity-policy unsuppress cannot erase custody.
        app._publish_restore_eligibility(spec=spec,
                                        entity_token=app.agents.get("worker").entity_token,
                                        suppressed=False)
        assert app._harnesses.eligibility[-1].suppressed
    finally:
        _close(app)


def test_query_failure_defers_only_affected_entity(tmp_path, monkeypatch):
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    try:
        app.agents.create("healthy")
        healthy = HarnessLaunchSpec("codex", "healthy", True)
        app.desired_state.upsert_harness(healthy)
        original = app._transfer_p2._store.journal.custody_snapshot

        def read(actor, token):
            if actor == "worker":
                raise OSError("injected read failure")
            return original(actor, token)

        monkeypatch.setattr(app._transfer_p2._store.journal, "custody_snapshot", read)
        assert app._filter_transfer_restore_specs((spec, healthy)) == (healthy,)
        assert app._harnesses.eligibility[-1].actor == "worker"
        assert app._harnesses.eligibility[-1].suppressed
    finally:
        _close(app)


def test_dormant_wake_does_not_start_a_held_incarnation(tmp_path):
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=17 * HOUR_MS)
    try:
        assert app._classify_restore_specs((spec,)) == ()
        reserve(app)
        assert not app._wake_dormant_agent("worker", reason="test")
        assert not app._harnesses.started
        assert app._harnesses.eligibility[-1].suppressed
    finally:
        _close(app)


@pytest.mark.parametrize("custody", ["held", "unavailable"])
def test_explicit_start_cannot_clear_custody_or_activity_suppression(
    tmp_path, monkeypatch, custody,
):
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=17 * HOUR_MS)
    try:
        assert app._classify_restore_specs((spec,)) == ()
        previous = app.agents.projection("worker").restore_disposition
        assert previous is not None
        if custody == "held":
            reserve(app)
            def acknowledge_hold(command, expected, *, timeout):
                assert expected is StartsHeld and timeout > 0
                return StartsHeld(command.correlation_id, command.actor,
                                  command.expected_entity_token, command.operation_id,
                                  command.generation, "held", version=1)
            monkeypatch.setattr(app._harnesses, "call", acknowledge_hold, raising=False)
        else:
            def unavailable(*_args):
                raise OSError("injected custody read failure")
            monkeypatch.setattr(app._transfer_p2._store.journal, "custody_snapshot", unavailable)
        starts = []
        def unexpected_start(operation):
            starts.append(operation)
            raise AssertionError("custody must reject before lifecycle submission")
        monkeypatch.setattr(app, "_run_lifecycle_operation", unexpected_start)
        with pytest.raises(DaemonRequestError) as rejected:
            app._ipc_lifecycle_start("lifecycle.start", spec.to_json())
        assert rejected.value.code == (
            "TRANSFER_STARTS_HELD" if custody == "held" else "TRANSFER_CUSTODY_UNAVAILABLE"
        )
        assert not starts
        assert app.agents.projection("worker").restore_disposition == previous
    finally:
        _close(app)


@pytest.mark.parametrize("change", [
    {"correlation_id": "other"}, {"actor": "other"},
    {"expected_entity_token": "other"}, {"operation_id": "other"},
    {"generation": 2}, {"disposition": "unavailable"},
])
def test_hold_reconstruction_requires_exact_owner_ack(tmp_path, monkeypatch, change):
    app, _ = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    try:
        reserve(app)
        def wrong_ack(command, expected, *, timeout):
            assert expected is StartsHeld and timeout > 0
            return replace(StartsHeld(command.correlation_id, command.actor,
                                     command.expected_entity_token, command.operation_id,
                                     command.generation, "held", version=1), **change)
        monkeypatch.setattr(app._harnesses, "call", wrong_ack, raising=False)
        token = app.agents.require("worker").entity_token
        assert app._hydrate_transfer_source("worker", token) is None
        # A bad volatile ack cannot erase or rewrite durable custody.
        assert app._transfer_p2._store.journal.get("move").phase == "quiesced"
        assert app._transfer_source_state("worker", token).state == "held"
    finally:
        _close(app)
