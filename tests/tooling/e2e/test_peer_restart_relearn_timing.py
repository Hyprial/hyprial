from __future__ import annotations

import runpy
from pathlib import Path

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
