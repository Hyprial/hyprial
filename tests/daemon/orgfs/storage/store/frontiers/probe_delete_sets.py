"""Isolated delete-set curve: uv run --offline python <this-file>.

Keep alternating live/deleted cells to fragment delete sets with only 50/100/200
meta commits. Setup defers eager materialization while preserving cache
invalidation, then primes the current token and measures two real commits. Each point
uses fresh synthetic data and no daemon, network or organization credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path


def probe(size, width, repeat):
    with tempfile.TemporaryDirectory(prefix="orgfs-delete-set-") as raw:
        root = Path(raw)
        (root / "home").mkdir()
        os.environ.update(HYPRIAL_HOME=str(root / "home"), HARNESS_STATE_DIR=str(root / "state"), HYPRIAL_NETWORK_ISOLATED="1", HYPRIAL_PEER_DISCOVERY="0")
        os.environ.pop("HARNESS_SOCKET_PATH", None)
        os.environ.pop("HARNESS_CONFIG", None)
        from pycrdt import Map
        from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore

        store = FileSpaceStore(root / "state", "delete-sets", node_id="synthetic")
        try:
            record_frontier = store._record_meta_frontier

            def defer_materialization(_token):
                if hasattr(store, "_refresh_meta_frontiers"):
                    store._meta_frontier_dirty = True
                    store._refresh_meta_frontiers()
                else:
                    rows = store._db.execute("SELECT token FROM meta_frontiers").fetchall()
                    with store._db:
                        store._db.executemany("DELETE FROM meta_frontiers WHERE token = ?", rows)

            store._record_meta_frontier = defer_materialization
            store.commit("meta", lambda doc: (
                doc["space"].__setitem__("owner", "user:owner"),
                doc["members"].__setitem__("user:owner", Map({"mode": "rw"})),
            ), author="user:owner", actor=None)
            for index in range(1, size):
                def churn(doc, value=str(index)):
                    with doc.transaction():
                        for column in range(width):
                            doc["space"][f"keep-{value}-{column}"] = value
                            doc["space"][f"churn-{column}"] = value

                store.commit("meta", churn, author="user:owner", actor=None)
            store._record_meta_frontier = record_frontier
            assert store._meta_history_head()[0] == size
            assert store._membership_at(store._meta_frontier_for_commit()) is not None
            durations = []
            original = store._membership_at

            def timed(token):
                started = time.perf_counter()
                result = original(token)
                durations.append(time.perf_counter() - started)
                return result

            store._membership_at = timed
            rounds = []
            for round_index in (1, 2):
                durations.clear()
                started = time.perf_counter()
                store.commit("meta", lambda doc, value=str(round_index): doc["space"].__setitem__("suffix", value), author="user:owner", actor=None)
                rounds.append({"round": round_index, "commitMs": 1000 * (time.perf_counter() - started), "currentMembershipMs": 1000 * sum(durations)})
            print(json.dumps({"N": size, "width": width, "repeat": repeat, "rounds": rounds}), flush=True)
        finally:
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[50, 100, 200])
    parser.add_argument("--width", type=int, default=64)
    args = parser.parse_args()
    if any(size < 2 for size in args.sizes) or args.width < 1:
        parser.error("N must be at least 2 and width positive")
    for repeat in range(2):
        for size in args.sizes:
            probe(size, args.width, repeat)
