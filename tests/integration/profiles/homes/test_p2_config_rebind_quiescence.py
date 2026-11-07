"""The rebind quiescence proof and its binding mutation must be one atomic step.

Reviewer finding B1: the service issued the quiescence check, the binding CAS
and the target retirement as separate owner calls, so durable desired state
could still be written between the proof and the mutation it authorised.
These controls inject exactly that write for the forward path and for
rollback, and refuse to let a rebind complete behind a stale proof.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError

from .config_rebind_support import (
    close_application,
    launch_spec,
    new_application,
    private_tree,
    race_desired_state_before_binding,
    receipt_phases,
    workspace,
)


def test_forward_rebind_refuses_desired_state_written_after_the_final_proof(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application
    app.handle("agent.create", {"name": "final-proof", "preferredHarness": "pi"})
    before = app.agents.require("final-proof").config
    source = private_tree(
        tmp_path / "final-proof-source", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    racing = race_desired_state_before_binding(
        app, monkeypatch, launch_spec("final-proof", workspace(tmp_path))
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle(
            "agent.config.rebind", {"name": "final-proof", "source": str(source)}
        )

    assert racing["inserted"] == 1
    assert refused.value.code == "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
    assert app.agents.require("final-proof").config == before
    phases = receipt_phases(app, "final-proof")
    assert sorted(phases.values()) == ["published"]

    # The refused transaction stays resumable: the desiring party withdraws
    # and the same request completes without a second rebind identity.
    app.desired_state.remove_harness("pi", "final-proof")
    resumed = app.handle(
        "agent.config.rebind", {"name": "final-proof", "source": str(source)}
    )
    assert resumed["state"] == "complete"
    assert receipt_phases(app, "final-proof")[str(resumed["receiptId"])] == "complete"


def test_rollback_refuses_desired_state_written_after_the_final_proof(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application
    app.handle("agent.create", {"name": "rollback-proof", "preferredHarness": "pi"})
    before = app.agents.require("rollback-proof").config
    source = private_tree(
        tmp_path / "rollback-proof-source", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    rebound = app.handle(
        "agent.config.rebind", {"name": "rollback-proof", "source": str(source)}
    )
    after = app.agents.require("rollback-proof").config
    assert after != before
    rollback = {
        "name": "rollback-proof",
        "rollbackReceiptId": rebound["receiptId"],
    }
    racing = race_desired_state_before_binding(
        app, monkeypatch, launch_spec("rollback-proof", workspace(tmp_path))
    )

    with pytest.raises(DaemonRequestError) as refused:
        app.handle("agent.config.rebind", rollback)

    assert racing["inserted"] == 1
    assert refused.value.code == "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
    assert app.agents.require("rollback-proof").config == after
    phases = receipt_phases(app, "rollback-proof")
    assert phases[str(rebound["receiptId"])] in {"rolling-back", "binding-restored"}

    app.desired_state.remove_harness("pi", "rollback-proof")
    resumed = app.handle("agent.config.rebind", rollback)
    assert resumed["state"] == "rolled-back"
    assert app.agents.require("rollback-proof").config == before
    assert not Path(str(rebound["target"])).exists()


def test_forward_refusal_after_final_proof_survives_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "forward-proof-restart"
    source = private_tree(
        tmp_path / "forward-proof-restart-source",
        {"native/pi/AGENTS.md": b"replacement\n"},
    )
    first = new_application(root)
    try:
        first.handle("agent.create", {"name": "kept", "preferredHarness": "pi"})
        before = first.agents.require("kept").config
        racing = race_desired_state_before_binding(
            first, monkeypatch, launch_spec("kept", workspace(tmp_path))
        )
        with pytest.raises(DaemonRequestError) as refused:
            first.handle("agent.config.rebind", {"name": "kept", "source": str(source)})
        assert racing["inserted"] == 1
        assert refused.value.code == "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
        assert sorted(receipt_phases(first, "kept").values()) == ["published"]
    finally:
        close_application(first)

    second = new_application(root)
    try:
        with pytest.raises(DaemonRequestError) as still_refused:
            second.handle(
                "agent.config.rebind", {"name": "kept", "source": str(source)}
            )
        assert still_refused.value.code == "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
        assert second.agents.require("kept").config == before

        # The restart restored the injected row as a durable desired harness
        # and seeded its runtime binding; withdrawing the row and releasing
        # that binding (what `down`/`release` does) restores quiescence.
        second.desired_state.remove_harness("pi", "kept")
        second._agent_session_domains.agent._liveness.release(
            second.agents.require("kept").uri
        )
        resumed = second.handle(
            "agent.config.rebind", {"name": "kept", "source": str(source)}
        )
        assert resumed["state"] == "complete"
    finally:
        close_application(second)


def test_rollback_refusal_after_final_proof_survives_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "rollback-proof-restart"
    source = private_tree(
        tmp_path / "rollback-proof-restart-source",
        {"native/pi/AGENTS.md": b"replacement\n"},
    )
    first = new_application(root)
    try:
        first.handle("agent.create", {"name": "restored", "preferredHarness": "pi"})
        before = first.agents.require("restored").config
        rebound = first.handle(
            "agent.config.rebind", {"name": "restored", "source": str(source)}
        )
        racing = race_desired_state_before_binding(
            first, monkeypatch, launch_spec("restored", workspace(tmp_path))
        )
        with pytest.raises(DaemonRequestError) as refused:
            first.handle(
                "agent.config.rebind",
                {"name": "restored", "rollbackReceiptId": rebound["receiptId"]},
            )
        assert racing["inserted"] == 1
        assert refused.value.code == "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
        after = first.agents.require("restored").config
        assert after != before
    finally:
        close_application(first)

    second = new_application(root)
    try:
        rollback = {"name": "restored", "rollbackReceiptId": rebound["receiptId"]}
        with pytest.raises(DaemonRequestError) as still_refused:
            second.handle("agent.config.rebind", rollback)
        assert still_refused.value.code == "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
        assert second.agents.require("restored").config == after

        second.desired_state.remove_harness("pi", "restored")
        second._agent_session_domains.agent._liveness.release(
            second.agents.require("restored").uri
        )
        resumed = second.handle("agent.config.rebind", rollback)
        assert resumed["state"] == "rolled-back"
        assert second.agents.require("restored").config == before
    finally:
        close_application(second)
