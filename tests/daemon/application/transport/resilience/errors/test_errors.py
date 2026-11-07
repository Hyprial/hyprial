from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any
import pytest

from ..support import (
    _app,
    _close_application_domains as _close_application_domains,
    short_state_root as short_state_root,
)


def test_internal_request_failure_remains_a_daemon_error_response(
    monkeypatch: pytest.MonkeyPatch, short_state_root: Path
) -> None:
    app = _app(short_state_root)
    logged: list[str] = []

    def fail_handle(_method: str, _params: dict[str, Any], **_kwargs: Any) -> None:
        raise RuntimeError("internal failure sentinel")

    monkeypatch.setattr(app, "handle", fail_handle)
    monkeypatch.setattr(
        app, "_log", lambda _level, _component, event, **_fields: logged.append(event)
    )
    server, client = socket.socketpair()
    try:
        client.sendall(
            b'{"version":1,"id":"failure","method":"ps","params":{}}\n'
        )
        app._serve_client(server)
        response = json.loads(client.makefile("rb").readline())
        assert response["id"] == "failure"
        assert response["error"]["code"] == "DAEMON_ERROR"
        assert response["error"]["message"] == "internal failure sentinel"
        assert logged == []
    finally:
        server.close()
        client.close()
