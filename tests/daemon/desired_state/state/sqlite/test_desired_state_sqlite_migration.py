"""Migration of pre-SQLite homes into the SQLite authority (U0a-2).

Allen's second ruling collapsed the migration to ONE rule:

    At construction: if the canonical ``.v1`` document exists, import it in
    ONE transaction (replace-all; re-importing the same data is lossless,
    so this is idempotent) and archive the file as ``.v1.migrated-<ts>``.
    If it does not exist, do nothing.

Three tests carry the rule (plus the malformed-source and empty-document
pins):

  - first import is EQUIVALENT (round-trip reuse):
    ``read_document()`` == the parsed pre-migration JSON
  - repeating the import of the same data is LOSSLESS (replace-all
    idempotence -- this is what makes crash-before-archive benign)
  - the archive rename is FATAL on failure: re-import being lossless
    relies on the .v1 being gone once imported, so a left-behind .v1 must
    stop the daemon instead of silently replacing newer SQLite state on
    the next start

Two sentences are pinned because the rule would be wrong without them:

  - the import reads ONLY the .v1 file -- ``desired-state.json`` is never
    looked at, because on every real upgraded machine it holds the
    migration.py poison pill (provider ``__hyprial_desired_state_schema_v1__``)
    and importing it would create a harness named "rollback-blocked"
  - an empty document still writes the root_state row, so "no rows" means
    "never stored", never "user removed everything"
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.desired_state import (
    DesiredState,
    DesiredStateStore,
    )

from hyprial.kernel import DesiredStateError
from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow
from hyprial.daemon.impl.state_db import StateDatabase

_V1_DOCUMENT: dict[str, Any] = {
    "schemaVersion": 1,
    "asMailbox": True,
    "providers": [
        {
            "provider": "codex",
            "name": "worker-1",
            "headless": True,
            "args": ["--full-auto"],
            "ownership": "managed",
            "nickname": "w1",
        }
    ],
    "interactiveSessions": [],
    "channelPins": {},
    "deprecatedSharedChannels": [],
    "zenoh": {"listen": [], "connect": []},
}

_POISON_PILL = [
    {
        "provider": "__hyprial_desired_state_schema_v1__",
        "name": "rollback-blocked",
        "note": "desired state uses schema v1; upgrade hyprial before starting the daemon",
    }
]


def _seed_v1(state_dir: Path, document: dict[str, Any] | None = None) -> Path:
    versioned = state_dir / "desired-state.json.v1"
    versioned.write_text(
        json.dumps(_V1_DOCUMENT if document is None else document),
        encoding="utf-8",
    )
    return versioned


def _archives(state_dir: Path, name: str) -> list[Path]:
    return sorted(state_dir.glob(f"{name}.migrated-*"))


def test_read_document_guards_return_none_for_every_empty_layout(
    tmp_path: Path,
) -> None:
    """The \"SQLite is empty\" judgment, pinned at its three layouts.

    document exists <=> the root_state table exists AND holds id=1.  Each
    guard here was a real bug shape: (a) connecting to a missing file
    creates it (read-only stores must leave no files), (b) a file that the
    journal bootstrapped first has no desired-state tables at all (the
    \"no such table\" crash), (c) DDL commits before the first transaction,
    so tables-without-rows is a reachable layout.  All three mean empty;
    none may raise, create files, or look like a document.
    """

    db_path = tmp_path / "lifecycle-operations.sqlite3"

    # (a) file absent.
    assert DesiredStateSqliteShadow(StateDatabase(db_path)).read_document() is None
    assert not db_path.exists()

    # (b) file present, desired-state tables absent (journal-only layout).
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            "CREATE TABLE IF NOT EXISTS lifecycle_meta ("
            "key TEXT PRIMARY KEY, value INTEGER NOT NULL);"
        )
        connection.commit()
    finally:
        connection.close()
    assert DesiredStateSqliteShadow(StateDatabase(db_path)).read_document() is None

    # (c) desired-state tables present, zero rows (rolled-back first write).
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            "CREATE TABLE IF NOT EXISTS root_state ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "schema_version INTEGER NOT NULL, as_mailbox INTEGER NOT NULL);"
        )
        connection.commit()
    finally:
        connection.close()
    assert DesiredStateSqliteShadow(StateDatabase(db_path)).read_document() is None

    # ... and a document, once written, is visible.
    DesiredStateSqliteShadow(StateDatabase(db_path)).write_document(
        _V1_DOCUMENT
    )
    rebuilt = DesiredStateSqliteShadow(StateDatabase(db_path)).read_document()
    assert rebuilt == _V1_DOCUMENT


def test_empty_document_still_writes_root_row(tmp_path: Path) -> None:
    """\"No rows\" must mean \"never stored\", not \"user removed everything\".

    ``_insert_all`` writes the root_state id=1 row UNCONDITIONALLY, so even
    an empty document leaves a row -- that is what keeps the migration's
    \"SQLite empty\" judgment (row existence) from misreading a user's
    deliberate empty state as \"never migrated\" (which would re-import
    stale files).  This test pins that property; if the root insert ever
    becomes conditional, the misfire encoded below becomes reachable by
    simply deleting the last harness.
    """

    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.save(DesiredState())  # the user's last harness is gone

    # The empty document IS a document: the root row exists and the store
    # reports the empty state -- not \"nothing was ever stored\".
    connection = sqlite3.connect(
        tmp_path / "lifecycle-operations.sqlite3"
    )
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM root_state WHERE id = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1
    rebuilt = DesiredStateSqliteShadow(
        StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    ).read_document()
    assert rebuilt is not None
    assert DesiredState.from_json(rebuilt) == DesiredState()
    assert store.load() == DesiredState()

    # A second store must not misread the deliberate empty state.
    assert DesiredStateStore(tmp_path / "desired-state.json").load() == DesiredState()


def test_first_import_is_equivalent_and_archives_the_source(
    tmp_path: Path,
) -> None:
    versioned = _seed_v1(tmp_path)

    store = DesiredStateStore(tmp_path / "desired-state.json")

    # The imported state is the authority (equivalence assertion: what the
    # JSON said is what the store reports, field for field).
    state = store.load()
    assert state == DesiredState.from_json(_V1_DOCUMENT)
    assert state.harnesses[0].name == "worker-1"
    # The source file is archived (kept, renamed), not left in place.
    assert not versioned.exists()
    archives = _archives(tmp_path, "desired-state.json.v1")
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8")) == _V1_DOCUMENT
    # Nothing writes the canonical file anymore: it simply stays absent.
    assert not (tmp_path / "desired-state.json").exists()

    # Migration equivalence (round-trip reuse): JSON -> SQLite -> document
    # equals JSON -> document.
    rebuilt = DesiredStateSqliteShadow(
        tmp_path / "lifecycle-operations.sqlite3"
    ).read_document()
    assert rebuilt == DesiredState.from_json(json.loads(archives[0].read_text(encoding="utf-8"))).to_json()


def test_re_importing_the_same_data_is_lossless(tmp_path: Path) -> None:
    # First construction imports and archives.
    _seed_v1(tmp_path)
    first = DesiredStateStore(tmp_path / "desired-state.json")
    assert first.load() == DesiredState.from_json(_V1_DOCUMENT)

    # The rule is replace-all and re-importing the same data is lossless:
    # seed the SAME document again (the benign crash-before-archive shape)
    # and let a second construction import it once more.
    _seed_v1(tmp_path)
    second = DesiredStateStore(tmp_path / "desired-state.json")

    assert second.load() == DesiredState.from_json(_V1_DOCUMENT)
    assert len(_archives(tmp_path, "desired-state.json.v1")) == 2


def test_rename_failure_is_startup_failure(tmp_path: Path) -> None:
    """A failed archive rename must stop the daemon -- never continue.

    Re-import being lossless relies on the .v1 being gone once imported.
    If the rename fails (permissions / read-only / disk) and the daemon
    kept running, the user's next change would save newer state into
    SQLite while the stale .v1 stayed behind -- and the next start would
    replace that newer state with the stale document, silently.  So the
    rename is fatal: nothing catches it, and the startup fails.
    """

    versioned = _seed_v1(tmp_path)
    original_rename = Path.rename

    def _failing_rename(self: Path, target: object) -> Path:
        raise OSError(f"injected rename failure for {self}")

    import pathlib

    pathlib.Path.rename = _failing_rename  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError, match="injected rename failure"):
            DesiredStateStore(tmp_path / "desired-state.json")
    finally:
        pathlib.Path.rename = original_rename  # type: ignore[method-assign]

    # The import itself committed (the benign variant): SQLite holds the
    # document, and the unconsumed source file is still there for the
    # operator -- the next successful start re-imports the same data.
    rebuilt = DesiredStateSqliteShadow(
        StateDatabase(tmp_path / "lifecycle-operations.sqlite3")
    ).read_document()
    assert rebuilt == DesiredState.from_json(_V1_DOCUMENT).to_json()
    assert versioned.exists()
    assert not _archives(tmp_path, "desired-state.json.v1")


def test_malformed_v1_fails_loudly_and_names_the_file(tmp_path: Path) -> None:
    versioned = _seed_v1(tmp_path, {"schemaVersion": 1, "providers": "nope"})

    with pytest.raises(DesiredStateError, match="cannot import desired state"):
        DesiredStateStore(tmp_path / "desired-state.json")
    # Nothing was consumed: the broken file is still there for the operator.
    assert versioned.exists()
    assert not _archives(tmp_path, "desired-state.json.v1")


def test_poison_pill_in_desired_state_json_is_never_imported(
    tmp_path: Path,
) -> None:
    """The import reads ONLY the .v1 file; desired-state.json is not looked at.

    On every real upgraded machine desired-state.json holds the migration.py
    poison pill -- reading it would import a harness named
    \"rollback-blocked\".
    """

    # The normal upgraded layout: real .v1 beside the poison pill.
    versioned = _seed_v1(tmp_path)
    (tmp_path / "desired-state.json").write_text(
        json.dumps(_POISON_PILL), encoding="utf-8"
    )

    store = DesiredStateStore(tmp_path / "desired-state.json")

    state = store.load()
    assert state == DesiredState.from_json(_V1_DOCUMENT)
    assert not any(
        harness.name == "rollback-blocked" for harness in state.harnesses
    )
    assert not versioned.exists()


def test_desired_state_json_without_v1_is_ignored(tmp_path: Path) -> None:
    # A canonical file with no .v1 beside it: the rule says \"do nothing\"
    # -- whatever that file holds (poison pill, legacy array) is not the
    # store's input.
    (tmp_path / "desired-state.json").write_text(
        json.dumps(_POISON_PILL), encoding="utf-8"
    )

    store = DesiredStateStore(tmp_path / "desired-state.json")

    assert store.load() == DesiredState()
    assert not _archives(tmp_path, "desired-state.json")
    assert (tmp_path / "desired-state.json").exists()
