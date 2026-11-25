# Decision record (GUI session host, PR 2b):
# - This is the public face of the host package. It is import-light on purpose:
#   the CLI never imports it at registration time, the launcher starts the host
#   as a child process (python -m hyprial.shell.impl.gui.host serve ...).
# - build_app and serve import their implementation when called, so importing
#   the package costs no uvicorn or httpx import; the imports are explicit
#   statements so the architecture gate can resolve them.
"""Same-origin session host for the GUI: ``/daemon/*`` sessions and built assets."""

from __future__ import annotations

from typing import Any, Sequence

from .config import ConfigError, HostConfig, read_config

__all__ = ["ConfigError", "HostConfig", "build_app", "read_config", "serve"]


def build_app(*args: Any, **kwargs: Any) -> Any:
    """Build the ASGI host application (see ``app.build_app``)."""
    from .app import build_app as build

    return build(*args, **kwargs)


def serve(argv: Sequence[str] | None = None) -> int:
    """Run the host as ``python -m hyprial.shell.impl.gui.host`` does."""
    from .__main__ import serve as run

    return run(argv)
