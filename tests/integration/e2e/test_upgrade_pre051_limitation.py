import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("mutation", [None, "wrong-error", "success", "no-start", "extra-check"])
@pytest.mark.parametrize("case", ["pre051", "cross-minor"])
def test_known_limitation_accepts_only_the_historical_failure(mutation, case):
    runner = runpy.run_path(str(Path(__file__).resolve().parents[3] / f"contract/e2e/run-upgrade-{case}-limitation.py"))
    output = (
        "RESULT PASS E2E-010 old-channel-running\n"
        "RESULT FAIL E2E-010 upgrade-continuity Channel probe exited during upgrade-auto-restart "
        "ModuleNotFoundError: No module named 'rich.table'\n"
        "RESULT FAIL E2E-010 upgrade-continuity-known-limitation-pre051 pass=5 fail=1 blocked=0\n"
    )
    if case == "cross-minor":
        output = output.replace("rich.table", "hyprial.identity.impl.identity_transaction")
        output = output.replace("known-limitation-pre051", "known-limitation-cross-minor")
        output = output.replace("Channel probe exited during upgrade-auto-restart", "hyprial upgrade --json exited 1")
    code = 1
    if mutation == "wrong-error":
        output = output.replace("rich.table", "unrelated").replace("hyprial.identity.impl.identity_transaction", "unrelated")
    elif mutation == "success":
        code = 0
    elif mutation == "no-start":
        output = output.replace("RESULT PASS E2E-010 old-channel-running", "")
    elif mutation == "extra-check":
        output = output.replace("pass=5", "pass=6")
    assert runner["expected_failure"](code, output) is (mutation in (None, "extra-check"))
