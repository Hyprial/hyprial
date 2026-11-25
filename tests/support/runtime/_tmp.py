"""Short test-state roots for daemon and AF_UNIX fixtures.

Decision (2026-11-23, PR #3106 real-Windows acceptance): Windows has no
``/tmp`` and no AF_UNIX ``sun_path`` limit (local IPC is a named pipe), so the
unset default there is the platform temp directory and the macOS socket-length
assertion applies to POSIX only.
"""

from __future__ import annotations

import os
import tempfile


_MACOS_SUN_PATH_LIMIT = 104
_TYPICAL_SOCKET_SUFFIX = "hyprial-test-xxxxxxxx/state/daemon.sock"


def hyprial_test_tmp_root() -> str:
    """Return the CI RAM-disk root, preserving ``/tmp`` locally."""

    if os.name == "nt":
        return os.environ.get("HYPRIAL_TEST_TMP_ROOT") or tempfile.gettempdir()
    root = os.environ.get("HYPRIAL_TEST_TMP_ROOT") or "/tmp"
    socket_path = os.path.join(root, _TYPICAL_SOCKET_SUFFIX)
    assert len(os.fsencode(socket_path)) < _MACOS_SUN_PATH_LIMIT, (
        "HYPRIAL_TEST_TMP_ROOT is too long for a typical macOS AF_UNIX socket: "
        f"{socket_path!r}"
    )
    return root
