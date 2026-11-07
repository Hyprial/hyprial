"""E2E-020 C and complete FIRST_WAVE B1/B2-ABSENT/C via CLI-only processes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from contract.e2e._daemon_runner import hyprial_executable


def test_public_cli_consumer_snapshot_suffix_and_follow(tmp_path):
    root = Path(__file__).resolve().parents[4]
    executable = hyprial_executable()
    assert executable.is_file(), "run tests in the installed project environment"
    log = tmp_path / "e2e020c.log"
    env = {**os.environ, "HYPRIAL_BIN": str(executable)}
    with log.open("w") as output:
        result = subprocess.run(
            [sys.executable, str(root / "contract/e2e/run-pac-public-events.py")],
            env=env, stdout=output, stderr=subprocess.STDOUT, timeout=240,
        )
    evidence = log.read_text()
    assert result.returncode == 0, evidence
    assert "RESULT PASS E2E-020-C public contract only; A/B/D NOT RUN" in evidence
    assert "inherited SIGINT=SIG_IGN still exits cleanly" in evidence
    assert "idle follow sigterm" in evidence and "idle follow output-eof" in evidence
    assert "real migration cursorFloor=2; expired after=1 resync" in evidence
    assert "old v5 real receipts preserve snapshot v6" in evidence
    assert "RESULT FAIL" not in evidence


def test_first_wave_b1_b2_absent_c_finish_in_one_runner(tmp_path):
    root = Path(__file__).resolve().parents[4]
    executable = hyprial_executable()
    log = tmp_path / "e2e020-first-wave.log"
    with log.open("w") as output:
        result = subprocess.run(
            [sys.executable, str(root / "contract/e2e/run-pac-slice-2.py")],
            env={**os.environ, "HYPRIAL_BIN": str(executable)}, stdout=output,
            stderr=subprocess.STDOUT, timeout=240,
        )
    evidence = log.read_text()
    assert result.returncode == 0, evidence
    assert "PASS E2E-020-B1" in evidence and "PASS E2E-020-B2" in evidence
    assert "RESULT PASS E2E-020-C public contract only" in evidence
    assert "RESULT PASS E2E-020 FIRST_WAVE B1/B2-ABSENT/C = 3/3" in evidence
    assert "RESULT FAIL" not in evidence
