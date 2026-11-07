"""The opaque CLI setup image is the exact reviewable original-code SQL dump."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def test_late_receipt_fixture_matches_original_code_manifest_and_sql():
    root = Path(__file__).resolve().parents[4] / 'contract/pac/events/fixtures'
    prefix = root / "slice-1-late-delivery"
    manifest = json.loads(prefix.with_suffix(".json").read_text())
    assert manifest["sourceCommit"] == "17de00f65b73468a985a78e3ce66b8e16319a406"
    assert len(manifest["sourceSha256"]) == 5
    binary = prefix.with_suffix(".sqlite3")
    dump = prefix.with_suffix(".sql").read_text()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == manifest["databaseSha256"]
    assert hashlib.sha256(dump.encode()).hexdigest() == manifest["dumpSha256"]
    with closing(sqlite3.connect(f"{binary.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "\n".join(db.iterdump()) + "\n" == dump
        assert db.execute("SELECT version FROM graphs").fetchone()[0] == 6
        assert {row[0] for row in db.execute("SELECT version FROM flag_events")} == {5}
        assert "plan_json" not in {row[1] for row in db.execute("PRAGMA table_info(notifications)")}
