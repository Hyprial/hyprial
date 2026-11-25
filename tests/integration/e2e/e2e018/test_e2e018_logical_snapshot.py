from __future__ import annotations

import ast
import importlib.util
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import ModuleType


SCENARIO = (
    Path(__file__).resolve().parents[4] / 'contract/e2e/run-pac-graph-slice.py'
)


def _load_scenario() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e018_scenario", SCENARIO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_logical_database_digest_ignores_checkpoint_but_detects_row_change(
    tmp_path: Path,
) -> None:
    scenario = _load_scenario()
    database = tmp_path / "graph.sqlite3"

    with closing(sqlite3.connect(database)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        writer.execute("INSERT INTO facts(value) VALUES ('first')")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        writer.execute("INSERT INTO facts(value) VALUES ('second')")
        writer.commit()
        logical_before = scenario.logical_database_digest(database)
        raw_before = database.read_bytes()

        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw_after = database.read_bytes()
        logical_after = scenario.logical_database_digest(database)

        assert raw_after != raw_before, "the old raw-byte invariant must fail"
        assert logical_after == logical_before

        writer.execute("INSERT INTO facts(value) VALUES ('third')")
        writer.commit()

    assert scenario.logical_database_digest(database) != logical_after


def test_every_post_flag_assert_has_an_explicit_failure_detail() -> None:
    tree = ast.parse(SCENARIO.read_text(encoding="utf-8"))
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    flag_helper = next(
        candidate
        for node in main.body
        if isinstance(node, ast.With)
        for statement in node.body
        if isinstance(statement, ast.Try)
        for candidate in statement.body
        if isinstance(candidate, ast.FunctionDef) and candidate.name == "flag"
    )
    first_flag_call = min(
        node.lineno
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "flag"
    )
    missing = [
        node.lineno
        for node in ast.walk(main)
        if isinstance(node, ast.Assert)
        and node.lineno > first_flag_call
        and node.msg is None
    ]
    post_flag_helpers = [
        flag_helper,
        *[
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"public_events", "workflow_status"}
        ],
    ]
    missing.extend(
        node.lineno
        for helper in post_flag_helpers
        for node in ast.walk(helper)
        if isinstance(node, ast.Assert) and node.msg is None
    )

    assert missing == [], f"post-flag asserts missing failure detail at lines={missing}"
