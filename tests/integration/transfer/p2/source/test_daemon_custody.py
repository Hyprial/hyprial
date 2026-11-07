"""Real daemon startup custody; fake model protocol does not mean fake daemon.

These tests arrange a C1 journal only while the disposable daemon is stopped.
They do not certify the not-yet-wired P2 quiesce IPC or model strict resume.
"""

from contextlib import closing
import sqlite3

import pytest

from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.transfer.p2.model import Binding, Role
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore
from .conftest import CONTROL, NODE, OWNER, SOURCE, seed_workers


@pytest.mark.parametrize("completed", [False, True], ids=["intent", "quiesced"])
def test_tc014_real_daemon_does_not_restore_source_with_durable_custody(real_daemon, completed):
    """Source intent/result survives daemon restart; unrelated worker still restores."""
    h = real_daemon
    seed_workers(h)
    h.start_daemon()
    before = {item["name"]: item for item in h.ipc("ps", {})["connectors"]}
    assert before[SOURCE]["running"] and before[CONTROL]["running"], "worker fixture did not start"
    h.stop_daemon()
    with closing(sqlite3.connect(h.state / "agents.sqlite3")) as conn:
        row = conn.execute("SELECT actor, entity_token FROM agents WHERE actor=?", (SOURCE,)).fetchone()
    assert row is not None, "baseline daemon did not create the source identity"
    actor, token = row
    store = AdmissionStore(StateDatabase(h.state / "lifecycle-operations.sqlite3"), node=NODE, owner=OWNER)
    store.prepare("migration-source", Binding(Role.SOURCE, OWNER, NODE, actor, token,
                                             "test-target", "test-visitor", "a" * 64))
    start = store.journal.begin("migration-source", step="quiesce", fingerprint="b" * 64, actor=actor)
    if completed:
        store.journal.finish("migration-source", expected=start.state,
                             receipt="test-arranged-stop-receipt", entity_token=token)
    # Explicit running intent mirrors a crash before desired-state cleanup.
    seed_workers(h)
    h.start_daemon()
    after = {item["name"]: item for item in h.ipc("ps", {})["connectors"]}
    assert after[CONTROL]["running"], "one held source must not block unrelated restore"
    assert SOURCE not in after or not after[SOURCE]["running"], (
        "daemon restored a source despite durable P2 custody"
    )
