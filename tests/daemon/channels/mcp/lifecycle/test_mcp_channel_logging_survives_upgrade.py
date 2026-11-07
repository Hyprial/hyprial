"""The MCP channel's logging must not import anything at log time (E2E-010).

The MCP SDK's ``configure_logging`` installs a rich ``RichHandler``.  Rich
renders lazily: the first record imports ``rich.table`` and friends.  During an
in-place ``hyprial daemon upgrade`` the old channel process keeps running while its
package files are swapped, so that first warning raised
``ModuleNotFoundError: No module named 'rich.table'`` straight out of
``logger.warning`` and killed the channel (nightly E2E-010 upgrade-continuity).
Run in a fresh interpreter so the server's logging setup is the first one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROBE = r"""
import importlib.abc, logging, sys

from hyprial.daemon.impl.mcp.proxy import StatelessDaemonProxy
from hyprial.daemon.impl.mcp.server import create_mcp_server

create_mcp_server(
    StatelessDaemonProxy.__new__(StatelessDaemonProxy),
    actor="agent:op:node:probe",
    session_ref="probe-session",
)


class _SwappedOut(importlib.abc.MetaPathFinder):
    # The upgrade replaced the package on disk: any import not done yet fails.
    def find_spec(self, name, path=None, target=None):
        if name == "rich" or name.startswith("rich."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return None


for name in [name for name in sys.modules if name.startswith("rich.")]:
    if name not in {"rich.console", "rich.logging"}:
        del sys.modules[name]
sys.meta_path.insert(0, _SwappedOut())

logging.getLogger("hyprial.daemon.impl.mcp.channel.loops").warning(
    "channel poll failed: %s", "PROBE_MARKER"
)
print("LOGGED_WITHOUT_IMPORT", flush=True)
"""


@pytest.mark.parametrize("existing_rich_handler", [False, True])
def test_channel_warning_needs_no_import_after_the_package_is_swapped(existing_rich_handler) -> None:
    root = Path(__file__).resolve().parents[5]
    probe = _PROBE
    if existing_rich_handler:
        probe = probe.replace(
            "create_mcp_server(\n",
            "from rich.logging import RichHandler\n"
            "logging.getLogger().addHandler(RichHandler())\n"
            "create_mcp_server(\n",
        )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr[-3000:]
    assert "LOGGED_WITHOUT_IMPORT" in completed.stdout
    # The warning still reaches stderr, as plain text.
    assert "channel poll failed: PROBE_MARKER" in completed.stderr
