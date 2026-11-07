"""Who a PAC notification comes from (Allen, Feishu DM, 2026-09-25).

«pac owner和节点之间消息的来源应该分开 … 当查询pac归属于谁的时候，应该显示agent-1，
当worker-2查询是谁handoff工作的时候，显示worker-1。代码中写死owner或者创建者是不可接受的行为»
and «没有actor触发的通知应该回归pac创建者».

So: a root node's request comes from the graph's creator; a handoff comes
from the principal whose completion made the node ready; a back-edge rework
comes from whoever set the rejecting node.  Ownership stays the creator's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

CREATOR = "user:test"
W1 = "agent:test:node:worker-1"
W2 = "agent:test:node:worker-2"
PM = "agent:test:node:pm"
CLOSER = "agent:test:node:closer"


class _Sender:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, **kwargs: Any) -> str:
        self.sent.append(kwargs)
        return kwargs["idempotency_key"]


@pytest.fixture
def service(tmp_path: Path):
    sender = _Sender()
    svc = GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="node",
        sender=sender,
        start_thread=False,
    )
    yield svc, sender
    assert svc.close()


def _senders_to(sender: _Sender, recipient: str) -> list[str]:
    return [item["sender"] for item in sender.sent if item["recipient"] == recipient]


def _complete(svc: GraphWorkflowService, graph: str, node_id: str, actor: str) -> None:
    node = next(n for n in svc.status(run_id=graph)["nodes"] if n["nodeId"] == node_id)
    store = PacGraphStore(svc.database)
    try:
        PacReactor(store, clock=svc.clock).set_flag(
            graph, node_id, actor=actor, expected_request=node["requestId"], reason_ref="done"
        )
    finally:
        store.close()
    svc._tick()


def test_root_from_creator_handoff_from_the_upstream_worker_rework_from_the_rejecter(
    service,
) -> None:
    svc, sender = service
    raw = yaml.safe_dump(
        {
            "version": 2,
            "name": "handoff",
            "on_failure": "hold",
            "nodes": [
                {"id": "draft", "task": "draft", "owner": W1},
                {"id": "build", "task": "build", "owner": W2, "after": ["draft"]},
                {"id": "reject", "task": "reject", "owner": PM},
                {"id": "close", "kind": "end", "task": "close", "owner": CLOSER, "after": ["build"]},
            ],
            "edges": [{"from": "reject", "to": "draft", "kind": "back"}],
        }
    )
    graph = svc.start(yaml_text=raw, sender=CREATOR, operation_key="handoff")["graphId"]
    svc._tick()

    # Roots: nothing any principal did caused them -> the creator.
    assert _senders_to(sender, W1) == [CREATOR]
    assert _senders_to(sender, PM) == [CREATOR]

    # worker-1 completes draft -> worker-2's handoff comes from worker-1.
    _complete(svc, graph, "draft", W1)
    assert _senders_to(sender, W2) == [W1]

    # pm sets the rejecting node -> draft's rework comes from pm.
    _complete(svc, graph, "reject", PM)
    assert _senders_to(sender, W1)[-1] == PM

    # Ownership is still the creator's; only the handoff source moved.
    assert svc.status(run_id=graph)["sender"] == CREATOR
