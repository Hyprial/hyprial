# Decisions (phase D, package F3, 2026-11-25):
# - A real node lost 6.6x of its create rate between 1k and 25k documents
#   (V report §5).  The F3 profile traced it to whole-table work on every
#   write: scans of ``space.nodes`` (doc-id index rebuild, path resolution,
#   same-name check) and copies of the vector table.  This test pins both
#   with counters that do not depend on the machine: the nodes visited
#   through ``space.nodes`` during one write, and the peak bytes Python
#   allocates during it (a table copy allocates in proportion to its size).
# - Built through ``OrgFsRuntime`` with fsync skipped for the fill only;
#   ``ORGFS_SCALING_WRITE_DOCS`` changes the large space (default 10,000).
# - Each operation is measured several times and the median is compared, so
#   an occasional tree compaction (every 500 tree records, design §2.3) or a
#   lane hiccup does not decide the result.  CPU time is printed, not asserted.
"""One write costs the same in a 1,000 and a large document space."""

from __future__ import annotations

import os
from pathlib import Path
import statistics
import time
import tracemalloc
from typing import Any, Callable, Iterator

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime

from ..fsprobe import fsync_disabled

_SMALL = 1000
_SAMPLES = 7


class _CountingNodes(dict):
    """``space.nodes`` that counts the entries every full iteration yields."""

    visited = 0

    def _count(self, iterator: Iterator[Any]) -> Iterator[Any]:
        for item in iterator:
            _CountingNodes.visited += 1
            yield item

    def __iter__(self) -> Iterator[Any]:
        return self._count(super().__iter__())

    def values(self) -> Any:
        return self._count(iter(super().values()))

    def items(self) -> Any:
        return self._count(iter(super().items()))

    def keys(self) -> Any:
        return self._count(iter(super().keys()))


def _space(path: Path, documents: int) -> tuple[OrgFsRuntime, str]:
    runtime = OrgFsRuntime(path, node_id="node-cost", author="user:cost")
    facade = runtime.facade
    space_id = facade.create_space("cost").space_id
    with fsync_disabled():
        for folder in range(100):
            facade.mkdir(space_id, f"d{folder:03d}")
        for index in range(documents):
            facade.write_text(space_id, f"d{index % 100:03d}/doc{index}.md", "x" * 600)
    space = facade._spaces[space_id]  # noqa: SLF001
    space.nodes = _CountingNodes(space.nodes)
    return runtime, space_id


def _operations(runtime: OrgFsRuntime, space_id: str) -> dict[str, Callable[[int], object]]:
    facade = runtime.facade
    return {
        "create": lambda i: facade.write_text(space_id, f"d{i % 100:03d}/new{i}.md", "y" * 600),
        "overwrite": lambda i: facade.write_text(space_id, f"d{i % 100:03d}/doc{i}.md", f"z{i}" * 300),
        "mkdir": lambda i: facade.mkdir(space_id, f"extra{i}"),
        "move": lambda i: facade.move(
            space_id, f"d{(i + 50) % 100:03d}/doc{i + 50}.md", f"d{(i + 51) % 100:03d}/moved{i}.md"
        ),
    }


def _cost(path: Path, documents: int) -> dict[str, tuple[int, int, float]]:
    runtime, space_id = _space(path, documents)
    try:
        result: dict[str, tuple[int, int, float]] = {}
        for name, operation in _operations(runtime, space_id).items():
            visits: list[int] = []
            peaks: list[int] = []
            cpu: list[float] = []
            for sample in range(_SAMPLES):
                _CountingNodes.visited = 0
                tracemalloc.start()
                started = time.process_time()
                with fsync_disabled():
                    operation(sample)
                cpu.append(time.process_time() - started)
                peaks.append(tracemalloc.get_traced_memory()[1])
                tracemalloc.stop()
                visits.append(_CountingNodes.visited)
            result[name] = (
                int(statistics.median(visits)),
                int(statistics.median(peaks)),
                statistics.median(cpu) * 1000,
            )
        return result
    finally:
        runtime.close()


def test_a_write_visits_and_allocates_the_same_in_a_small_and_a_large_space(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "harness-state"))
    large_documents = int(os.environ.get("ORGFS_SCALING_WRITE_DOCS", "10000"))
    small = _cost(tmp_path / "small", _SMALL)
    large = _cost(tmp_path / "large", large_documents)
    for name in small:
        print(
            f"{name}: {_SMALL} docs visits/peak/cpu {small[name][0]}/{small[name][1]}/"
            f"{small[name][2]:.2f}ms; {large_documents} docs {large[name][0]}/"
            f"{large[name][1]}/{large[name][2]:.2f}ms"
        )
    for name in small:
        small_visits, small_peak, _cpu = small[name]
        large_visits, large_peak, _cpu = large[name]
        assert large_visits <= small_visits + 16, (name, small[name], large[name])
        assert large_peak <= 2 * small_peak + 64 * 1024, (name, small[name], large[name])
