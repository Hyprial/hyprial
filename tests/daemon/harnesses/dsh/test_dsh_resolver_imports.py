"""A spawned DNS worker must not import the full harness/daemon graph."""

import os
import subprocess
import sys
from pathlib import Path


def test_spawn_entry_point_has_no_runtime_import_side_effects() -> None:
    source = Path(__file__).resolve().parents[4] / 'src'
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from hyprial.kernel.impl.processes._dsh_resolver import resolve_hostname
assert callable(resolve_hostname)
unexpected = sorted(name for name in sys.modules if
    name.startswith(('hyprial.harnesses', 'hyprial.daemon', 'claude_agent_sdk', 'lark_oapi')))
assert not unexpected, unexpected
"""],
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
