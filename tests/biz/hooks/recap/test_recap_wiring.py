"""The shell composition registers the recap consumer on the H1 seam."""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon import HookConsumersDeps
from hyprial.shell.impl.daemon_wiring import hook_consumers


def test_hook_consumers_factory_registers_the_recap_consumer(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "state" / "recaps.jsonl").write_text(
        json.dumps({"schemaVersion": 1, "event": "turn", "deliveryId": "d-1"})
        + "\n",
        encoding="utf-8",
    )
    deps = HookConsumersDeps(home_for_agent=lambda _actor: home, logger=None)

    consumers = hook_consumers(deps)

    assert set(consumers) == {"recap"}
    consumer = consumers["recap"]
    # The consumer resolves homes through the injected daemon-owned resolver.
    served = consumer.recent_recaps("agent:owner:node:worker", 5)
    assert [record["deliveryId"] for record in served] == ["d-1"]
    assert consumer.close(0.0)
