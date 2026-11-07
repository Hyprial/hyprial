"""A stable tick must not re-project hundreds of unchanged production graphs."""

import json

import pytest

from hyprial.daemon.impl.pac.workflows.runtime import operations

from .support import build_scale, measured_tick


@pytest.mark.parametrize("steps", [1, 12])
def test_stable_tick_has_bounded_sql_work_at_production_scale(tmp_path, monkeypatch, steps):
    service, sender, _, _ = build_scale(tmp_path / "scale", steps=steps)
    try:
        sent = len(sender.sent)
        facts = measured_tick(service, monkeypatch, 2100)
        print(json.dumps({**facts, "productSource": operations.__file__, "stepsPerGraph": steps}), flush=True)
        assert len(sender.sent) == sent
        assert facts['materializedNodeRows'] == 0, facts
        assert facts["vmSteps"] < 100_000, facts
        assert facts["projections"] == [], facts
    finally:
        assert service.close()
