"""Run with uv; JSONL reports two consecutive cache invalidations per size.

Use PYTHONPATH=<frozen-checkout>/src to measure the frozen implementation with
the same interpreter, workload and dependencies. Every store is disposable.
The historical workload replaces a scalar so payload growth does not confound
the number of cached frontiers. --nested measures growing nested-map payloads.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import tempfile
import time
from pathlib import Path

from pycrdt import Map

from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore


def measure(size, repeat, nested):
    with tempfile.TemporaryDirectory(prefix="orgfs-frontier-") as root:
        store = FileSpaceStore(Path(root), "bench", node_id="bench-owner")
        try:
            store.commit(
                "meta",
                lambda doc: (
                    doc["space"].__setitem__("owner", "user:owner"),
                    doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
                ),
                author="user:owner", actor=None,
            )
            tokens = [store._meta_frontier_for_commit()]
            for index in range(size - 1):
                def change(doc, value=str(index)):
                    if nested:
                        doc["members"]["user:changing"] = Map({"mode": "rw", "value": value})
                    else:
                        doc["space"]["counter"] = value

                store.commit("meta", change, author="user:owner", actor=None)
                tokens.append(store._meta_frontier_for_commit())
            for token in tokens:
                assert store._membership_at(token) is not None
            durations = []
            original = store._membership_at

            def timed(token):
                started = time.perf_counter()
                result = original(token)
                durations.append(time.perf_counter() - started)
                return result

            store._membership_at = timed
            rounds = []
            for suffix in range(2):
                durations.clear()
                started = time.perf_counter()
                store.commit(
                    "meta", lambda doc, value=str(suffix): doc["space"].__setitem__("suffix", value),
                    author="user:owner", actor=None,
                )
                commit_seconds = time.perf_counter() - started
                current_seconds = sum(durations)
                durations.clear()
                started = time.perf_counter()
                for token in tokens:
                    assert store._membership_at(token) is not None
                rounds.append({
                    "commitSeconds": commit_seconds,
                    "currentMembershipSeconds": current_seconds,
                    "allTokenSeconds": time.perf_counter() - started,
                    "cachedRows": store._db.execute("SELECT COUNT(*) FROM meta_frontiers").fetchone()[0],
                })
            print(json.dumps({"M": size, "repeat": repeat, "nested": nested, "rounds": rounds}), flush=True)
        finally:
            store.close()


def journal_digest(store):
    digest = hashlib.sha256()
    for row in store._db.execute("SELECT envelope_bytes FROM commits ORDER BY rowid"):
        digest.update(bytes(row[0]))
    return digest.hexdigest()


def capture(root):
    root.mkdir(parents=True, exist_ok=False)
    store = FileSpaceStore(root / "owner", "equivalence", node_id="owner")
    member = FileSpaceStore(root / "member", "equivalence", node_id="member")
    try:
        birth = store.commit("meta", lambda doc: (
            doc["space"].__setitem__("owner", "user:owner"),
            doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            doc["members"].__setitem__("user:member", Map({"mode": "rw"})),
        ), author="user:owner", actor=None)
        assert member.import_envelope(birth.envelope_bytes, supplier="owner").status == "applied"
        tokens = [store._meta_frontier_for_commit()]
        for index in range(24):
            store.commit("meta", lambda doc, value=str(index): doc["members"].__setitem__(
                "user:changing", Map({"mode": "rw", "value": value})), author="user:owner", actor=None)
            tokens.append(store._meta_frontier_for_commit())
            if index % 4 == 0:
                writer = member._writer_for("user:member", None)
                member.commit("meta", lambda doc: doc["writers"].__setitem__(
                    writer, Map({"author": "user:member", "node": "member", "peerId": member._client_id(writer)})), author="user:member", actor=None)
                for row in member._db.execute("SELECT envelope_bytes FROM commits ORDER BY rowid"):
                    assert store.import_envelope(bytes(row[0]), supplier="member").status == "applied"
        rows = []
        for token in tokens:
            assert store._membership_at(token) is not None
            row = store._db.execute("SELECT membership_json FROM meta_frontiers WHERE token=?", (token,)).fetchone()
            rows.append({"token": base64.b64encode(token).decode(), "membership_json": row[0]})
        result = {"journalSha256": journal_digest(store), "rows": rows}
        (root / "baseline.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"captured": len(rows), "journalSha256": result["journalSha256"]}))
    finally:
        member.close()
        store.close()


def compare(root):
    expected = json.loads((root / "baseline.json").read_text())
    store = FileSpaceStore(root / "owner", "equivalence", node_id="owner")
    try:
        assert journal_digest(store) == expected["journalSha256"]
        rows = []
        for item in expected["rows"]:
            token = base64.b64decode(item["token"])
            assert store._membership_at(token) is not None
            actual = store._db.execute("SELECT membership_json FROM meta_frontiers WHERE token=?", (token,)).fetchone()[0]
            rows.append({**item, "candidate_membership_json": actual, "equal": actual == item["membership_json"]})
        result = {"journalSha256": journal_digest(store), "rows": rows}
        (root / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"equal": sum(row["equal"] for row in rows), "tokens": len(rows), "journalSha256": result["journalSha256"]}))
        assert all(row["equal"] for row in rows)
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nested", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capture", type=Path)
    mode.add_argument("--compare", type=Path)
    args = parser.parse_args()
    if args.capture:
        capture(args.capture)
    elif args.compare:
        compare(args.compare)
    else:
        for repeat in range(2):
            for size in (50, 100, 200):
                measure(size, repeat, args.nested)
