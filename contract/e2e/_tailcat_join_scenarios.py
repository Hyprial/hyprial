"""Run v7 transport acceptance using compiled real engine tests and a local DERP.

No daemon, account or production socket is used. The binary contains the current
checkout's engine and membership packages, not a fake sidecar implementation.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "./internal/membershiptest"


def run(scenario: str, test: str) -> int:
    go = shutil.which("go")
    if go is None:
        print(f"RESULT BLOCKED {scenario}: Go toolchain is missing")
        return 3
    started = time.monotonic()
    evidence = {"scenario": scenario, "test": test, "scope": "real-v7-transport",
                "result": "FAIL", "cliCasdoorOrgfs": "NOT_RUN"}
    try:
        with tempfile.TemporaryDirectory(prefix="hr-join-", dir="/tmp") as temporary:
            root = Path(temporary)
            binary = root / "acceptance.test"
            # Build with the usual Go dependency/cache environment. Only the
            # compiled test is run, under a separate HOME and no actor session.
            build = subprocess.run(
                [go, "test", "-c", "-o", str(binary), PACKAGE],
                cwd=ROOT / "sidecar/hyprial-tailcat", capture_output=True,
                text=True, timeout=300,
            )
            if build.returncode:
                evidence["reason"] = "Go acceptance binary did not build"
                print(f"RESULT FAIL {scenario}: Go acceptance binary did not build")
                return 1
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("HYPRIAL_", "HARNESS_", "PI_"))}
            home = root / "home"
            home.mkdir(mode=0o700)
            env.update(HOME=str(home), HYPRIAL_HOME=str(home / "hyprial"),
                       HARNESS_STATE_DIR=str(home / "state"), HYPRIAL_NETWORK_ISOLATED="1")
            command = [str(binary), "-test.run", f"^{test}$", "-test.v",
                       "-test.count=1", "-test.timeout=4m"]
            result = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                    text=True, timeout=270)
            # A zero exit with no selected test, or a skip, is NOT acceptance.
            lines = result.stdout.splitlines()
            passed = (result.returncode == 0
                      and any(line.startswith(f"--- PASS: {test} (") for line in lines)
                      and "PASS" in lines
                      and not any("--- SKIP:" in line for line in lines))
            evidence["exitCode"] = result.returncode
            evidence["executed"] = any(line == f"=== RUN   {test}" for line in lines)
            evidence["result"] = "PASS" if passed else "FAIL"
            # Preserve only assertion lines from our test, never transport logs
            # which can include address credentials. Go failure details remain
            # available by running the exact named test directly.
            evidence["assertions"] = [line.strip().split(scenario + ":", 1)[1].strip()
                                      for line in lines if scenario + ":" in line]
            print(f"RESULT {evidence['result']} {scenario}: {test} "
                  "scope=real-v7-transport cli/casdoor/orgfs=NOT_RUN")
            return 0 if passed else 1
    except (OSError, subprocess.TimeoutExpired) as error:
        evidence["reason"] = type(error).__name__
        print(f"RESULT FAIL {scenario}: {type(error).__name__}")
        return 1
    finally:
        evidence["durationSeconds"] = round(time.monotonic() - started, 2)
        destination = Path(os.environ.get("HYPRIAL_E2E_ARTIFACTS", ROOT / "e2e-artifacts"))
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{scenario.lower()}-{time.time_ns()}.json"
        target.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(f"RESULT evidence {scenario}: {target}")
