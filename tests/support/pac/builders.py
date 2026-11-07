"""Shared script of the Allen example, as ruled 2026-09-07 (丙: fix the
graph, keep the rules).

One builder, used by the graph/reactor/projection/restate tests so they
all exercise byte-identical structure.  The ruling's shape:

- ``pm``'s two confirmations are TWO nodes — ``pm-confirm-1`` (design
  draft) and ``pm-confirm-2`` (demo) — same owner, distinct briefs;
- ``demo -> pm-confirm-2 -> deploy-help`` are plain forward edges; the
  forward subgraph is a pure DAG (no forward cycle anywhere);
- the two back edges are the ONLY loops, unchanged:
  ``pm-不通过 -> pm-helper`` and ``dev-不通过 -> dev-help``.

Owner values are full principal URIs (the 2026-09-14 write boundary):
people as ``user:<owner>``, agents as ``agent:<owner>:<machine>:<actor>``
with the fixture machine ``test-node``.  The builder registers NO local
agents: the write boundary validates URI shape only and never consults
local knowledge (design-pac-owner-full-uri §1.3), and identity-protocol
tests build real ``AgentRegistry`` records where they need them (§7.2) —
never a hand-built ``agents(actor)`` table.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_edge, add_node, create_graph
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path

TEST_MACHINE = "test-node"

ALLEN_OWNERS = {
    "feat-collector": f"agent:allen:{TEST_MACHINE}:feat-bot",
    "pm-helper": f"agent:allen:{TEST_MACHINE}:pm-helper",
    "pm-confirm-1": "user:pm",
    "dev-help": f"agent:allen:{TEST_MACHINE}:dev-help",
    "dev-confirm": "user:dev",
    "实施": "user:dev",
    "demo": "user:dev",
    "pm-confirm-2": "user:pm",
    "pm-不通过": "user:pm",
    "dev-不通过": "user:dev",
    "deploy-help": f"agent:allen:{TEST_MACHINE}:deploy-help",
    "deploy-confirm": "user:deploy",
    "上线": "user:deploy",
}
ALLEN_FORWARD = [
    ("feat-collector", "pm-helper"),
    ("pm-helper", "pm-confirm-1"),
    ("pm-confirm-1", "dev-help"),
    ("dev-help", "dev-confirm"),
    ("dev-confirm", "实施"),
    ("实施", "demo"),
    ("demo", "pm-confirm-2"),
    ("pm-confirm-2", "deploy-help"),
    ("deploy-help", "deploy-confirm"),
    ("deploy-confirm", "上线"),
]
ALLEN_BACK = [
    ("pm-不通过", "pm-helper"),
    ("dev-不通过", "dev-help"),
]


def principal_uri(short_name: str, *, principal: str = "allen") -> str:
    """The fixture URI for one Allen-cast short name (test-side mapping).

    People map to ``user:<name>``; the four helper/bot agents map to
    ``agent:<principal>:test-node:<name>``.  Tests use this to spell
    expectations without hardcoding the mapping twice.
    """

    agents = {"feat-bot", "pm-helper", "dev-help", "deploy-help"}
    if short_name in agents:
        return f"agent:{principal}:{TEST_MACHINE}:{short_name}"
    return f"user:{short_name}"


def build_allen_graph(state_dir: Path, *, principal: str = "allen") -> tuple[PacGraphStore, str]:
    store = PacGraphStore(default_database_path(state_dir))
    head = create_graph(store, name="allen-example", created_by=f"user:{principal}")
    graph_id = head["graphId"]
    version = head["version"]
    for node_id, owner in ALLEN_OWNERS.items():
        version = add_node(
            store,
            state_dir,
            graph_id=graph_id,
            node_id=node_id,
            owner=owner,
            brief_ref=f"notes/allen-example.md#{node_id}",
            expect_version=version,
        )["version"]
    for source, target in ALLEN_FORWARD + ALLEN_BACK:
        version = add_edge(
            store,
            graph_id=graph_id,
            from_node=source,
            to_node=target,
            kind="back" if (source, target) in ALLEN_BACK else "forward",
            expect_version=version,
        )["version"]
    activate_graph(store, graph_id, actor=f"user:{principal}")
    return store, graph_id
