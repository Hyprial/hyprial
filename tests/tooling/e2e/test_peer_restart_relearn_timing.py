from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from hyprial.daemon.impl.transport.zenoh.config import (
    DEFAULT_CONNECT_RETRY_MAX_MS,
    DEFAULT_LEASE_MS,
)


ROOT = Path(__file__).resolve().parents[3]
RUNNER = ROOT / "contract" / "e2e" / "run-peer-restart-relearn.py"


def test_relearn_bound_uses_the_product_lease_and_retry_defaults() -> None:
    scenario = runpy.run_path(str(RUNNER))

    assert scenario["LEASE_MS"] == DEFAULT_LEASE_MS
    assert scenario["CONNECT_RETRY_MAX_MS"] == DEFAULT_CONNECT_RETRY_MAX_MS
    assert scenario["STALE_THRESHOLD_SECONDS"] == (
        DEFAULT_LEASE_MS + DEFAULT_CONNECT_RETRY_MAX_MS
    ) / 1_000


def test_outer_deadline_is_not_retried_as_a_transient_probe_failure():
    scenario = runpy.run_path(str(RUNNER))

    def expired():
        raise scenario["ScenarioTimeout"]("outer deadline")

    with pytest.raises(scenario["ScenarioTimeout"], match="outer deadline"):
        scenario["_wait"]("probe", expired, lambda _value: False, timeout=1)


def test_failed_probe_cannot_satisfy_a_truthy_success_predicate():
    scenario = runpy.run_path(str(RUNNER))

    def failed():
        raise AssertionError("temporarily unavailable")

    with pytest.raises(AssertionError, match="did not complete"):
        scenario["_wait"]("probe", failed, bool, timeout=0.01)


def test_case_specific_a_directory_record_keeps_the_roster_identity():
    import json

    scenario = runpy.run_path(str(RUNNER))
    calls = []

    class Daemon:
        def command(self, *args):
            calls.append(args)

    member = scenario["_LOCAL_MEMBER"]("case-owner", "orgfs-redial-a-case", "tc-e2e-unix:/tmp/private.sock")
    scenario["plant_device"](Daemon(), "space", member)
    directory = json.loads(calls[1][5])
    assert directory["serverPublic"] == member["serverPublic"] == "nodekey:" + "01" * 32
    assert directory["clientPublic"] == member["masterPublic"] == "nodekey:" + "02" * 32
    assert directory["deviceId"] == "orgfs-redial-a-case"
    assert calls[1][4] == "directory/devices/case-owner/orgfs-redial-a-case.json"
