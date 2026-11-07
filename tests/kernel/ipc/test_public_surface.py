"""The daemon IPC client is exposed through the kernel interface only."""

from __future__ import annotations

import subprocess
import sys

import hyprial.kernel as kernel_module
from hyprial.kernel import interface


def test_daemon_ipc_client_is_public_on_kernel_interface() -> None:
    assert callable(interface.daemon_request)
    assert callable(interface.daemon_request_once)
    assert callable(interface.daemon_socket_path)
    assert {
        "daemon_request",
        "daemon_request_once",
        "daemon_socket_path",
    }.issubset(interface.__all__)


def test_kernel_package_re_exports_the_same_objects() -> None:
    assert kernel_module.daemon_request is interface.daemon_request
    assert kernel_module.daemon_request_once is interface.daemon_request_once
    assert kernel_module.daemon_socket_path is interface.daemon_socket_path


def test_no_temporary_connector_name_is_published() -> None:
    """The one-line shell delegate is gone, so its public alias goes too."""

    assert not hasattr(interface, "connect_daemon_socket")
    assert "connect_daemon_socket" not in interface.__all__


def test_windows_pipe_implementation_is_not_imported_eagerly() -> None:
    """The named-pipe port is resolved at connect time, never at import time."""

    code = (
        "import sys\n"
        "import hyprial.kernel.impl.ipc.client\n"
        "import hyprial.kernel\n"
        "assert 'hyprial.kernel.impl.platform.windows_pipe' not in sys.modules, "
        "'windows_pipe imported eagerly'\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
