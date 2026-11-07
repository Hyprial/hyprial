"""Short test-state roots for daemon and AF_UNIX fixtures."""

from __future__ import annotations

import os


_MACOS_SUN_PATH_LIMIT = 104
_TYPICAL_SOCKET_SUFFIX = "hyprial-test-xxxxxxxx/state/daemon.sock"


def hyprial_test_tmp_root() -> str:
    """Return the CI RAM-disk root, preserving ``/tmp`` locally."""

    root = os.environ.get("HYPRIAL_TEST_TMP_ROOT") or "/tmp"
    socket_path = os.path.join(root, _TYPICAL_SOCKET_SUFFIX)
    assert len(os.fsencode(socket_path)) < _MACOS_SUN_PATH_LIMIT, (
        "HYPRIAL_TEST_TMP_ROOT is too long for a typical macOS AF_UNIX socket: "
        f"{socket_path!r}"
    )
    return root
