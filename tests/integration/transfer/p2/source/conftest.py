"""Disposable real daemon; no model account, production home or mesh."""

import os
from pathlib import Path
import shutil
import sys
import tempfile

import pytest

from hyprial.daemon.impl.desired_state import DesiredState, DesiredStateStore
from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import IsolatedHarness
from tests.support.runtime._tmp import hyprial_test_tmp_root

OWNER = "transfer-test-owner"
NODE = "node-e2e"
SOURCE = "transfer-source"
CONTROL = "transfer-control"


def seed_workers(harness, *, extra=()):
    """Persist actual launch specs pointing only at the repository protocol double."""
    fixture = Path(__file__).resolve().parents[4] / "fixtures/harnesses/fake_pi_rpc.py"
    assert fixture.is_file(), "test fixture missing, not a transfer product failure"
    store = DesiredStateStore(
        harness.state / "desired-state.json",
        state_db=StateDatabase(harness.state / "lifecycle-operations.sqlite3"),
    )
    store.save(DesiredState(harnesses=tuple(
        HarnessLaunchSpec(harness="pi", name=name, headless=True, cwd=str(harness.root),
                          session_ref=f"test-{name}", command=(sys.executable, str(fixture)))
        for name in (SOURCE, CONTROL, *extra)
    )))


@pytest.fixture
def real_daemon():
    root = Path(tempfile.mkdtemp(prefix="p2src-", dir=hyprial_test_tmp_root()))
    daemon = IsolatedHarness(root)
    # Do not inherit worker credentials, forwarding settings, production socket,
    # tool homes or API keys. Both daemon roots and HOME are disposable.
    daemon.environment = {
        "PATH": os.environ["PATH"], "PYTHONPATH": str(Path(__file__).resolve().parents[5] / "src"),
        "HOME": str(root), "HYPRIAL_HOME": str(daemon.home),
        "HARNESS_STATE_DIR": str(daemon.state), "HYPRIAL_NODE_ID": NODE,
        "HYPRIAL_NETWORK_ISOLATED": "1", "HYPRIAL_PEER_DISCOVERY": "0",
        "HYPRIAL_ZENOH_LISTEN": "", "HYPRIAL_ZENOH_CONNECT": "",
        "UV_TOOL_DIR": str(root / "uv-tools"), "FAKE_PI_MODE": "settled",
    }
    daemon.write_owner(OWNER)
    try:
        yield daemon
    finally:
        if daemon.daemon is not None:
            for name in (SOURCE, CONTROL):
                try:
                    daemon.ipc("down", {"name": name, "provider": "pi"})
                except (AssertionError, OSError):
                    pass
            daemon.stop_daemon()
        shutil.rmtree(root)
