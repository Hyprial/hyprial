"""A desired state that still holds a ``user-proxy`` launch spec does not load.

I7b-2 (owner A-4) deleted the ``user-proxy`` harness kind.  No compatibility
or migration code: a home that kept the spec fails to load its desired state
until the row is removed by hand (migration guide §14).  The failure names no
harness-specific hint, and no path drops the row silently.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.desired_state.documents import DesiredState, DesiredStateError
from hyprial.daemon.impl.desired_state.store import DesiredStateStore
from hyprial.kernel import DECLARED_HARNESSES, HarnessLaunchSpec

EXPECTED = "has an invalid harness; expected codex, claude, pi, jev, or agy"


def _relay_document() -> dict[str, object]:
    document = DesiredState(
        harnesses=(HarnessLaunchSpec(harness="claude", name="relay", headless=True),)
    ).to_json()
    for row in document["providers"]:
        row["provider"] = "user-proxy"
    return document


def test_user_proxy_is_not_a_declared_harness() -> None:
    assert "user-proxy" not in DECLARED_HARNESSES
    with pytest.raises(DesiredStateError) as raised:
        HarnessLaunchSpec.from_json(
            {"provider": "user-proxy", "name": "relay", "headless": True}, "spec"
        )
    assert EXPECTED in str(raised.value)
    assert "user-proxy" not in str(raised.value)


def test_a_sqlite_row_with_the_retired_kind_fails_the_load(tmp_path: Path) -> None:
    path = tmp_path / "desired-state.json"
    DesiredStateStore(path).upsert_harness(
        HarnessLaunchSpec(harness="claude", name="relay", headless=True)
    )
    database = tmp_path / "lifecycle-operations.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("UPDATE harnesses SET harness='user-proxy' WHERE name='relay'")

    with pytest.raises(DesiredStateError) as raised:
        DesiredStateStore(path).load()
    assert EXPECTED in str(raised.value)
    assert "user-proxy" not in str(raised.value)

    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT harness, name FROM harnesses").fetchall()
    assert rows == [("user-proxy", "relay")]


def test_a_legacy_json_document_with_the_retired_kind_fails_the_load(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desired-state.json"
    versioned = Path(f"{path}.v1")
    text = json.dumps(_relay_document())
    versioned.write_text(text, encoding="utf-8")

    with pytest.raises(DesiredStateError) as raised:
        DesiredStateStore(path).load()
    assert EXPECTED in str(raised.value)
    assert "user-proxy" not in str(raised.value)
    assert versioned.read_text(encoding="utf-8") == text
    assert not (tmp_path / "lifecycle-operations.sqlite3").exists()


def test_the_guide_repair_deletes_the_row_and_the_store_loads(tmp_path: Path) -> None:
    path = tmp_path / "desired-state.json"
    store = DesiredStateStore(path)
    store.upsert_harness(HarnessLaunchSpec(harness="claude", name="relay", headless=True))
    store.upsert_harness(HarnessLaunchSpec(harness="claude", name="keeper", headless=True))
    database = tmp_path / "lifecycle-operations.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("UPDATE harnesses SET harness='user-proxy' WHERE name='relay'")
    with pytest.raises(DesiredStateError):
        DesiredStateStore(path).load()

    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM harnesses WHERE harness = 'user-proxy';")

    loaded = DesiredStateStore(path).load()
    assert [(spec.harness, spec.name) for spec in loaded.harnesses] == [("claude", "keeper")]
