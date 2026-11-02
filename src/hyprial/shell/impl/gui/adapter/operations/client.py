"""Production daemon port shared by the adapter host's named operations."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hyprial.kernel import DaemonRequestError


def _request(method: str, params: dict, *, once: bool) -> Any:
    """Use the public kernel port without importing the CLI composition root."""
    from hyprial.kernel import (
        RequestPortError, configured_harness_state_dir, daemon_socket_path,
        daemon_request as request, daemon_request_once as request_once,
    )

    def endpoint() -> Path:
        return daemon_socket_path(configured_harness_state_dir, env=os.environ)

    try:
        if once:
            return request_once(method, params, socket_path=endpoint())
        return request(method, params, socket_path=endpoint)
    except RequestPortError as error:
        raise DaemonRequestError(error.code, str(error), error.data) from error


def daemon_request(method: str, params: dict) -> Any:
    """Public shell port for a named daemon operation with restore handling."""
    return _request(method, params, once=False)


def daemon_request_once(method: str, params: dict) -> Any:
    """Public shell port for a single admission attempt without replay."""
    return _request(method, params, once=True)


def daemon_request_callable() -> Callable[[str, dict], Any]:
    """Compatibility port for the existing GUI adapter host."""
    return daemon_request
