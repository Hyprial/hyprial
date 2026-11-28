"""An actual Pi reservation refusal crosses lifecycle, registry and AF_UNIX."""

from types import SimpleNamespace

from hyprial.daemon.impl.composition import AgentIdentityProjectionView, CorrelatedDomainEvents, HarnessPortClient
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.harnesses.pi.configuration.test_pi_controlled_loader import _real_p22_context
from tests.daemon.harnesses.pi.connections.test_lifecycle import make_process
from .enumeration import _wire_af_unix


def test_real_pi_busy_reaches_the_start_wire(app, actor_host, tmp_path):
    registry, _, cwd, context, channel, complete = _real_p22_context(tmp_path / "pi")
    spec = HarnessLaunchSpec("pi", "alpha", True, args=("--approve",), cwd=str(cwd))
    incumbent = make_process(spec, channel, complete, actor_host, shared_session="wire-session")
    refused = []

    class Launcher:
        def start(self, selected):
            child = make_process(selected, channel, complete, actor_host, shared_session="wire-session")
            refused.append(child)
            if not child.wait_ready(timeout=20):
                error = child.last_failure
                child.stop()
                raise error
            return child

    def registration(*_):
        return SimpleNamespace(close=lambda: None)

    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(Launcher(), event_sink=events, desired_state=app.desired_state,
                                agent_identity=AgentIdentityProjectionView(app._agent_session_domains))
    harnesses = HarnessPortClient(actor, events)
    app._harnesses = harnesses
    app._inbox = SimpleNamespace(receive=lambda _: None, has_received=lambda _: False, has_fetched=lambda _: False)
    app._presence = SimpleNamespace(actor_online=lambda _: False, is_blocked=lambda _: False)
    app._start_lifecycle_manager(
        transport=SimpleNamespace(declare_liveliness=registration, subscribe=registration, declare_queryable=registration),
        inbox=app._inbox, local_delivery=SimpleNamespace(register_actor=registration), harnesses=harnesses,
    )
    app.agents.create("alpha")
    app.desired_state.upsert_harness(spec)
    try:
        assert incumbent.wait_ready(timeout=20), incumbent.last_error
        response = _wire_af_unix(app, "agent.start", {
            "name": "alpha", "cwd": str(cwd), "approve": True,
        })
        assert "error" in response and "result" not in response, response
        assert response["error"]["code"] == "MODEL_SESSION_BUSY", response
        assert response["error"]["data"] == {"phase": "admission", "reason": "session-reserved"}
        assert refused and all(not child.running for child in refused)
        assert incumbent.running
    finally:
        incumbent.stop()
        for child in refused:
            child.stop()
        app._close()
        actor.close_runtime(15)
        registry.close()
