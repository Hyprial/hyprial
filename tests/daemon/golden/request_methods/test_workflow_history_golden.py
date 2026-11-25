"""Golden projection from a populated legacy workflow archive."""

import sqlite3
from contextlib import closing
from collections.abc import Callable

from tests.support.golden.app import GoldenDaemon, golden_daemon as golden_daemon


def test_workflow_history_list_populated(golden_daemon: Callable[..., GoldenDaemon]) -> None:
    daemon = golden_daemon()
    with closing(sqlite3.connect(daemon.app.state_dir / "workflows.sqlite3")) as db:
        db.executescript("""
            CREATE TABLE runs(run_id TEXT,name TEXT,state TEXT,sender TEXT,
                              created_at_ms INTEGER,finished_at_ms INTEGER);
            INSERT INTO runs VALUES ('old', NULL, 'done', 'user:op', 'legacy-time', NULL);
        """)
    golden = daemon.golden("request_methods", "workflow-history-list")
    result = golden.call("workflow.history.list", {"actor": "user:op"}, label="populated")
    assert result["runs"][0]["created_at_ms"] == "legacy-time"
    assert result["runs"][0]["name"] is None
    golden.check()
