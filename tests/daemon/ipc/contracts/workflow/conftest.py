"""Generic request-socket fixture for workflow context contract tests."""

from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

import pytest

from tests.daemon.ipc.contracts.agent.test_relations_specs import app as app
from tests.integration.service_connect.test_service_connect_integration import _IdleRuntime


@pytest.fixture()
def plan_socket(app):
    app.socket_path = Path("/tmp") / f"workflow-context-{uuid4().hex}.sock"
    app._runtime = _IdleRuntime()
    app._maintenance_generation = 1
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        yield app
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(2.0)
        app.socket_path.unlink(missing_ok=True)
        assert not dispatcher.is_alive()
