from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from hyprial.daemon.impl.desired_state import (
    DesiredState,
    DesiredStateStore,
    InteractiveSession,
)


def test_store_startup_migrates_legacy_interactive_process_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desired-state.json"
    writer = DesiredStateStore(path)
    writer.save(
        DesiredState(
            interactive_sessions=(
                InteractiveSession(
                    actor="agent:owner:node:legacy-codex",
                    cwd=str(tmp_path),
                    command=("codex", "--remote"),
                    source="codex-app-server",
                    session_ref="legacy-codex-session",
                    runtime="codex_interactive",
                ),
            )
        )
    )
    database = tmp_path / "lifecycle-operations.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE interactive_sessions DROP COLUMN process_pid")
        connection.execute(
            "ALTER TABLE interactive_sessions DROP COLUMN process_identity"
        )

    restarted = DesiredStateStore(path)
    state = restarted.load()

    assert len(state.interactive_sessions) == 1
    session = state.interactive_sessions[0]
    assert session.actor == "agent:owner:node:legacy-codex"
    assert session.session_ref == "legacy-codex-session"
    assert session.process_pid is None
    assert session.process_identity is None
    with closing(sqlite3.connect(database)) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(interactive_sessions)")
        }
    assert {"process_pid", "process_identity"}.issubset(columns)
