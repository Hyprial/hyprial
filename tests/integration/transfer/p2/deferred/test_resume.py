"""RESUME reconstructs only the runtime half; CREATE keeps its negative fence."""
from dataclasses import replace

import pytest

from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation, LifecycleSpec
from hyprial.daemon.impl.lifecycle_manager.steps import _plan, _operation_json, _operation_from_json
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessLaunchProjection
from hyprial.daemon.impl.network.route_ports import RouteSpec


def test_resume_is_the_runtime_mirror_of_deactivate():
    spec = LifecycleSpec("source", "source", HarnessLaunchProjection("pi", "source", True),
                         RouteSpec("source", "live/source", "inbox/source"),
                         expected_entity_token="entity")
    resume = getattr(LifecycleOperation, "resume", None)
    assert callable(resume), "lifecycle has no existing-incarnation resume plan"
    operation = resume("resume", spec)
    assert operation.kind.value == "resume" and operation.source is None
    assert _operation_from_json(_operation_json(operation)) == operation
    forward = _plan(operation)
    assert [s.name for s in forward] == ["agent.bind", "harness.ensure", "route.managed.ensure"]
    reverse = _plan(LifecycleOperation.deactivate("stop", spec))
    assert [(s.domain, s.forward, s.inverse) for s in forward] == [
        (s.domain, s.inverse, s.forward) for s in reversed(reverse)
    ]
    assert all(s.route_owner != "persona:source" for s in forward)
    assert all(s.spec.expected_entity_token == "entity" for s in forward)


@pytest.mark.parametrize("kind", ["create", "resume"])
def test_failed_runtime_never_destroys_preserved_entity(tmp_path, kind):
    from hyprial.daemon.impl.composition import AgentIdentityProjectionView, CorrelatedDomainEvents, HarnessPortClient
    from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
    from tests.integration.actor_preservation.lifecycle.test_actor_preserved_restore_threshold_states import HOUR_MS, _app, _close
    from tests.daemon.application.composition.composition.test_final_composition_lifecycle import _Transport, _LocalDelivery, _Inbox, _Presence
    app, spec = _app(tmp_path, now_ms=2_000_000_000_000, age_ms=HOUR_MS)
    native = []
    class FailingLauncher:
        def start(self, _):
            native.append("start")
            raise RuntimeError("injected native start failure")
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(FailingLauncher(), event_sink=events, desired_state=app.desired_state,
                               agent_identity=AgentIdentityProjectionView(app._agent_session_domains))
    app._harnesses = HarnessPortClient(actor, events)
    app._inbox, app._presence = _Inbox(), _Presence()
    transport = _Transport()
    app._start_lifecycle_manager(transport=transport, inbox=app._inbox,
                                local_delivery=_LocalDelivery(), harnesses=app._harnesses)
    try:
        agent = app.agents.require("worker")
        target = replace(app._lifecycle_spec(spec), expected_entity_token=agent.entity_token)
        factory = getattr(LifecycleOperation, kind, None)
        assert callable(factory), "runtime resume plan missing"
        operation = factory("failed-" + kind, target)
        app._lifecycle_manager.submit(operation)
        result = app._lifecycle_manager.wait(operation.operation_id, 5)
        assert app.agents.require("worker").entity_token == agent.entity_token
        assert not transport.registrations, "resume touched the preserved persona route"
        if kind == "create":
            assert result.error_code == "LIFECYCLE_INCARNATION_UNSUPPORTED"
            assert native == []
        else:
            assert result.state.value == "compensated", result
            assert native and set(native) == {"start"}
            assert result.completed_effects == ("agent.bind",)
            assert result.compensated_effects == ("agent.bind",)
    finally:
        _close(app)
