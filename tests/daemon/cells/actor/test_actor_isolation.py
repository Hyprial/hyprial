from __future__ import annotations

from tests.daemon.cells.actor.test_registry import Runtime, make
from tests.daemon.cells.actor.actor_support import agent_cell, agent_id


def test_one_cell_health_read_does_not_use_orgfs_or_block_another_cell() -> None:
    first = agent_cell(agent_id("first"))
    second = agent_cell(agent_id("second"))
    runtime = Runtime(first)
    registry, host, _runtime = make(first, runtime=runtime)
    registry.on_index({first.cell_id, second.cell_id})
    assert host.spawn_count == 2
    assert registry.snapshot(first.cell_id) is not None
    assert registry.snapshot(second.cell_id) is not None
    registry.detach()
