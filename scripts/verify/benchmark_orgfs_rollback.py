"""Isolated synthetic rollback benchmark; run once per document count.

Uses real CRDT documents and the facade write path, with a commit recorder to
exclude disk latency. Setup is outside timing; tracemalloc measures Python
allocation peaks during one write (not native Yrs allocations). RSS is the
process lifetime high-water mark, including setup. No daemon is contacted.
"""

import argparse
import json
import resource
import sys
import time
import tracemalloc

from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.orgfs.document.model import _ContentDocument


class Recorder:
    def commit(self, doc_id, mutate, *, author, actor):
        mutate(None)

    def commit_many(self, mutations, *, author, actor):
        return ()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("count", type=int)
    parser.add_argument("--bytes", type=int, default=16384)
    args = parser.parse_args()
    facade = LocalOrgFs()
    try:
        info = facade.create_space("benchmark")
        space = facade._spaces[info.space_id]
        payload = ("Task status and evidence: synthetic benchmark.\n" * args.bytes)[:args.bytes]
        for index in range(args.count):
            node = facade._new_node(space, space.nodes["root"], f"note-{index}", "doc")
            space.contents[node.doc_id] = _ContentDocument(payload)
            node.required_content_frontier = space.contents[node.doc_id].doc.get_state()
            space.tree.record(node)
        facade._finish(space, tuple(space.nodes), "created")
        facade.stores = Recorder()
        # Verify that this is the durable branch, not the in-memory shortcut.
        assert facade._store(info.space_id) is facade.stores
        tracemalloc.start()
        start = time.perf_counter()
        facade.write_text(info.space_id, "note-0", payload + "updated\n")
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(json.dumps({"documents": args.count, "document_bytes": args.bytes,
                          "write_seconds": elapsed, "python_peak_bytes": peak,
                          "process_peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024}))
    finally:
        assert facade.close_effects()


if __name__ == "__main__":
    main()
