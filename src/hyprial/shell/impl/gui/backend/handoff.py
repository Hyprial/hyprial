"""Child environment, loopback URL, browser, and port helpers for the launcher.

Decision port-probe-capability-20261121 (W1C-GUI): ``assert_port_free`` mirrors
the session host's ``_listen`` socket option by capability
(``SO_EXCLUSIVEADDRUSE`` where present, ``SO_REUSEADDR`` otherwise) rather than
branching on ``os.name``.  An exclusive bind's ``EACCES`` is mapped to port
conflict only after a second socket probe confirms an active listener.
"""

from __future__ import annotations

import errno
from pathlib import Path
import socket
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Mapping
from typing import Any

from ..errors import GuiError


def child_output(json_output: bool) -> tuple[Any, Any]:
    if not json_output:
        return None, None
    try:
        sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL, subprocess.DEVNULL
    return sys.stderr, sys.stderr


def child_environment(
    source: Mapping[str, str],
    *,
    gui_home: Path,
) -> dict[str, str]:
    """Return a child-only environment without mutating the parent."""
    environment = dict(source)
    for name in (
        "HYPRIAL_OWNER",
        "HYPRIAL_BACKEND_ENDPOINT",
        "HYPRIAL_GUI_UPSTREAM_TOKEN",
        "HYPRIAL_GUI_LAUNCH_GRANT",
    ):
        environment.pop(name, None)
    environment["HYPRIAL_GUI_HOME"] = str(gui_home)
    return environment


def assert_port_free(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(
                socket.SOL_SOCKET,
                exclusive if exclusive is not None else socket.SO_REUSEADDR,
                1,
            )
            sock.bind((host, port))
    except OSError as error:
        if error.errno == errno.EADDRINUSE or (
            exclusive is not None
            and error.errno == errno.EACCES
            and port > 0
            and _is_exclusive_port_conflict(family, host, port)
        ):
            raise GuiError(
                "GUI_PORT_IN_USE",
                f"GUI port {host}:{port} is already in use; choose another --port",
                {"host": host, "port": port},
            ) from error
        raise GuiError(
            "GUI_PORT_UNAVAILABLE",
            f"cannot bind GUI port {host}:{port}: {error}",
            {"host": host, "port": port},
        ) from error


def _is_exclusive_port_conflict(family: int, host: str, port: int) -> bool:
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            return True
    probe_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.05)
            return probe.connect_ex((probe_host, port)) == 0
    except (AttributeError, OSError):
        return False


def frontend_url(host: str, port: int) -> str:
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in display_host:
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}/"


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def open_browser(url: str, opener: Callable[[str], Any] | None) -> None:
    open_ = opener or webbrowser.open
    try:
        opened = open_(url)
    except Exception as error:
        raise GuiError("GUI_BROWSER_FAILED", "the system browser could not be opened") from error
    if opened is False:
        raise GuiError("GUI_BROWSER_FAILED", "the system browser declined to open the GUI")


__all__ = [
    "assert_port_free",
    "child_environment",
    "child_output",
    "frontend_url",
    "join_url",
    "open_browser",
]
