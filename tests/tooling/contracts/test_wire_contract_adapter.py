from __future__ import annotations

import copy
import json
from pathlib import Path

from hyprial.daemon.impl.ipc.wire import evaluate_case

ROOT = Path(__file__).resolve().parents[3] / 'contract/wire/golden'


def _corpus() -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    cases = json.loads((ROOT / "expectations.json").read_text(encoding="utf-8"))[
        "cases"
    ]
    envelopes = json.loads((ROOT / "envelopes.json").read_text(encoding="utf-8"))[
        "envelopes"
    ]
    return cases, {item["fixture_id"]: item for item in envelopes}


def test_every_wire_golden_case_runs_through_python_stack() -> None:
    cases, envelopes = _corpus()
    for case in cases:
        assert evaluate_case(case, envelopes) == case["expect"], case["id"]


def test_adapter_does_not_echo_declared_expectation() -> None:
    cases, envelopes = _corpus()
    poisoned = copy.deepcopy(cases[0])
    poisoned["expect"] = {"poisoned": True}

    assert evaluate_case(poisoned, envelopes) != poisoned["expect"]


def test_adapter_observes_envelope_and_environment_mutations() -> None:
    cases, envelopes = _corpus()
    by_id = {case["id"]: case for case in cases}

    changed_envelopes = copy.deepcopy(envelopes)
    changed_envelopes["request-basic"]["intent"] = "event"
    changed = evaluate_case(by_id["WG-001"], changed_envelopes)
    assert changed["wake"] is False
    assert changed["replyOwed"] is False

    changed_case = copy.deepcopy(by_id["WG-017"])
    changed_case["context"]["registryMatches"] = ["agent:bob:pc:missing"]
    resolved = evaluate_case(changed_case, envelopes)
    assert resolved["accepted"] is True
    assert resolved["code"] is None

    changed_pin = copy.deepcopy(by_id["WG-022"])
    changed_pin["context"]["pinnedActor"] = "agent:alice:mac:alternate"
    assert (
        evaluate_case(changed_pin, envelopes)["target"] == "agent:alice:mac:alternate"
    )
