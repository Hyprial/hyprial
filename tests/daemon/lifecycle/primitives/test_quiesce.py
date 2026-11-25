# Decision record (file header; no inline comments by repository rule):
# - Agent teleport slice 3 (docs/design/agent-teleport-acceptance.md): quiesce
#   becomes a unit callable without a ``transfer.*`` params dict.  Every
#   collaborator is a plain callable so teleport can drive it from its own
#   service and ``transfer.quiesce`` can keep driving it from the daemon app.
# - The order is the one ``_transfer_quiesce`` already owns: resolve once to
#   refuse an unknown worker before any node-wide write-back, force-sync the
#   session refs, resolve again so the snapshot carries the freshest ref, stop
#   that spec, then read the quiescence proof.
# - The proof is produced by an injected port and returned untouched.  A proof
#   that says "not quiescent" is reported as-is, never rounded up; the caller
#   decides what to refuse (same rule as the P2 effect's ``unknown``).
"""Direct tests for the extracted quiesce primitive."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec, ipc_errors


STALE = HarnessLaunchSpec(
    harness="pi", name="wk", headless=True, cwd="/work", session_ref="session-1"
)
FRESH = replace(STALE, session_ref="session-2")


class _Source:
    """Records every port call in order; resolves stale first, fresh after a sync."""

    def __init__(self, proof: dict[str, object] | None = None) -> None:
        self.events: list[str] = []
        self.synced = False
        self.proof = proof if proof is not None else {"quiescent": True, "runtime": False}
        self.deactivate_error: Exception | None = None
        self.resolve_error: Exception | None = None

    def resolve_spec(self) -> HarnessLaunchSpec:
        self.events.append("resolve")
        if self.resolve_error is not None:
            raise self.resolve_error
        return FRESH if self.synced else STALE

    def sync_session_refs(self) -> object:
        self.events.append("sync")
        self.synced = True
        return {("pi", "wk"): "session-2"}

    def deactivate(self, spec: HarnessLaunchSpec) -> object:
        self.events.append(f"deactivate:{spec.session_ref}")
        if self.deactivate_error is not None:
            raise self.deactivate_error
        return object()

    def prove_quiescence(self, spec: HarnessLaunchSpec) -> dict[str, object]:
        self.events.append(f"prove:{spec.session_ref}")
        return self.proof


def _quiesce(source: _Source):
    from hyprial.daemon.impl.lifecycle.primitives import quiesce_agent

    return quiesce_agent(
        resolve_spec=source.resolve_spec,
        sync_session_refs=source.sync_session_refs,
        deactivate=source.deactivate,
        prove_quiescence=source.prove_quiescence,
    )


def test_quiesce_syncs_refs_then_stops_the_fresh_spec_and_returns_the_proof() -> None:
    source = _Source()

    result = _quiesce(source)

    assert source.events == [
        "resolve",
        "sync",
        "resolve",
        "deactivate:session-2",
        "prove:session-2",
    ]
    assert result.spec == FRESH
    assert result.proof is source.proof
    assert result.quiescent is True


def test_quiesce_result_type_is_exported_with_the_unit() -> None:
    from hyprial.daemon.impl.lifecycle.primitives import Quiescence

    result = _quiesce(_Source())

    assert isinstance(result, Quiescence)


def test_quiesce_reports_a_non_quiescent_proof_as_is() -> None:
    proof: dict[str, object] = {"quiescent": False, "runtime": True}
    source = _Source(proof)

    result = _quiesce(source)

    assert result.quiescent is False
    assert result.proof is proof
    assert "deactivate:session-2" in source.events


def test_quiesce_treats_a_proof_without_a_true_verdict_as_not_quiescent() -> None:
    result = _quiesce(_Source({"quiescent": "yes"}))

    assert result.quiescent is False


def test_quiesce_of_an_unknown_worker_touches_nothing() -> None:
    source = _Source()
    source.resolve_error = DaemonRequestError(
        ipc_errors.TRANSFER_WORKER_NOT_FOUND, "no worker named 'wk'"
    )

    with pytest.raises(DaemonRequestError) as refusal:
        _quiesce(source)

    assert refusal.value.code == ipc_errors.TRANSFER_WORKER_NOT_FOUND
    assert source.events == ["resolve"]


def test_quiesce_does_not_prove_after_a_failed_stop() -> None:
    source = _Source()
    source.deactivate_error = RuntimeError("lifecycle refused")

    with pytest.raises(RuntimeError, match="lifecycle refused"):
        _quiesce(source)

    assert source.events == ["resolve", "sync", "resolve", "deactivate:session-2"]
