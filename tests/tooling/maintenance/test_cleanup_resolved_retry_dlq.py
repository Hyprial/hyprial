from __future__ import annotations

import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/archived/cleanup_resolved_retry_dlq.py'


def database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE inbox(
            message_id TEXT PRIMARY KEY
        );
        CREATE TABLE delivery_status(
            message_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE TABLE dlq(
            dlq_id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        INSERT INTO inbox VALUES ('msg-resolved');
        INSERT INTO inbox VALUES ('msg-pending');
        INSERT INTO delivery_status VALUES
            ('msg-resolved', 'fetched', 'RECIPIENT_COMMIT');
        INSERT INTO dlq(message_id, reason)
            VALUES ('msg-resolved', 'DELIVERY_RETRY_EXHAUSTED');
        INSERT INTO dlq(message_id, reason)
            VALUES ('msg-pending', 'DELIVERY_RETRY_EXHAUSTED');
        """
    )
    connection.commit()
    connection.close()
    return path


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_cleanup_defaults_to_fetched_evidence_dry_run(tmp_path: Path) -> None:
    path = database(tmp_path / "inbox.sqlite3")

    result = run_script("--database", str(path), "--message-id", "msg-resolved")

    assert result.returncode == 0
    assert "DRY-RUN verified 1 false retry" in result.stdout
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM dlq").fetchone()[0] == 2


def test_cleanup_requires_confirmation_then_backs_up_exact_target(
    tmp_path: Path,
) -> None:
    path = database(tmp_path / "inbox.sqlite3")
    backup = tmp_path / "before.sqlite3"

    refused = run_script(
        "--database", str(path), "--message-id", "msg-resolved", "--apply"
    )
    assert refused.returncode != 0
    assert "--confirm-fetched" in refused.stderr

    result = run_script(
        "--database",
        str(path),
        "--message-id",
        "msg-resolved",
        "--apply",
        "--confirm-fetched",
        "--backup",
        str(backup),
    )

    assert result.returncode == 0
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT message_id FROM dlq").fetchall() == [
            ("msg-pending",)
        ]
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT COUNT(*) FROM dlq").fetchone()[0] == 2


def test_cleanup_refuses_unfetched_true_failure(tmp_path: Path) -> None:
    path = database(tmp_path / "inbox.sqlite3")

    result = run_script(
        "--database",
        str(path),
        "--message-id",
        "msg-pending",
        "--apply",
        "--confirm-fetched",
    )

    assert result.returncode != 0
    assert "not disproved by recipient fetched evidence" in result.stderr
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM dlq").fetchone()[0] == 2
