# Decision record (file header; no inline comments by repository rule):
# - Meta cases for support.verdict (HQ review: report PASS / FAIL / BLOCKED,
#   never a skip passed off as a PASS). The pure functions are checked on
#   fake report objects; one subprocess run per verdict drives the real
#   VerdictPlugin through pytest on tiny synthetic test files in tmp_path
#   (outside cases/, so the coverage scan never sees them), checking the
#   exit status, the last summary line and the --verdict-json payload.
# - The synthetic runs use their own pytest.ini and conftest.py (which only
#   registers the plugin), so nothing of the engine suite is loaded there.
# - These cases test the suite, not the engine: no case marker.
"""Suite integrity: session verdict and exit status."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from support.verdict import BLOCKED_PREFIX, EXIT_BLOCKED, EXIT_FAIL, EXIT_PASS, decide, outcome_of

SUITE = Path(__file__).resolve().parents[2]


def report(when: str, outcome: str, longrepr: object = None, **extra: object) -> SimpleNamespace:
    return SimpleNamespace(when=when, outcome=outcome, longrepr=longrepr, **extra)


def test_outcome_of_maps_every_phase() -> None:
    assert outcome_of(report("setup", "passed")) is None
    assert outcome_of(report("call", "passed")) == ("passed", "")
    assert outcome_of(report("call", "failed", "E  assert 1 == 2"))[0] == "failed"
    assert outcome_of(report("setup", "failed", "boom"))[0] == "error"
    assert outcome_of(report("teardown", "failed", "boom"))[0] == "error"
    assert outcome_of(report("call", "skipped", ("f.py", 1, "Skipped: BLOCKED: no FT python"))) == (
        "blocked", "BLOCKED: no FT python")
    assert outcome_of(report("setup", "skipped", ("f.py", 1, "Skipped: not today"))) == ("skipped", "not today")
    assert outcome_of(report("call", "skipped", ("f.py", 1, "x"), wasxfail="D9"))[0] == "xfailed"
    assert outcome_of(report("call", "passed", wasxfail="D9"))[0] == "xpassed"
    assert outcome_of(report("call", "failed", "[XPASS(strict)] D9"))[0] == "xpassed"


def test_decide_precedence_and_exit_status() -> None:
    passed = {"a": ("passed", ""), "x": ("xfailed", "D9")}
    green = decide(passed)
    assert (green.verdict, green.exit_status) == ("PASS", EXIT_PASS)
    blocked = decide({**passed, "f": ("blocked", f"{BLOCKED_PREFIX} no FT")})
    assert (blocked.verdict, blocked.exit_status, blocked.blocked) == ("BLOCKED", EXIT_BLOCKED, ["f"])
    for bad in ("failed", "error", "xpassed", "skipped"):
        verdict = decide({**passed, "f": ("blocked", "BLOCKED: x"), "b": (bad, "")},
                         pytest_exit_status=1 if bad != "skipped" else 0)
        assert (verdict.verdict, verdict.exit_status) == ("FAIL", EXIT_FAIL), bad
    assert decide(passed, collection_errors=1, pytest_exit_status=2).exit_status == 2
    assert decide(passed, pytest_exit_status=2).verdict == "FAIL"
    assert decide({}).verdict == "FAIL"
    assert EXIT_BLOCKED not in (EXIT_PASS, EXIT_FAIL, 2, 3, 4, 5)


SYNTHETIC = {
    "pass": ("def test_ok():\n    assert True\n", EXIT_PASS, "PASS"),
    "blocked": ("import pytest\n\ndef test_ok():\n    pass\n\ndef test_ft():\n"
                f"    pytest.skip('{BLOCKED_PREFIX} no free-threaded interpreter')\n", EXIT_BLOCKED, "BLOCKED"),
    "plain-skip": ("import pytest\n\ndef test_ok():\n    pass\n\ndef test_skip():\n    pytest.skip('later')\n",
                   EXIT_FAIL, "FAIL"),
    "fail-and-blocked": ("import pytest\n\ndef test_bad():\n    assert False\n\ndef test_ft():\n"
                         f"    pytest.skip('{BLOCKED_PREFIX} x')\n", EXIT_FAIL, "FAIL"),
    "strict-xfail": ("import pytest\n\n@pytest.mark.xfail(strict=True, reason='D9')\ndef test_known():\n"
                     "    assert False\n\ndef test_ok():\n    pass\n", EXIT_PASS, "PASS"),
    "strict-xpass": ("import pytest\n\n@pytest.mark.xfail(strict=True, reason='D9')\ndef test_known():\n"
                     "    pass\n", EXIT_FAIL, "FAIL"),
}


@pytest.mark.parametrize("name", sorted(SYNTHETIC))
def test_verdict_plugin_end_to_end(tmp_path: Path, name: str) -> None:
    body, status, verdict = SYNTHETIC[name]
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "from support.verdict import VerdictPlugin\n\n\n"
        "def pytest_addoption(parser):\n"
        "    parser.addoption('--verdict-json', default='')\n"
        "    parser.addoption('--actor-backend', default='synthetic')\n\n\n"
        "def pytest_configure(config):\n"
        "    config.pluginmanager.register(VerdictPlugin(config), 'conformance-verdict')\n",
        encoding="utf-8")
    (tmp_path / "test_synthetic.py").write_text(body, encoding="utf-8")
    out = tmp_path / "verdict.json"
    env = {**os.environ, "PYTHONPATH": str(SUITE)}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:logging", f"--verdict-json={out}",
                           str(tmp_path)], cwd=str(tmp_path), env=env, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == status, done.stdout + done.stderr
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    summary = [line for line in lines if line.startswith("CONFORMANCE VERDICT:")]
    assert summary == [f"CONFORMANCE VERDICT: {verdict} backend=synthetic "
                       f"python={sys.version.split()[0]} exit={status}"], done.stdout
    assert lines.index(summary[0]) >= len(lines) - 2, done.stdout
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert (payload["verdict"], payload["exit_status"], payload["backend"]) == (verdict, status, "synthetic")
    if name == "blocked":
        assert payload["blocked"] == ["test_synthetic.py::test_ft"]
        assert payload["passed"] == 1
    if name == "strict-xfail":
        assert (payload["xfailed"], payload["passed"]) == (1, 1)
