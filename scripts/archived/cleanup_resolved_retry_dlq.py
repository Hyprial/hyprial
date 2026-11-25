#!/usr/bin/env python3
"""Remove explicitly selected retry DLQ rows disproved by fetched evidence.

This repairs the historical pull-retirement defect only. The command is dry-run
by default. Apply mode requires exact message IDs, a durable inbox row plus a
fetched recipient terminal record, explicit confirmation, and an online SQLite
backup first.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Candidate:
    dlq_id: int
    message_id: str
    reason: str
    in_inbox: bool
    terminal_state: str | None
    terminal_reason: str | None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--message-id",
        action="append",
        dest="message_ids",
        required=True,
        help="Exact resolved message ID; repeat once per DLQ row.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete verified rows after creating a backup. Default is dry-run.",
    )
    parser.add_argument(
        "--confirm-fetched",
        action="store_true",
        help="Acknowledge every selected retry failure has fetched evidence.",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="Apply-mode backup path; defaults beside the database.",
    )
    return parser


def _candidates(
    connection: sqlite3.Connection, message_ids: Sequence[str]
) -> tuple[Candidate, ...]:
    placeholders = ",".join("?" for _ in message_ids)
    rows = connection.execute(
        f"""SELECT d.dlq_id, d.message_id, d.reason,
                   EXISTS(SELECT 1 FROM inbox i
                           WHERE i.message_id = d.message_id),
                   (SELECT s.state FROM delivery_status s
                     WHERE s.message_id = d.message_id),
                   (SELECT s.reason FROM delivery_status s
                     WHERE s.message_id = d.message_id)
              FROM dlq d
             WHERE d.message_id IN ({placeholders})
             ORDER BY d.dlq_id""",
        tuple(message_ids),
    ).fetchall()
    return tuple(
        Candidate(
            dlq_id=int(row[0]),
            message_id=str(row[1]),
            reason=str(row[2]),
            in_inbox=bool(row[3]),
            terminal_state=None if row[4] is None else str(row[4]),
            terminal_reason=None if row[5] is None else str(row[5]),
        )
        for row in rows
    )


def _verify(candidates: Sequence[Candidate], message_ids: Sequence[str]) -> None:
    expected = set(message_ids)
    if len(expected) != len(message_ids):
        raise ValueError("duplicate --message-id values are not allowed")
    by_message: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_message.setdefault(candidate.message_id, []).append(candidate)
    missing = sorted(expected - by_message.keys())
    duplicated = sorted(key for key, values in by_message.items() if len(values) != 1)
    unsafe = sorted(
        candidate.message_id
        for candidate in candidates
        if candidate.reason != "DELIVERY_RETRY_EXHAUSTED"
        or not candidate.in_inbox
        or candidate.terminal_state != "fetched"
        or candidate.terminal_reason not in {"RECIPIENT_COMMIT", "RECIPIENT_DUPLICATE"}
    )
    problems = []
    if missing:
        problems.append(f"missing from DLQ: {', '.join(missing)}")
    if duplicated:
        problems.append(f"multiple DLQ rows: {', '.join(duplicated)}")
    if unsafe:
        problems.append(
            "not disproved by recipient fetched evidence: " + ", ".join(unsafe)
        )
    if problems:
        raise ValueError("; ".join(problems))


def _default_backup(database: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return database.with_name(f"{database.name}.pre-resolved-dlq-cleanup-{stamp}.bak")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"database does not exist: {database}")
    message_ids = tuple(args.message_ids)
    if args.apply and not args.confirm_fetched:
        raise SystemExit("refused: --apply requires --confirm-fetched")

    connection = sqlite3.connect(
        f"{database.as_uri()}?mode={'rw' if args.apply else 'ro'}",
        uri=True,
        timeout=5.0,
    )
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        candidates = _candidates(connection, message_ids)
        _verify(candidates, message_ids)
        if not args.apply:
            print(
                f"DRY-RUN verified {len(candidates)} false retry DLQ row(s); "
                "no changes made"
            )
            return 0

        backup = (args.backup or _default_backup(database)).expanduser().resolve()
        if backup.exists():
            raise ValueError(f"backup already exists: {backup}")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup_connection = sqlite3.connect(backup)
        try:
            connection.backup(backup_connection)
        finally:
            backup_connection.close()
        os.chmod(backup, 0o600)

        connection.execute("BEGIN IMMEDIATE")
        candidates = _candidates(connection, message_ids)
        _verify(candidates, message_ids)
        placeholders = ",".join("?" for _ in candidates)
        cursor = connection.execute(
            f"DELETE FROM dlq WHERE dlq_id IN ({placeholders})",
            tuple(candidate.dlq_id for candidate in candidates),
        )
        if cursor.rowcount != len(candidates):
            connection.rollback()
            raise RuntimeError(
                f"delete count changed: expected {len(candidates)}, got {cursor.rowcount}"
            )
        connection.commit()
        print(
            f"APPLIED removed {len(candidates)} false retry DLQ row(s); backup={backup}"
        )
        return 0
    except (sqlite3.Error, ValueError, RuntimeError) as error:
        connection.rollback()
        raise SystemExit(f"refused: {error}") from error
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
