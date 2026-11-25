"""Real PAC store snapshot through the declared workflow context request."""

from hyprial.daemon.impl.pac.graphs.reactor import NullSender, PacReactor
from tests.support.golden.app import golden_daemon as golden_daemon
from tests.support.pac.builders import build_allen_graph, principal_uri


def test_workflow_node_context_golden(golden_daemon):
    daemon = golden_daemon()
    store, graph_id = build_allen_graph(daemon.app.state_dir, principal="op")
    try:
        golden = daemon.golden("convergence", "workflow-node-context")
        empty = golden.call("workflow.node.context", {"runId": graph_id, "target": "pm-confirm-2"}, label="no-activation")
        assert empty["currentActivation"] is None
        reactor = PacReactor(store, clock=lambda: 100, sender=NullSender())
        for node, owner in (("feat-collector", "feat-bot"), ("pm-helper", "pm-helper"),
                            ("pm-confirm-1", "pm"), ("dev-help", "dev-help"),
                            ("dev-confirm", "dev"), ("实施", "dev"), ("demo", "dev")):
            reactor.set_flag(graph_id, node, actor=principal_uri(owner), reason_ref=f"artifact#{node}")
        active = golden.call("workflow.node.context", {"runId": graph_id, "target": "pm-confirm-2"}, label="requested")
        assert active["currentActivation"]["round"] == 1
        golden.check()
    finally:
        store.close()
