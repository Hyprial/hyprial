"""GUI process lifecycle (refactor: session state + child-process primitives).

This package replaces the former ``lifecycle`` module one-for-one: every public
name that used to be importable from ``...backend.lifecycle`` still is, and the
package ``__all__`` matches the old module's public surface.
"""

from __future__ import annotations

from hyprial.kernel import process_birth_identity  # noqa: F401  (compat patch surface)

from ...errors import GuiError  # noqa: F401
from .process import (  # noqa: F401
    PopenFactory,
    Terminator,
    UrlOpen,
    _authorized_urlopen,
    _cleanup_runtime_files,
    _read_captured,
    _redact,
    _run_build,
    _run_owned_build,
    _spawn,
    _spawn_captured,
    _terminate_child,
    _terminate_process,
    _terminate_windows,
    _wait_for_http,
    _wait_for_mock,
    _wait_for_host_ready,
)
from .process import GuiChild  # noqa: F401
from .process import _EOF  # noqa: F401
from .session import (  # noqa: F401
    GuiSession,
    _start_source_session,
    start_session,
)

__all__ = [
    "GuiChild",
    "GuiSession",
    "PopenFactory",
    "Terminator",
    "UrlOpen",
    "start_session",
]
