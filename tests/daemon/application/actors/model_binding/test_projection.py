"""Lifecycle and actor projections retain the exact model binding snapshot."""

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.composition.views import harness_launch_projection
from hyprial.daemon.impl.harnesses.actor.contracts import (
    _launch_projection,
    _launch_spec,
)


def test_binding_survives_each_actor_projection_and_payload():
    ref = {"bindingRef": "binding-1", "bindingRevision": 3}
    spec = HarnessLaunchSpec("pi", "worker", True, model_binding=ref)
    for projection in (harness_launch_projection(spec), _launch_projection(spec)):
        restored = _launch_spec(projection)
        assert restored.model_binding == ref
        assert projection.to_payload()["modelBinding"] == ref
        payload = projection.to_payload()
        payload["modelBinding"]["bindingRevision"] = 9
        assert projection.to_payload()["modelBinding"] == ref
