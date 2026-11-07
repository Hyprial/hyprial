from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]


def test_assertion_failure_still_closes_owned_daemon_application() -> None:
    """An asserted failure stays singular; app cleanup runs before the fd guard."""

    probe_dir = Path(tempfile.mkdtemp(prefix="appclose-probe-", dir=ROOT / "tests"))
    probe = probe_dir / "test_failure.py"
    probe.write_text(
        """from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections


def test_failure(tmp_path: Path, own_daemon_application) -> None:
    app = own_daemon_application(
        DaemonApplication(
            state_dir=tmp_path / "state",
            socket_path=tmp_path / "daemon.sock",
            node_id="appclose-failure-probe",
            owner="owner",
        **_app_injections(),
        )
    )
    assert app.node_id == "wrong-node", "intentional assertion failure"
""",
        encoding="utf-8",
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)

    output = completed.stdout + completed.stderr
    assert completed.returncode == 1, output
    assert "1 failed" in output
    assert "intentional assertion failure" in output
    assert "ERROR at teardown" not in output
    assert "test leaked" not in output
