"""The read-only PAC graph definition feed and its post-commit notification (S7c)."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from hyprial.daemon.impl.pac.contracts.workflow import load_workflow_text
from hyprial.daemon.impl.pac.storage.definitions import (
    definition_size,
    definitions_after,
    digests_after,
    notify_definition_published,
    read_definition_text,
    register_definition_sink,
)
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.daemon.impl.pac.workflows.graphs import compile_workflow

USER = "user:alice"


def _spec(name: str = "work"):
    return load_workflow_text(
        yaml.safe_dump(
            {"version": 2, "name": name, "nodes": [{"id": "build", "task": "implement"}]}
        )
    )


def _compile(store: PacGraphStore, name: str, key: str) -> str:
    return compile_workflow(
        store,
        _spec(name),
        sender=USER,
        machine="machine",
        local_owner="alice",
        operation_key=key,
        at=100,
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    (tmp_path / "state").mkdir()
    opened = PacGraphStore(default_database_path(tmp_path / "state"))
    try:
        yield opened
    finally:
        opened.close()


def test_feed_lists_compiled_definitions_in_commit_order(store: PacGraphStore) -> None:
    first = _compile(store, "alpha", "one")
    second = _compile(store, "beta", "two")
    rows = definitions_after(store.path, 0, limit=32)
    assert [row.graph_id for row in rows] == [first, second]
    assert [row.name for row in rows] == ["alpha", "beta"]
    assert rows[0].position < rows[1].position
    assert definitions_after(store.path, rows[0].position, limit=32) == rows[1:]
    assert definitions_after(store.path, 0, limit=1) == rows[:1]
    text = read_definition_text(rows[0])
    assert text is not None
    assert sha256(text.encode("utf-8")).hexdigest() == rows[0].digest
    assert text == _spec("alpha").canonical


def test_feed_is_empty_without_a_database(tmp_path: Path) -> None:
    assert definitions_after(tmp_path / "missing.sqlite3", 0, limit=32) == ()
    assert digests_after(tmp_path / "missing.sqlite3", 0, limit=32) == ()


def test_the_digest_feed_pages_positions_and_digests_without_rows(
    store: PacGraphStore,
) -> None:
    for number, name in enumerate(("alpha", "beta", "alpha")):
        _compile(store, name, f"key-{number}")
    rows = definitions_after(store.path, 0, limit=32)
    pairs = digests_after(store.path, 0, limit=32)
    assert pairs == tuple((row.position, row.digest) for row in rows)
    assert pairs[0][1] == pairs[2][1] != pairs[1][1]
    assert digests_after(store.path, pairs[0][0], limit=1) == pairs[1:2]


def test_definition_size_reads_metadata_and_refuses_a_missing_artifact(
    store: PacGraphStore,
) -> None:
    _compile(store, "alpha", "one")
    row = definitions_after(store.path, 0, limit=32)[0]
    assert definition_size(row) == len(_spec("alpha").canonical.encode("utf-8"))
    Path(row.specification_ref).unlink()
    assert definition_size(row) is None


def test_a_changed_specification_file_reads_as_unavailable(store: PacGraphStore) -> None:
    _compile(store, "alpha", "one")
    row = definitions_after(store.path, 0, limit=32)[0]
    Path(row.specification_ref).chmod(0o600)
    Path(row.specification_ref).write_text("{}", encoding="utf-8")
    assert read_definition_text(row) is None


def test_compile_notifies_the_sink_registered_for_its_database(store: PacGraphStore) -> None:
    seen: list[Path] = []
    unregister = register_definition_sink(store.path, seen.append)
    try:
        _compile(store, "alpha", "one")
        _compile(store, "alpha", "one")
    finally:
        unregister()
    assert seen == [store.path.resolve()]
    _compile(store, "beta", "two")
    assert seen == [store.path.resolve()]


def test_a_raising_sink_never_fails_the_compile(store: PacGraphStore) -> None:
    def broken(_path: Path) -> None:
        raise RuntimeError("mirror down")

    unregister = register_definition_sink(store.path, broken)
    try:
        graph_id = _compile(store, "alpha", "one")
    finally:
        unregister()
    assert store.graph(graph_id) is not None
    notify_definition_published(store.path)


def test_without_a_sink_notification_is_a_no_op(store: PacGraphStore) -> None:
    notify_definition_published(store.path)
    assert _compile(store, "alpha", "one").startswith("wf-")
