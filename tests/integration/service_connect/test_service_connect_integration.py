from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.bootstrap.discovery import ForwardingEndpoints
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.cli.inventory import registered_top_level
from hyprial.shell.impl.cli.runtime import entry
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _daemon_dependencies,
)
from tests.daemon.service_connect.support import FakeController, write_access
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)


class _ClosableFakeController(FakeController):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _IdleRuntime:
    def stop(self) -> None:
        pass


def _application(tmp_path) -> tuple[DaemonApplication, _ClosableFakeController]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state = home / "state"
    state.mkdir()
    controller = _ClosableFakeController()
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="service-connect-integration",
        hyprial_home=home,
        owner="tester",
        forwarding_discovery=ForwardingEndpoints(controller, slots=lambda: ()),
        **_daemon_dependencies(),
    )
    write_access(state / "tailcat" / "service-access.json")
    return app, controller


def _ipc(socket_path, method: str, params: dict[str, object]) -> dict[str, object]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2.0)
    try:
        client.connect(str(socket_path))
        client.sendall(
            json.dumps(
                {"version": 1, "id": method, "method": method, "params": params}
            ).encode()
            + b"\n"
        )
        value = json.loads(client.makefile("rb").readline())
        assert isinstance(value, dict)
        return value
    finally:
        client.close()


def test_router_activates_service_manager_with_real_sqlite(tmp_path) -> None:
    app, controller = _application(tmp_path)
    app._start_service_connect()

    connected = app.handle("service.connect", {"name": "subapi"})

    assert connected["service"]["state"] == "listening"
    assert connected["service"]["localPort"] == 18080
    desired = app.desired_state.load()
    assert [(item.name, item.local_port) for item in desired.service_connections] == [
        ("subapi", 18080)
    ]
    assert [call[0] for call in controller.calls] == ["map"]

    try:
        app.handle("service.connect", {"name": "missing"})
    except DaemonRequestError as error:
        assert error.code == "SERVICE_NOT_REGISTERED"
    else:  # pragma: no cover - the router must translate the domain failure
        raise AssertionError("unknown service unexpectedly connected")


def test_unknown_service_method_uses_normal_router_unknown_method_error(tmp_path) -> None:
    app, _controller = _application(tmp_path)
    app._start_service_connect()
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]

    try:
        app.handle("service.future", {})
    except DaemonRequestError as error:
        assert error.code == "METHOD_NOT_FOUND"
    else:  # pragma: no cover - the router must retain its generic fallback
        raise AssertionError("unknown service method reached the service manager")


def test_real_ipc_socket_routes_service_request_to_manager(tmp_path) -> None:
    app, controller = _application(tmp_path)
    short_socket = Path("/private/tmp") / f"hyprial-sc-{uuid4().hex}.sock"
    app.socket_path = short_socket
    app._runtime = _IdleRuntime()
    app._maintenance_generation = 1
    app._start_service_connect()
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        response = _ipc(app.socket_path, "service.connect", {"name": "subapi"})

        assert response["id"] == "service.connect"
        assert response["result"]["service"]["localPort"] == 18080
        assert [call[0] for call in controller.calls] == ["map"]
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(2.0)
        assert not dispatcher.is_alive()
        short_socket.unlink(missing_ok=True)


def test_real_root_mounts_one_service_group_with_four_commands() -> None:
    names = [command.name for command in registered_top_level(entry.app)]
    assert names.count("service") == 0

    result = CliRunner().invoke(entry.app, ["org", "service"])

    assert result.exit_code == 2, result.stderr
    for command in ("connect", "disconnect", "guide", "list"):
        assert command in result.output
    assert "mode" not in result.output
    assert "installed" not in result.output
