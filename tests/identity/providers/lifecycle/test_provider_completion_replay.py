"""Completion retries must not recreate released helper custody."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("mode", ["baseline", "replay"])
def test_real_completion_replays_preserve_terminal_progress_and_close(tmp_path, mode):
    root = Path(__file__).resolve().parents[4]
    receipt = tmp_path / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[4] / 'tests/support/providers/completion_replay_probe.py'),
            mode,
            str(receipt),
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    records = json.loads(receipt.read_text())
    assert not any(row["kind"] == "thread-error" for row in records)
    if mode == "replay":
        deliveries = [
            row for row in records if row["kind"] == "native-completion-enqueued"
        ]
        assert sum(row["copy"] == 2 for row in deliveries) == 2
        ack = next(row for row in records if row["kind"] == "replayed-decision-ack")
        assert not ack["reused_id_present"]
        # The other third-round helper remains owned until its own receipt.
        assert ack["helper_custody"] == 1


def test_missing_runner_receipt_drains_without_acknowledging_other_helper(
    tmp_path, monkeypatch
):
    import threading

    from hyprial.kernel.impl.actor_runtime import AdmissionResult
    from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted
    from hyprial.identity.impl.provider_auth.actor._authority import ProviderAuthAuthority
    from hyprial.identity.impl.provider_auth.helper import HelperOutcome
    from tests.identity.providers.authentication.episodes.support import _authority, _fail

    entered = {name: threading.Event() for name in ("kimi-coding", "openai-codex")}
    release = {name: threading.Event() for name in entered}
    receipt_processed = threading.Event()
    receipts = []
    removed_operation = None
    receive = ProviderAuthAuthority._receive

    def observed_receive(self, event):
        receive(self, event)
        if (
            isinstance(event, EffectCompleted)
            and event.operation_id == removed_operation
        ):
            receipts.append(event)
            receipt_processed.set()

    def runner(provider, announce, *, stop):
        entered[provider].set()
        assert release[provider].wait(5)
        return HelperOutcome.OK

    monkeypatch.setattr(ProviderAuthAuthority, "_receive", observed_receive)
    actor = _authority(tmp_path, runner, helper_capacity=2, observation_capacity=2)
    try:
        assert _fail(actor) is AdmissionResult.ACCEPTED
        assert (
            actor.handle_turn_failure(
                "OAuth refresh failed for openai-codex: invalid_grant",
                harness="pi",
                provider="openai-codex",
                model="gpt",
                worker="openai-worker",
            )
            is AdmissionResult.ACCEPTED
        )
        assert all(event.wait(2) for event in entered.values())
        with actor._guard:
            operations = {
                item[1].provider: operation
                for operation, item in actor._runners.items()
            }
            removed_operation = operations["kimi-coding"]
            # Fault control: custody table entry disappears before its accepted
            # native operation has completed or its lane receipt was ACKed.
            actor._runners.pop(removed_operation)
        live_operation = operations["openai-codex"]
        assert live_operation != removed_operation
        assert actor._helpers.snapshot().outstanding == 2
        release["kimi-coding"].set()
        assert receipt_processed.wait(2)
        assert actor._helpers.snapshot().outstanding == 1
        with actor._helpers._condition:
            assert (live_operation, 1) in actor._helpers._pending
            assert (removed_operation, 1) not in actor._helpers._pending
        assert "finished:" + removed_operation not in actor._commands
        # Replaying the old receipt/ACK again must still leave live custody.
        receipt_processed.clear()
        actor._runtime.tell(actor._handle, receipts[0])
        assert receipt_processed.wait(2)
        assert actor._helpers.snapshot().outstanding == 1
        release["openai-codex"].set()
        assert actor.close(2)
        assert actor.status()["custody"] == 0
        assert all(
            not worker.is_alive()
            for lane in (actor._helpers, actor._decisions, actor._notifications)
            for worker in lane._threads
        )
    finally:
        for event in release.values():
            event.set()
        # Test cleanup for the deliberately broken baseline, not its assertion.
        if removed_operation is not None:
            actor._helpers.acknowledge(removed_operation, 1)
        assert actor.close(2)
