"""E2E-001 keeps a TTL verdict terminal when its recipient node is absent."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
RUNNER = ROOT / "contract" / "e2e" / "run-two-machine-roundtrip.py"
DOC = ROOT / "contract" / "e2e" / "01-two-machine-roundtrip.md"
PHASES = ROOT / "contract" / "e2e" / "_two_machine" / "phases.py"
PORTS = ROOT / "contract" / "e2e" / "_two_machine" / "ports.py"
STATUS_QUERY = (
    ROOT
    / "src"
    / "hyprial"
    / "daemon"
    / "impl"
    / "application"
    / "messaging"
    / "delivery"
    / "status_queries.py"
)


def _scenario_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (RUNNER, PHASES, PORTS, STATUS_QUERY)
    )


def _assert_runner_wiring(source: str) -> None:
    assert 'ANSWERED_GHOST_ACTOR = f"agent:e2e:{NODES[1]}:ghost"' in source
    assert '"phase6",\n        ports.NODE_C,\n        ports.DRIVER,\n        ports.GHOST_ACTOR,' in source
    assert source.count('expected_reason="TTL_EXPIRED"') >= 4
    assert 'assert_no_known_loss("phase6e", local_expiry)' in source
    assert 'assert_no_known_loss("phase6e", remote_expiry)' in source
    assert "def wait_for_unconfirmed(" not in source
    assert "assert_unconfirmed_record" not in source
    assert "observedState" not in source
    assert "observedReason" not in source
    assert 'projected["state"] = "unconfirmed"' not in source
    assert "missingRecipientHolders" in source
    assert "RESULT ok phase6e absentRecipientExpired=true" in source
    assert '"absentRecipientExpired=true "' in source


def _assert_doc_wiring(doc: str) -> None:
    assert "absent recipient is `expired`" in doc
    assert "`reason=TTL_EXPIRED`" in doc
    assert "`missingRecipientHolders`" in doc
    assert "observedState" not in doc
    assert "observedReason" not in doc


def test_runner_wiring_pins_the_6d_6e_contract() -> None:
    _assert_runner_wiring(_scenario_source())


def test_doc_wiring_pins_the_6d_6e_contract() -> None:
    _assert_doc_wiring(DOC.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "old,new",
    [
        ('expected_reason="TTL_EXPIRED"', 'expected_reason="RECIPIENT_HOLDER_UNANSWERED"'),
        (
            'assert_no_known_loss("phase6e", local_expiry)',
            'assert_no_known_loss("phase6e", local_unconfirmed)',
        ),
        ("absentRecipientExpired=true", "absentRecipientUnconfirmed=true"),
    ],
)
def test_runner_contract_mutations_are_rejected(old: str, new: str) -> None:
    source = _scenario_source()
    mutated = source.replace(old, new, 1)
    assert mutated != source
    with pytest.raises(AssertionError):
        _assert_runner_wiring(mutated)


def test_projection_mutation_is_rejected() -> None:
    source = _scenario_source()
    anchor = "projected = record.to_json()"
    mutated = source.replace(
        anchor,
        f'{anchor}\n    projected["state"] = "unconfirmed"',
        1,
    )
    assert mutated != source
    with pytest.raises(AssertionError):
        _assert_runner_wiring(mutated)


def test_doc_semantics_mutation_is_rejected() -> None:
    doc = DOC.read_text(encoding="utf-8")
    mutated = doc.replace("absent recipient is `expired`", "absent recipient is `unknown`")
    assert mutated != doc
    with pytest.raises(AssertionError):
        _assert_doc_wiring(mutated)


def test_docker_success_report_names_the_verified_expiry(monkeypatch, capsys):
    from tests.integration.e2e.e2e001.test_e2e001_container_owner import load_runner

    runner = load_runner()
    monkeypatch.setattr(runner.sys, "argv", ["run-two-machine-roundtrip.py"])
    monkeypatch.setattr(runner, "_install_signal_handlers", lambda: {})
    monkeypatch.setattr(runner, "run_docker", lambda **_kwargs: None)
    monkeypatch.setattr(runner, "_ci_capture_enabled", lambda: False)
    monkeypatch.setattr(runner, "remove_resources", lambda: [])
    monkeypatch.setattr(runner, "self_check", lambda: [])
    assert runner.main() == 0
    report = capsys.readouterr().out
    assert "mode=docker" in report and "phases=7" in report
    assert "absentRecipientExpired=true" in report
    assert "absentRecipientUnconfirmed" not in report
