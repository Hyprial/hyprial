"""AT07 ③-3: the source completes on its own rules plus a claimed half.

``transfer.complete`` gains an additive P2 branch: when the caller brings the
target's report, this daemon parses it to the exact shape and hands it to the
**existing** decision together with its own source row.  These tests pin both
arms of that sentence -- a genuine pair lets the source retire, and every
near-miss (no source half, an attention row, a malformed or foreign claim, a
source that still runs or cannot be probed) still holds with the source
untouched.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.transfer.p2.actor import Command, Operation
from hyprial.daemon.impl.transfer.p2.journal import StepState
from hyprial.daemon.impl.transfer.p2.model import Admission, Role
from hyprial.daemon.impl.transfer.p2.orchestration.lift import (
    TargetHalf,
    half_to_json,
)
from tests.integration.transfer.p2.lift.test_source_quiesce_wiring import (
    _app,
    _close_application,
    _p2_params,
    _prove,
    _started,
)

OPERATION = "op-done"
OTHER_DIGEST = "b" * 64


def _report(
    source_binding,
    operation_id: str = OPERATION,
    *,
    plan_digest: str | None = None,
    role: Role = Role.TARGET,
    step: str = "activate",
    phase: str = "active",
    version: int = 1,
    generation: int | None = None,
    row_operation_id: str | None = None,
    row_actor: str | None = None,
    **binding_changes,
) -> dict[str, object]:
    """The target's own report of its half, as ``transfer.receive`` ships it.

    ``binding_changes`` are applied after the role swap, so a test can spell one
    field differently and watch the join refuse it.
    """

    binding = replace(
        source_binding,
        role=role,
        plan_digest=source_binding.plan_digest if plan_digest is None else plan_digest,
        **binding_changes,
    )
    admission = Admission(
        operation_id=operation_id, binding=binding, created_at_ms=1,
        expires_at_ms=1 << 52, revoked_at_ms=None, cancelled_at_ms=None,
    )
    row = StepState(
        operation_id=row_operation_id or operation_id,
        actor=row_actor or binding.target_actor,
        phase=phase,
        step=step,
        fingerprint=binding.plan_digest,
        # A real target counts its own attempts: the landing is the first and
        # the activation the second, so a report that says "active" at
        # generation 1 is not an attempt this chain can have had.
        generation=generation if generation is not None else (2 if step == "activate" else 1),
        entity_token="target-incarnation", receipt="activated", needs_attention=False,
    )
    payload = half_to_json(TargetHalf(operation_id=operation_id, admission=admission, row=row))
    payload["version"] = version
    return payload


def _quiesce(app, operation_id: str = OPERATION):
    """Drive the source half through the real P2 quiesce, then read its rows."""

    app.handle("transfer.quiesce", _p2_params(operation_id))
    admission = app._transfer_p2.submit(Command(Operation.GET, operation_id)).result(5)
    assert admission is not None and admission.binding.role is Role.SOURCE
    return admission


def _complete(app, report, operation_id: str = OPERATION):
    return app.handle(
        "transfer.complete",
        {
            "name": "worker",
            "provider": "codex",
            "operationId": operation_id,
            "chain": {"report": report},
        },
    )


def test_a_genuine_pair_retires_the_source(tmp_path, monkeypatch) -> None:
    """The positive arm: both halves genuine ⇒ complete, and the agent retires."""

    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()
        admission = _quiesce(app)

        payload = _complete(app, _report(admission.binding))

        assert payload["removedAgent"] is True
        assert payload["chain"]["action"] == "complete"
        assert payload["chain"]["reason"] is None
        assert payload["chain"]["sourcePhase"] == "quiesced"
        assert payload["chain"]["targetPhase"] == "active"
        assert app.agents.get("worker") is None
        assert process.running is False
    finally:
        _close_application(app)


def test_a_report_that_claims_active_before_its_landing_is_refused(
    tmp_path, monkeypatch
) -> None:
    """Condition 2(b): an active row the target cannot have reached is stale.

    A genuine target reaches ``activate`` as its second own attempt, so a report
    that carries ``active`` at generation 1 describes an attempt that is not
    this chain's.  Refusing it keeps an old "the target is active" from coming
    back and unlocking the source.
    """

    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()
        admission = _quiesce(app)

        with pytest.raises(DaemonRequestError) as refused:
            _complete(app, _report(admission.binding, generation=1))

        assert refused.value.code == "TRANSFER_CONFLICT"
        assert refused.value.data["reason"] == "stale-target-half"
        # The source is untouched: still present, still stopped.
        assert app.agents.get("worker") is not None
        assert process.running is False
    finally:
        _close_application(app)


def test_a_valid_half_cannot_complete_without_the_source_own_row(
    tmp_path, monkeypatch
) -> None:
    """Negative control one: no source half ⇒ hold, never complete."""

    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()
        admission = _quiesce(app, "op-quiesced")

        with pytest.raises(DaemonRequestError) as refused:
            _complete(app, _report(admission.binding, "op-other"), "op-other")

        assert refused.value.code == "TRANSFER_CONFLICT"
        assert refused.value.data["reason"] == "no-quiesce-intent"
        # The source is untouched: the agent is still there and still stopped.
        assert app.agents.get("worker") is not None
        assert process.running is False
    finally:
        _close_application(app)


def test_an_attention_row_holds_even_with_a_valid_target_half(
    tmp_path, monkeypatch
) -> None:
    """Negative control two: the source's own attention row wins over the claim."""

    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()
        admission = _quiesce(app)
        from hyprial.daemon.impl.transfer.p2.store import AdmissionStore

        store = AdmissionStore(app.state_db, node=app.node_id, owner=app.owner)
        row = store.journal.get(OPERATION)
        store.journal.attention(
            OPERATION, expected=row, event_id="override-1", principal="operator",
            action="manual-restore", reason="operator took the wheel",
        )

        with pytest.raises(DaemonRequestError) as refused:
            _complete(app, _report(admission.binding))

        assert refused.value.code == "TRANSFER_CONFLICT"
        assert refused.value.data["reason"] == "attention:quiesce"
        assert app.agents.get("worker") is not None
    finally:
        _close_application(app)


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [(True, "dual-active"), (None, "liveness-unknown")],
)
def test_a_source_that_still_runs_or_cannot_be_probed_holds(
    tmp_path, monkeypatch, verdict, reason
) -> None:
    """Only an observed *not running* unlocks completion.

    ``True`` is the anti-dual-active arm; ``None`` is the fail-closed arm --
    an unreadable probe holds instead of completing.
    """

    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()
        admission = _quiesce(app)
        monkeypatch.setattr(app, "_transfer_source_running", lambda actor, token: verdict)

        with pytest.raises(DaemonRequestError) as refused:
            _complete(app, _report(admission.binding))

        assert refused.value.code == "TRANSFER_CONFLICT"
        assert refused.value.data["reason"] == reason
        assert app.agents.get("worker") is not None
    finally:
        _close_application(app)


