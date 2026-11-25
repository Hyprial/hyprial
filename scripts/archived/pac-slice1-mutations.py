#!/usr/bin/env python3
"""Mutation verification driver for the pac slice (red -> revert -> green).

Applies one mutation at a time to the implementation, runs the focused
tests, records red, reverts.  Not part of the shipped test suite; this is
the spec's 变异验证 receipt.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python")

MUTATIONS: list[tuple[str, Path, str, str, str]] = [
    (
        "M1-join-ignores-later-predecessors",
        ROOT / "src/hyprial/pac/reactor.py",
        "            predecessors = self._forward_predecessors(graph_id, target_id)",
        "            predecessors = self._forward_predecessors(graph_id, target_id)[:1]",
        "tests/test_pac_reactor.py",
    ),
    (
        "M2-cycle-check-disabled",
        ROOT / "src/hyprial/pac/graph.py",
        "        if cycle is not None:",
        "        if False:",
        "tests/test_pac_graph.py",
    ),
    (
        "M3-reset-sends-no-withdraw",
        ROOT / "src/hyprial/pac/reactor.py",
        "            if edge not in existing_edges:",
        "            if True:",
        "tests/test_pac_reactor.py",
    ),
    (
        "M3b-reset-withdraws-never-turned-edges-too",
        ROOT / "src/hyprial/pac/reactor.py",
        "            if edge not in existing_edges:",
        "            if False:",
        "tests/test_pac_reactor.py",
    ),
    (
        "M4-notifications-lose-their-pk",
        ROOT / "src/hyprial/pac/store.py",
        "    PRIMARY KEY (event_id, edge)",
        "    -- PRIMARY KEY (event_id, edge)",
        "tests/test_pac_reactor.py",
    ),
    (
        "M5-back-edge-round-always-one",
        ROOT / "src/hyprial/pac/reactor.py",
        "            round_no = self._store.set_event_count(graph_id, target_id) + 1",
        "            round_no = 1",
        "tests/test_pac_reactor.py tests/test_pac_restate.py",
    ),
    (
        "M6-owner-check-dropped",
        ROOT / "src/hyprial/pac/reactor.py",
        "        if actor != node.owner:",
        "        if False:",
        "tests/test_pac_reactor.py tests/test_pac_cli.py",
    ),
    (
        "M7-inbox-import-planted-in-pac",
        ROOT / "src/hyprial/pac/reactor.py",
        "from .store import PacGraphStore",
        "from hyprial.inbox.service import InboxService  # mutation\nfrom .store import PacGraphStore",
        "tests/test_pac_tripwire.py",
    ),
    (
        "M8-cas-version-check-dropped",
        ROOT / "src/hyprial/pac/graph.py",
        "    if graph[\"version\"] != expect_version:",
        "    if False:",
        "tests/test_pac_graph.py",
    ),
    (
        "M9-projection-connection-writable",
        ROOT / "src/hyprial/pac/store.py",
        '        uri = f"file:{path}?mode=ro"',
        '        uri = f"file:{path}"',
        "tests/test_pac_projection.py",
    ),
    (
        "M10-overdue-pollutes-flag-events",
        ROOT / "src/hyprial/pac/reactor.py",
        "        if not planned:\n            return []",
        "        if not planned:\n            return []\n        pollute = self._store.write()\n        pollute.execute(\"INSERT INTO flag_events (event_id, graph_id, version, node_id, action, actor, at) VALUES (?, ?, 0, ?, 'set', 'clock', 0)\", (planned[0].event_id, graph_id, planned[0].node_id))\n        pollute.commit()",
        "tests/test_pac_clock.py",
    ),
    (
        "M11-turn-text-drops-the-round-suffix",
        ROOT / "src/hyprial/pac/reactor.py",
        '    return f"轮到你(第 {round_no} 次):{node_id} {brief_ref} (event {event_id})"',
        '    return f"轮到你:{node_id} {brief_ref} (event {event_id})"',
        "tests/test_pac_reactor.py tests/test_pac_restate.py",
    ),
]


def run(tests: str) -> str:
    completed = subprocess.run(
        [PY, "-m", "pytest", *tests.split(), "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=300,
    )
    tail = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "NO OUTPUT"
    return tail


def main() -> int:
    receipts: list[str] = []
    all_red = True
    for label, path, old, new, tests in MUTATIONS:
        backup = path.read_text(encoding="utf-8")
        assert old in backup, f"anchor missing for {label}"
        path.write_text(backup.replace(old, new), encoding="utf-8")
        try:
            tail = run(tests)
        finally:
            path.write_text(backup, encoding="utf-8")
        # a mutation is verified when the focused suite is NOT all-green
        fully_green = tail.endswith("passed") or ", 0 failed" in tail or tail.split()[0].isdigit() and "failed" not in tail and "error" not in tail
        status = "RED(caught)" if not fully_green else "GREEN(MISSED!)"
        if fully_green:
            all_red = False
        receipts.append(f"{label}: {status}  [{tail}]")

    # restore absolute cleanliness + confirm the full pac suite is green again
    green = run("tests/test_pac_graph.py tests/test_pac_reactor.py tests/test_pac_clock.py tests/test_pac_projection.py tests/test_pac_restate.py tests/test_pac_tripwire.py tests/test_pac_cli.py")
    for line in receipts:
        print(line)
    print(f"reverted -> pac suite back to: {green}")
    print("ALL MUTATIONS CAUGHT" if all_red else "SOME MUTATIONS MISSED")
    return 0 if all_red and "failed" not in green else 1


if __name__ == "__main__":
    sys.exit(main())
