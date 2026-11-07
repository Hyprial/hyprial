"""Offline synthetic frontier probe: uv run --offline python <this-file>.

Each case owns a disposable home, one space, N meta commits and M distinct
owner writers with one cached frontier token per writer. No daemon, network,
credentials or real organization data is used. Defaults: N=100/300/668, M=8.
The arrival interval is an explicit input, not a measurement of another node.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import time
from pathlib import Path


def probe(size: int, writers: int, arrival_seconds: float) -> None:
    with tempfile.TemporaryDirectory(prefix="orgfs-scale-") as raw:
        root = Path(raw)
        home, state = root / "home", root / "state"
        home.mkdir()
        state.mkdir()
        os.environ.update(
            HYPRIAL_HOME=str(home), HARNESS_STATE_DIR=str(state),
            HYPRIAL_NETWORK_ISOLATED="1", HYPRIAL_PEER_DISCOVERY="0",
        )
        os.environ.pop("HARNESS_SOCKET_PATH", None)
        os.environ.pop("HARNESS_CONFIG", None)

        from pycrdt import Map
        from hyprial.daemon.impl.orgfs.storage.store import FileSpaceStore

        store = FileSpaceStore(state, "frontier-scale", node_id="scale-node")
        try:
            store.commit(
                "meta", lambda doc: (
                    doc["space"].__setitem__("owner", "user:scale-owner"),
                    doc["members"].__setitem__("user:scale-owner", Map({"mode": "rw"})),
                ), author="user:scale-owner", actor="agent:scale-0",
            )
            tokens = {0: store._meta_frontier_for_commit()}
            for index in range(1, size):
                lane = index % writers
                store.commit(
                    "meta", lambda doc, value=str(index): doc["space"].__setitem__("counter", value),
                    author="user:scale-owner", actor=f"agent:scale-{lane}",
                )
                tokens[lane] = store._meta_frontier_for_commit()
            actual = store._db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT writer) FROM commits WHERE doc_id = 'meta'"
            ).fetchone()
            assert tuple(actual) == (size, writers), tuple(actual)
            assert len(tokens) == writers
            for token in tokens.values():
                assert store._membership_at(token) is not None
            original = store._membership_at
            durations = []

            def timed(token):
                started = time.perf_counter()
                result = original(token)
                durations.append(time.perf_counter() - started)
                return result

            store._membership_at = timed
            for round_index in (1, 2):
                durations.clear()
                started = time.perf_counter()
                store.commit(
                    "meta", lambda doc, value=str(round_index): doc["space"].__setitem__("suffix", value),
                    author="user:scale-owner", actor="agent:scale-0",
                )
                commit_ms = 1000 * (time.perf_counter() - started)
                recompute_ms = 1000 * sum(durations)
                started_reads = time.perf_counter()
                for token in tokens.values():
                    assert store._membership_at(token) is not None
                reads_ms = 1000 * (time.perf_counter() - started_reads)
                elapsed_ms = commit_ms + reads_ms
                commits = store._db.execute("SELECT COUNT(*) FROM commits WHERE doc_id = 'meta'").fetchone()[0]
                print(
                    f"N={size}, M={writers}, commits={commits}, elapsed_ms={elapsed_ms:.3f}, "
                    f"round={round_index}, current_recompute_ms={recompute_ms:.3f}, "
                    f"commit_ms={commit_ms:.3f}, historical_reads_ms={reads_ms:.3f}, "
                    f"arrival_interval_ms={1000 * arrival_seconds:.3f}, "
                    f"elapsed_over_arrival={elapsed_ms / (1000 * arrival_seconds):.6f}",
                    flush=True,
                )
        finally:
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[100, 300, 668])
    parser.add_argument("--writers", type=int, default=8)
    parser.add_argument("--arrival-seconds", type=float, default=90.0)
    args = parser.parse_args()
    if not args.sizes or any(size < 1 for size in args.sizes):
        parser.error("N must be positive")
    if not 1 <= args.writers <= min(args.sizes):
        parser.error("M must be between 1 and the smallest N")
    if args.arrival_seconds <= 0:
        parser.error("arrival interval must be positive")
    for size in args.sizes:
        probe(size, args.writers, args.arrival_seconds)