@pytest.mark.parametrize(
    "foreign",
    [
        {"plan_digest": OTHER_DIGEST},
        {"target_node": "other-server"},
        {"target_actor": "other-worker"},
        {"source_node": "other-laptop"},
        {"source_agent_id": "other-worker"},
        {"source_entity_token": "other-incarnation"},
        {"owner": "bob"},
    ],
)
def test_a_foreign_chain_is_refused(tmp_path, monkeypatch, foreign) -> None:
    """A half whose plan is not this chain's is refused on the seven-field join."""

    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()
        admission = _quiesce(app)

        with pytest.raises(DaemonRequestError) as refused:
            _complete(app, _report(admission.binding, **foreign))

        assert refused.value.code == "TRANSFER_CONFLICT"
        assert refused.value.data["reason"] == "plan-mismatch"
        assert app.agents.get("worker") is not None
    finally:
        _close_application(app)


@pytest.mark.parametrize(
    "damage",
    [
        {"version": 2},
        {"role": Role.SOURCE},
        {"step": "quiesce"},
        {"operationId": "op-elsewhere"},
    ],
)
def test_a_malformed_half_is_refused_before_anything_else(
    tmp_path, monkeypatch, damage
) -> None:
    """A damaged report is refused; it is never read as "no report"."""

    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()
        admission = _quiesce(app)
        report = _report(admission.binding)
        if "operationId" in damage:
            report["operationId"] = damage["operationId"]
        elif "step" in damage:
            report["row"]["step"] = damage["step"]
        elif "role" in damage:
            report["admission"]["binding"]["role"] = damage["role"].value
        else:
            report["version"] = damage["version"]

        with pytest.raises(DaemonRequestError) as refused:
            _complete(app, report)

        assert refused.value.code == "INVALID_ARGUMENT"
        assert app.agents.get("worker") is not None
    finally:
        _close_application(app)


def test_the_legacy_complete_path_is_untouched(tmp_path, monkeypatch) -> None:
    """Without chain facts the call is exactly the P0 retire it has always been."""

    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()
        _quiesce(app)

        payload = app.handle(
            "transfer.complete", {"name": "worker", "provider": "codex"}
        )

        assert payload["removedAgent"] is True
        assert "chain" not in payload
        assert app.agents.get("worker") is None
    finally:
        _close_application(app)


def test_the_probe_answers_only_for_the_incarnation_the_chain_quiesced(
    tmp_path, monkeypatch
) -> None:
    """A foreign incarnation is *unknown*, never "not running"."""

    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()
        admission = _quiesce(app)

        assert app._transfer_source_running("worker", "another-incarnation") is None
        assert app._transfer_source_running("nobody", "x") is None
        verdict = app._transfer_source_running(
            "worker", admission.binding.source_entity_token
        )
        assert verdict is False  # the double's stopped worker reads as not running
    finally:
        _close_application(app)
