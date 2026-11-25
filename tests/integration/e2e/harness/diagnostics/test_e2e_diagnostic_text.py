"""RESULT lines keep the END of a failure, where the error is, and redact first."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
SCENARIOS = ROOT / "contract" / "e2e"
sys.path.insert(0, str(SCENARIOS))

from _diagnostic_text import HEAD_CHARS, TAIL_CHARS, clip, one_line  # noqa: E402


def _load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCENARIOS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _uv_failure() -> str:
    # Shape of E2E-010 (baseline task 40654): long progress, error last.
    progress = "".join(f"Downloading package-{i} (1.0MiB)\n" for i in range(400))
    return progress + "error: Failed to install: eclipse-zenoh-1.0.0 (the real reason)\n"


def test_the_last_line_of_a_long_stderr_survives() -> None:
    rendered = clip(one_line(_uv_failure()))
    assert rendered.endswith("error: Failed to install: eclipse-zenoh-1.0.0 (the real reason)")
    assert rendered.startswith("Downloading package-0 ")
    assert "chars elided]" in rendered
    assert len(rendered) < HEAD_CHARS + TAIL_CHARS + 64


def test_short_detail_is_unchanged_and_one_line() -> None:
    assert clip(one_line("a\n b\x00\tc")) == "a b c"
    exact = "x" * (HEAD_CHARS + TAIL_CHARS)
    assert clip(exact) == exact


def test_reporter_keeps_the_tail_of_a_failure(capsys) -> None:
    runner = _load("run-upgrade-continuity.py", "e2e010_under_test")
    report = runner.Report()
    report.add("FAIL", "upgrade-continuity", _uv_failure())
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("RESULT FAIL E2E-010 upgrade-continuity ")
    assert line.endswith("(the real reason)")
    assert report.results[-1].detail.endswith("(the real reason)")


def test_redaction_runs_before_clipping_so_no_fragment_survives(capsys) -> None:
    runner = _load("run-lark-app-onboarding.py", "e2e011_under_test")
    secret = runner.STUB_APP_SECRET
    # Put the secret exactly across the head cut: an unredacted clip would
    # keep the first part of it.
    padding = "p" * (HEAD_CHARS - len(secret) // 2)
    detail = padding + secret + ("q" * (TAIL_CHARS + 500))
    report = runner.Report()
    report.add("FAIL", "onboarding", detail)
    out = capsys.readouterr().out
    fragment = secret[: len(secret) // 2]
    assert fragment not in out
    assert fragment not in report.results[-1].detail


# -- run-all-harness-lifecycle lark-cli failures: redact BEFORE any cut ------

LIFECYCLE_ID = "oc_a1b2c3d4e5f60718293a4b5c"


def _straddling_text(
    native_id: str,
    offset: int,
    cut: int = 600,
    before: str = "",
    after: str = "",
) -> str:
    """Build text so a tail-only ``[-cut:]`` boundary falls `offset` chars into the id."""

    tail_len = cut - len(native_id) + offset - len(after)
    assert tail_len >= 0
    return before + ("." * 8) + native_id + ("." * tail_len) + after


def _assert_no_raw_fragment(message: str, native_id: str) -> None:
    # No substring of the raw id's random part of length >= 6 may survive.
    random_part = native_id[3:]
    for i in range(len(random_part) - 5):
        assert random_part[i : i + 6] not in message, message


def _lifecycle_runner():
    return _load("run-all-harness-lifecycle.py", "e2e013_under_test")


def _fake_run(runner, failing):
    def run(cmd, **_kwargs):
        if list(cmd[1:3]) == ["profile", "list"]:
            profiles = json.dumps([{"appId": runner.LARK_APP_ID, "name": "e2e"}])
            return subprocess.CompletedProcess(cmd, 0, stdout=profiles, stderr="")
        return failing(cmd)

    return run


@pytest.mark.parametrize("offset", range(len(LIFECYCLE_ID)))
def test_profile_list_failure_redacts_before_cut(offset, monkeypatch) -> None:
    runner = _lifecycle_runner()
    stderr = _straddling_text(LIFECYCLE_ID, offset)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr),
    )
    with pytest.raises(AssertionError) as excinfo:
        runner.e2e_lark_profile()
    _assert_no_raw_fragment(str(excinfo.value), LIFECYCLE_ID)


@pytest.mark.parametrize("offset", range(len(LIFECYCLE_ID)))
def test_lark_cli_nonzero_exit_redacts_before_cut(offset, monkeypatch) -> None:
    runner = _lifecycle_runner()
    stderr = _straddling_text(LIFECYCLE_ID, offset)

    def failing(cmd):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(runner.subprocess, "run", _fake_run(runner, failing))
    with pytest.raises(AssertionError) as excinfo:
        runner.lark_cli("auth", "status")
    _assert_no_raw_fragment(str(excinfo.value), LIFECYCLE_ID)


@pytest.mark.parametrize("offset", range(len(LIFECYCLE_ID)))
def test_lark_cli_ok_false_redacts_before_cut(offset, monkeypatch) -> None:
    runner = _lifecycle_runner()
    payload = _straddling_text(
        LIFECYCLE_ID,
        offset,
        before='{"ok": false, "error": "',
        after='"}',
    )

    def failing(cmd):
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", _fake_run(runner, failing))
    with pytest.raises(AssertionError) as excinfo:
        runner.lark_cli("auth", "status")
    _assert_no_raw_fragment(str(excinfo.value), LIFECYCLE_ID)


def test_pac_graph_slice_report_keeps_the_error_at_the_end(capsys) -> None:
    runner = _load("run-pac-graph-slice.py", "e2e018_under_test")
    detail = ("progress " * 1200) + "ERROR the real reason: journal conflict"
    report = runner.Report()
    report.add("FAIL", "graph-slice", detail)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("RESULT FAIL E2E-018 graph-slice ")
    assert line.endswith("ERROR the real reason: journal conflict")
    assert "chars elided]" in line
