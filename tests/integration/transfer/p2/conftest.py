from dataclasses import replace

import pytest

from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.transfer.p2.model import Binding, Role
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore


@pytest.fixture
def binding():
    """Synthetic identity and digest, with no secret material or real account."""
    return Binding(
        role=Role.SOURCE, owner="test-owner", source_node="source",
        source_agent_id="a.synthetic", source_entity_token="source-incarnation",
        target_node="target", target_actor="visitor", plan_digest="a" * 64,
    )


@pytest.fixture
def ledger(tmp_path):
    """Real shared SQLite, a controlled wall clock, and no daemon sockets."""
    now = [1000]
    db = StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    store = AdmissionStore(db, node="source", owner="test-owner", clock_ms=lambda: now[0])
    return store, now, db


@pytest.fixture
def target_binding(binding):
    return replace(binding, role=Role.TARGET)
