"""Doubles for the ensure tests.

Decision: daemon-first-z4-ensure-tests-20261117.  The request client and the
launch primitive are replaced on the explicit service bindings only, and the
home's socket is a path under a private short directory; no daemon, service
unit or home outside the per-test isolated one is involved.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.kernel import RequestPortError, ipc_errors
from hyprial.shell.impl.cli.commands.common import support
from hyprial.shell.impl.cli.commands.common.services import get_services
from tests.support.runtime._tmp import hyprial_test_tmp_root


@pytest.fixture
def ensure_env(monkeypatch):
    services = get_services()
    root = Path(tempfile.mkdtemp(prefix="z4", dir=hyprial_test_tmp_root()))
    env = SimpleNamespace(
        services=services, requests=[], launches=[], script=[], launch_error=None,
        socket=root / "s.sock", listeners=[],
    )

    def fake_request(method, params=None, **_kwargs):
        env.requests.append(method)
        step = env.script.pop(0) if env.script else {"ok": True}
        if isinstance(step, BaseException):
            raise step
        return step

    def fake_launch(**kwargs):
        env.launches.append(kwargs)
        if env.launch_error is not None:
            raise env.launch_error
        return {"started": True}

    def listen(*, stale=False):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(env.socket))
        server.listen(8)
        env.listeners.append(server)
        if stale:
            server.close()

    env.listen = listen
    monkeypatch.setattr(support, "daemon_request", fake_request)
    monkeypatch.setattr(services, "_launch_daemon_process", fake_launch)
    monkeypatch.setattr(services, "_socket_path", lambda: env.socket)
    monkeypatch.setattr(services, "require_initialized_hyprial_home", lambda: None)
    try:
        yield env
    finally:
        for server in env.listeners:
            server.close()
        shutil.rmtree(root, ignore_errors=True)


def unavailable() -> ipc_errors.DaemonUnavailableError:
    return ipc_errors.DaemonUnavailableError("daemon socket is not reachable")


def refusal(code: str, message: str = "refused", data=None) -> RequestPortError:
    return RequestPortError(code, message, data)
