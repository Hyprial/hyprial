"""E2E-001's containers must carry the isolation owner.

The v0.4.5 nightly (isolated-macos task 9379, and 9294 before it with the
identical first failure line) went red at phase 1: the containers' daemon
refused to start -- "user identity is not set: HYPRIAL_OWNER is unset or
empty, and /data/home/settings.json has no 'owner'".  run-two-machine-
roundtrip.py builds its own container environment, so the containers never
got the `HYPRIAL_OWNER=e2e` that _daemon_runner gives every IsolatedDaemon
(#361 made the owner mandatory, U5 removed `init --owner`).  Card: spec
fix-e2e001-owner 2026-09-13; same fix shape as E2E-004's
test_e2e004_outbound_daemon_owner.py (1b817daf).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNNER = ROOT / "contract" / "e2e" / "run-two-machine-roundtrip.py"


def load_runner():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "e2e001_container_owner_under_test", RUNNER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(RUNNER.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(RUNNER.parent))
    return module


def test_every_container_carries_the_isolation_owner() -> None:
    runner = load_runner()
    runner._DERP.update({"host": "172.29.31.2", "derpPort": 12345, "stunPort": 12346, "insecureForTests": True})
    environment = runner.container_environment(
        "hyprial-e2e001-a"
    )
    # The rule, same as _daemon_runner / E2E-004: the isolation defines the
    # identity, fixed, never inherited from the host shell.
    assert environment["HYPRIAL_OWNER"] == "e2e"
    # GHOST_ACTOR mints `agent:<owner>:e2e001-ghost-node:ghost` and
    # driver_uri reads the owner back from the daemon's own report; the
    # container env must agree with the URI literal or the two sides of the
    # canonical identity drift apart.
    ghost_owner = runner.GHOST_ACTOR.split(":")[1]
    assert environment["HYPRIAL_OWNER"] == ghost_owner
    # The extraction must not lose the shape phase code depends on.
    assert environment["HYPRIAL_NODE_ID"] == "hyprial-e2e001-a"
    assert environment["HYPRIAL_HOME"] == "/data/home"
    assert environment["HARNESS_STATE_DIR"] == "/data/state"
    assert environment["HYPRIAL_ZENOH_LISTEN"] == "unixsock-stream//data/home/net/zenoh.sock"
    assert "HYPRIAL_ZENOH_CONNECT" not in environment


def test_extra_env_still_merges_last() -> None:
    # Phase 6 (HYPRIAL_HOLD_TTL_MS) and phase 7 (mailbox role) pass extra_env;
    # _daemon_runner documents "runners that need a specific owner assign
    # env['HYPRIAL_OWNER'] after construction" -- the merge order is that
    # rule's docker twin and must survive the extraction.
    runner = load_runner()
    runner._DERP.update({"host": "172.29.31.2", "derpPort": 12345, "stunPort": 12346, "insecureForTests": True})
    environment = runner.container_environment(
        "hyprial-e2e001-c",
        extra_env={"HYPRIAL_HOLD_TTL_MS": "25000"},
    )
    assert environment["HYPRIAL_HOLD_TTL_MS"] == "25000"
    assert environment["HYPRIAL_OWNER"] == "e2e"
