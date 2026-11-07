"""Headline acceptance #1: the whole flow restates from ``flag_events`` alone.

Concept §5: "全部由 flag_events 表能复述整个流程".  The test feeds the
recorded event stream of the Allen run (happy path + one withdraw + one
pm-不通过 back edge) to the pure fold and asserts the restatement equals
the expected flow — and that the live ``notifications`` table the reactor
wrote is exactly what the fold derives (no drift between the two).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.pac.graphs.projection import Projection, restate
from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from tests.support.pac.builders import build_allen_graph, principal_uri

# The slice-1 flow script (ruling 丙): set/reset/back in execution order —
# happy path, one withdraw leg, one pm-不通过 back edge, and the redo
# cascade it honestly causes (each downstream owner resets on 「已撤回」
# and re-confirms on the re-turn).
SCRIPT = [
    ("feat-collector", "feat-bot", "set"),
    ("feat-collector", "feat-bot", "reset"),
    ("feat-collector", "feat-bot", "set"),
    ("pm-helper", "pm-helper", "set"),
    ("pm-confirm-1", "pm", "set"),
    ("dev-help", "dev-help", "set"),
    ("dev-confirm", "dev", "set"),
    ("实施", "dev", "set"),
    ("demo", "dev", "set"),
    ("pm-不通过", "pm", "set"),
    ("pm-helper", "pm-helper", "reset"),
    ("pm-helper", "pm-helper", "set"),
    ("pm-confirm-1", "pm", "reset"),
    ("pm-confirm-1", "pm", "set"),
    ("dev-help", "dev-help", "reset"),
    ("dev-help", "dev-help", "set"),
    ("dev-confirm", "dev", "reset"),
    ("dev-confirm", "dev", "set"),
    ("实施", "dev", "reset"),
    ("实施", "dev", "set"),
    ("demo", "dev", "reset"),
    ("demo", "dev", "set"),
    ("pm-confirm-2", "pm", "set"),
    ("deploy-help", "deploy-help", "set"),
    ("deploy-confirm", "deploy", "set"),
    ("上线", "deploy", "set"),
]

#: The expected restatement, one line per step: event + what it caused.
#: This is the flow the spec's ①–⑤ walk through (ruling 丙 shape), pinned
#: textually — and mirrored byte-for-byte by E2E-018's own copy so the
#: unit gate and the wire gate cannot drift apart.
def _flow_line(action: str, node: str, actor: str, notify: tuple[str, str] | None = None) -> str:
    line = f"{action} {node} by {principal_uri(actor)}"
    if notify is not None:
        recipient, text = notify
        line += f" => notify {principal_uri(recipient)} {text}"
    return line


#: The expected restatement, one line per step: event + what it caused.
#: This is the flow the spec's ①–⑤ walk through (ruling 丙 shape), pinned
#: textually — and mirrored value-for-value by E2E-018's own copy so the
#: unit gate and the wire gate cannot drift apart.  Actor/recipient fields
#: are full principal URIs since the 2026-09-14 write boundary.
EXPECTED_FLOW = [
    _flow_line("set", "feat-collector", "feat-bot", ("pm-helper", "轮到你:pm-helper notes/allen-example.md#pm-helper")),
    _flow_line("reset", "feat-collector", "feat-bot", ("pm-helper", "已撤回:pm-helper notes/allen-example.md#pm-helper")),
    _flow_line("set", "feat-collector", "feat-bot", ("pm-helper", "轮到你:pm-helper notes/allen-example.md#pm-helper")),
    _flow_line("set", "pm-helper", "pm-helper", ("pm", "轮到你:pm-confirm-1 notes/allen-example.md#pm-confirm-1")),
    _flow_line("set", "pm-confirm-1", "pm", ("dev-help", "轮到你:dev-help notes/allen-example.md#dev-help")),
    _flow_line("set", "dev-help", "dev-help", ("dev", "轮到你:dev-confirm notes/allen-example.md#dev-confirm")),
    _flow_line("set", "dev-confirm", "dev", ("dev", "轮到你:实施 notes/allen-example.md#实施")),
    _flow_line("set", "实施", "dev", ("dev", "轮到你:demo notes/allen-example.md#demo")),
    _flow_line("set", "demo", "dev", ("pm", "轮到你:pm-confirm-2 notes/allen-example.md#pm-confirm-2")),
    _flow_line("set", "pm-不通过", "pm", ("pm-helper", "轮到你(第 2 次):pm-helper notes/allen-example.md#pm-helper")),
    _flow_line("reset", "pm-helper", "pm-helper", ("pm", "已撤回:pm-confirm-1 notes/allen-example.md#pm-confirm-1")),
    _flow_line("set", "pm-helper", "pm-helper", ("pm", "轮到你(第 2 次):pm-confirm-1 notes/allen-example.md#pm-confirm-1")),
    _flow_line("reset", "pm-confirm-1", "pm", ("dev-help", "已撤回:dev-help notes/allen-example.md#dev-help")),
    _flow_line("set", "pm-confirm-1", "pm", ("dev-help", "轮到你(第 2 次):dev-help notes/allen-example.md#dev-help")),
    _flow_line("reset", "dev-help", "dev-help", ("dev", "已撤回:dev-confirm notes/allen-example.md#dev-confirm")),
    _flow_line("set", "dev-help", "dev-help", ("dev", "轮到你(第 2 次):dev-confirm notes/allen-example.md#dev-confirm")),
    _flow_line("reset", "dev-confirm", "dev", ("dev", "已撤回:实施 notes/allen-example.md#实施")),
    _flow_line("set", "dev-confirm", "dev", ("dev", "轮到你(第 2 次):实施 notes/allen-example.md#实施")),
    _flow_line("reset", "实施", "dev", ("dev", "已撤回:demo notes/allen-example.md#demo")),
    _flow_line("set", "实施", "dev", ("dev", "轮到你(第 2 次):demo notes/allen-example.md#demo")),
    _flow_line("reset", "demo", "dev", ("pm", "已撤回:pm-confirm-2 notes/allen-example.md#pm-confirm-2")),
    _flow_line("set", "demo", "dev", ("pm", "轮到你:pm-confirm-2 notes/allen-example.md#pm-confirm-2")),
    _flow_line("set", "pm-confirm-2", "pm", ("deploy-help", "轮到你:deploy-help notes/allen-example.md#deploy-help")),
    _flow_line("set", "deploy-help", "deploy-help", ("deploy", "轮到你:deploy-confirm notes/allen-example.md#deploy-confirm")),
    _flow_line("set", "deploy-confirm", "deploy", ("deploy", "轮到你:上线 notes/allen-example.md#上线")),
    _flow_line("set", "上线", "deploy"),  # terminal: nothing downstream
]


@pytest.fixture()
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    store, graph_id = build_allen_graph(state)
    reactor = PacReactor(store, sender=NullSender())
    for node_id, actor, action in SCRIPT:
        if action == "set":
            reactor.set_flag(graph_id, node_id, actor=principal_uri(actor))
        else:
            reactor.reset_flag(graph_id, node_id, actor=principal_uri(actor))
    projection = Projection(store.path)
    yield store, graph_id, projection
    store.close()


def _narrative(document: dict) -> list[str]:
    lines = []
    for step in document["steps"]:
        caused = "; ".join(
            f"notify {item['recipient']} {item['text'].rsplit(' (event ', 1)[0]}"
            for item in step["caused"]
        )
        lines.append(
            f"{step['action']} {step['node']} by {step['actor']}"
            + (f" => {caused}" if caused else "")
        )
    return lines


def test_flag_events_alone_restate_the_expected_flow(recorded) -> None:
    store, graph_id, projection = recorded
    document = projection.snapshot(graph_id)
    assert _narrative(document) == EXPECTED_FLOW


def test_restatement_uses_only_the_event_stream(recorded) -> None:
    """Re-feed ONLY the flag_events rows (structure being static context)
    into the pure fold — same answer as the live snapshot."""

    store, graph_id, projection = recorded
    from hyprial.daemon.impl.pac.graphs.projection import _events_since, _load_structure
    from hyprial.daemon.impl.pac.storage.store import connect

    connection = connect(store.path, read_only=True)
    try:
        structure = _load_structure(connection, graph_id)
        events = _events_since(connection, graph_id, 0)
    finally:
        connection.close()
    assert len(events) == len(SCRIPT)
    replayed = restate(structure, events)
    assert _narrative(replayed) == EXPECTED_FLOW


def test_live_notifications_table_equals_the_fold(recorded) -> None:
    """The reactor's durable rows and the pure fold agree on every turn
    and withdraw — (event_id, edge, kind, recipient, round) for row."""

    store, graph_id, projection = recorded
    document = projection.snapshot(graph_id)
    folded = {
        (item["eventId"], item["edge"]): (
            item["kind"],
            item["recipient"],
            item.get("round"),
        )
        for item in [*document["turns"], *document["withdraws"]]
    }
    live = {
        (row.event_id, row.edge): (row.kind, row.recipient, row.round_no)
        for row in store.notifications(graph_id)
    }
    assert folded == live, "the recorded notifications ARE the fold of flag_events"
