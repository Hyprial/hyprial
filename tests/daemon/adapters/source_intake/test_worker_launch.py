"""Real worker launch over loopback sockets, without inherited descriptors."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import textwrap

from hyprial.daemon.impl.adapters.worker.process import AdapterWorkerLauncher, WorkerLaunchSpec

ROOT = Path(__file__).resolve().parents[4]


def test_worker_launch_invoke_and_drained_close(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        from hyprial.daemon.impl.adapters.worker.entry import main

        class Driver:
            def start(self):
                pass

            def handle_control(self, control_type, payload):
                return payload

            def drain(self, timeout):
                return True

            def close(self):
                pass

        main(factory=lambda context: Driver())
        """
    )
    launcher = AdapterWorkerLauncher(
        state_dir=tmp_path,
        command=[sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        startup_timeout=5.0,
    )
    process = launcher.start(WorkerLaunchSpec("instance", "fake", 1))
    try:
        assert process.invoke({"value": 42}, timeout=5.0) == {"value": 42}
    finally:
        outcome = process.close(timeout=5.0)
    assert outcome.outcome == "closed"
    assert outcome.drained is True
    assert outcome.returncode == 0
