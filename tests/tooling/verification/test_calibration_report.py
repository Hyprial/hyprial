"""Calibration must be able to say the fake has drifted.

The E2E line's correctness rests on the fake resembling the service it
imitates.  Nothing in a mock announces that it has fallen behind, so the only
thing standing between "our tests are green" and "our tests are green and
meaningless" is a comparison that can fail.

A calibration that cannot report drift is not a calibration -- which is the
same standard applied to the compaction that ran eleven times, succeeded
every time, and left the context where it found it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _calibration():
    spec = importlib.util.spec_from_file_location(
        "_probe_calibration", ROOT / "contract" / "e2e" / "_calibration.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_changed_contract_version_is_reported_before_payloads() -> None:
    """The version explains any number of field differences at once."""

    calibration = _calibration()
    drifts = calibration.compare_contract_versions(
        {"openai.chat.completions": "2024-10-01"},
        {"openai.chat.completions": "2025-03-01"},
    )
    assert [d.kind for d in drifts] == ["contract-version-changed"]
    assert drifts[0].real == "2025-03-01"
    assert drifts[0].mock == "2024-10-01"


def test_an_unknown_live_version_is_its_own_finding() -> None:
    """"Cannot tell" must not be rendered as "matches".

    A contract the live probe could not identify is not a contract that
    agrees; collapsing the two would make the report say the reassuring thing
    whenever the check itself failed.
    """

    calibration = _calibration()
    drifts = calibration.compare_contract_versions(
        {"anthropic.messages": "2023-06-01"}, {}
    )
    assert [d.kind for d in drifts] == ["contract-version-unknown"]


def test_a_key_the_vendor_added_is_reported_as_missing_from_the_mock() -> None:
    """The drift that matters: the harness will see a field the fake omits."""

    calibration = _calibration()
    drifts = calibration.compare_payloads(
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
        {"choices": [{"delta": {"content": "hi"}}]},
    )
    assert [(d.path, d.kind) for d in drifts] == [
        ("choices[].finish_reason", "missing-in-mock")
    ]


def test_volatile_fields_do_not_produce_drift() -> None:
    """Ids and token counts differ every call.

    Comparing them for equality would make the report never empty, and a
    report that is never empty is one nobody reads -- the drift that matters
    would arrive in a crowd.
    """

    calibration = _calibration()
    drifts = calibration.compare_payloads(
        {"id": "chatcmpl-real", "usage": {"total_tokens": 91}, "object": "x"},
        {"id": "chatcmpl-fake", "usage": {"total_tokens": 2}, "object": "x"},
    )
    assert drifts == []


def test_a_volatile_field_changing_type_is_still_drift() -> None:
    """Presence and type are contract; the value is not.

    A vendor returning usage as a string instead of an object changes how a
    harness parses it, however volatile the number inside was.
    """

    calibration = _calibration()
    drifts = calibration.compare_payloads(
        {"usage": {"total_tokens": 91}}, {"usage": "91"}
    )
    assert [(d.path, d.kind) for d in drifts] == [("usage", "type")]


def test_identical_shapes_report_no_drift() -> None:
    """The report must be able to come back empty, or it is noise."""

    calibration = _calibration()
    payload = {"choices": [{"delta": {"content": "text"}}], "object": "chunk"}
    assert calibration.compare_payloads(payload, dict(payload)) == []


def test_the_report_is_a_difference_not_a_verdict() -> None:
    """Rendering states what differs and leaves the judgement to a reader.

    A boolean would have to decide in advance which drifts matter, and would
    be wrong for exactly the ones worth knowing about.
    """

    calibration = _calibration()
    version = calibration.compare_contract_versions(
        {"openai.chat.completions": "2024-10-01"},
        {"openai.chat.completions": "2025-03-01"},
    )
    payload = calibration.compare_payloads({"a": {"b": 1}}, {"a": {}})
    report = calibration.render_report(version, payload)

    assert "contract versions:" in report
    assert "payload shape:" in report
    assert "a.b" in report
    # No verdict vocabulary: the reader decides.
    assert "PASS" not in report and "FAIL" not in report


def test_no_drift_says_so_explicitly() -> None:
    """Silence and agreement must not look the same."""

    calibration = _calibration()
    assert "no drift" in calibration.render_report([], [])
