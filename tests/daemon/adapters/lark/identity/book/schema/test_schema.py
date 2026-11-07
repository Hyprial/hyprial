from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest
from hyprial.daemon.impl.adapters.lark import LarkStateStore
from hyprial.daemon.impl.adapters.lark.state.records import IDENTITIES_SCHEMA_VERSION, STATE_SCHEMA_VERSION



def test_plain_v2_database_gains_identities_table_and_sub_stamp(
    tmp_path: Path,
) -> None:
    """An existing v2 database upgrades in place instead of failing closed."""

    path = tmp_path / "adapters.sqlite3"
    seeded = LarkStateStore(path, adapter="lark:main")
    seeded.record_seen("evt-pre-upgrade")
    seeded.close()
    # Rewind the database to its pre-identities v2 shape.
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX identities_by_name")
        db.execute("DROP TABLE identities")
        db.execute("DELETE FROM meta WHERE key = 'identitiesSchemaVersion'")

    store = LarkStateStore(path, adapter="lark:main")

    # Pre-existing state survived; the new table works; the sub-schema is
    # stamped under its own key.
    assert store.seen("evt-pre-upgrade")
    store.observe_identity("user", "ou_x", source="chat-members:oc_g")
    assert store.identity("user", "ou_x") is not None
    with sqlite3.connect(path) as db:
        stamps = dict(db.execute("SELECT key, value FROM meta").fetchall())
    assert stamps["identitiesSchemaVersion"] == str(IDENTITIES_SCHEMA_VERSION)
    # The global stamp is untouched: a deployed v2 build exact-matches it and
    # must keep opening this database (the table is invisible to it) even if
    # it restarts before the identities-aware rollout completes.
    assert stamps["schemaVersion"] == str(STATE_SCHEMA_VERSION) == "2"


def test_unknown_and_json_era_versions_still_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "adapters.sqlite3"
    LarkStateStore(path).close()
    for version in ("1", "999"):
        with sqlite3.connect(path) as db:
            db.execute(
                "UPDATE meta SET value = ? WHERE key = 'schemaVersion'",
                (version,),
            )
        with pytest.raises(ValueError, match="unsupported Lark state schema"):
            LarkStateStore(path)


def test_unknown_identities_schema_version_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "adapters.sqlite3"
    LarkStateStore(path).close()
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE meta SET value = '999'"
            " WHERE key = 'identitiesSchemaVersion'"
        )
    with pytest.raises(ValueError, match="unsupported Lark identities schema"):
        LarkStateStore(path)
