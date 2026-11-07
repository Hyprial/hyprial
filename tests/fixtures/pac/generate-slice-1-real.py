#!/usr/bin/env python3
"""Maintainer-only fixture generator; NOT run by pytest or CI.

Loads the five exact 17de00f6 PAC modules in memory, without checking out another
worktree. The original Graph/PacReactor APIs create a disposable legacy store.
SQL dump/source hashes are retained. --late-edit also emits an opaque database
fixture for CLI-only migration/late-receipt tests. No production state/actors.
Run: uv run --python 3.12 python tests/fixtures/pac/generate-slice-1-real.py [--late-edit]
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

BASE = "17de00f65b73468a985a78e3ce66b8e16319a406"
ROOT = Path(__file__).resolve().parent


def main():
    late_edit = "--late-edit" in sys.argv
    output = ROOT.parents[2] / "contract/pac/events/fixtures" if late_edit else ROOT
    output.mkdir(parents=True, exist_ok=True)
    prefix = "slice-1-late-delivery" if late_edit else "slice-1-real"
    package = types.ModuleType("original_slice1_pac")
    package.__path__ = []
    sys.modules[package.__name__] = package
    hashes = {}
    for name in ("errors", "store", "graph", "reactor", "projection"):
        path = f"src/hyprial/daemon/impl/pac/{name}.py"
        source = subprocess.check_output(["git", "show", f"{BASE}:{path}"])
        hashes[path] = hashlib.sha256(source).hexdigest()
        module = types.ModuleType(f"{package.__name__}.{name}")
        module.__file__ = f"{BASE}:{path}"
        sys.modules[module.__name__] = module
        setattr(package, name, module)
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    with tempfile.TemporaryDirectory(prefix="pac-original-fixture-") as raw:
        state = Path(raw) / "state"
        state.mkdir()
        os.environ.update(HYPRIAL_HOME=str(Path(raw) / "home"), HARNESS_STATE_DIR=str(state), HYPRIAL_OWNER="fixture-owner")
        store = package.store.PacGraphStore(state / "pac.sqlite3")
        try:
            gid = package.graph.create_graph(store, name="legacy-generated", created_by="fixture-owner")["graphId"]
            version = 1
            for node in ("a", "b", "timer"):
                version = package.graph.add_node(
                    store, state, graph_id=gid, node_id=node, owner="fixture-owner", brief_ref=f"artifact#brief-{node}",
                    kind="clock" if node == "timer" else "task", deadline_ms=50 if node == "timer" else None,
                    expect_version=version,
                )["version"]
            package.graph.add_edge(store, graph_id=gid, from_node="a", to_node="b", expect_version=version)
            reactor = package.reactor.PacReactor(store, clock=lambda: 100)
            reactor.set_flag(gid, "a", actor="fixture-owner", reason_ref="artifact#result-a")
            reactor.reset_flag(gid, "a", actor="fixture-owner", reason_ref="artifact#withdraw")
            reactor.set_flag(gid, "a", actor="fixture-owner", reason_ref="artifact#result-a-v2")
            reactor.set_flag(gid, "b", actor="fixture-owner", reason_ref="artifact#result-b")
            reactor.tick_clocks(gid)
            if late_edit:
                # Slice 1 really allowed this edit after flags/plans. A later
                # receipt belongs to v5 while the graph snapshot is now v6.
                package.graph.add_node(
                    store, state, graph_id=gid, node_id="later", owner="fixture-owner",
                    brief_ref="artifact#later", kind="task", expect_version=5,
                )
            assert store._db.execute("PRAGMA user_version").fetchone()[0] == 0
            dump = "\n".join(store._db.iterdump()) + "\n"
            (output / f"{prefix}.sql").write_text(dump)
            manifest = {"sourceCommit": BASE, "sourceSha256": hashes, "graphId": gid,
                        "dumpSha256": hashlib.sha256(dump.encode()).hexdigest(),
                        "userVersion": 0, "generator": Path(__file__).name,
                        "description": "Original store/graph/reactor; task set/reset/set/completion + clock tick; no sender"}
        finally:
            store.close()
        if late_edit:
            # Closing the last real connection checkpoints WAL before copying.
            database = (state / "pac.sqlite3").read_bytes()
            (output / f"{prefix}.sqlite3").write_bytes(database)
            manifest["databaseSha256"] = hashlib.sha256(database).hexdigest()
            manifest["description"] += "; then real old add_node v5->v6; late receipts remain v5"
            manifest["generator"] += " --late-edit"
        (output / f"{prefix}.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
