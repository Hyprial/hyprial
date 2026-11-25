"""Z-6c review F1: the published bearer works as soon as it is published.

``rebind`` writes ``server.json`` while it holds the refresh lock.  A client that
reads the file the moment the new bearer is visible (before the directory fsync
returns) must be served, not refused: its request is stamped after the install
and only waits for the rebind to finish.  If the write fails nothing dispatches:
every waiter is refused and none reaches the new daemon handle.
"""

from __future__ import annotations

import json
import threading
import time

from hyprial.shell.impl.request_http import config as config_module
from hyprial.shell.impl.request_http import server as server_module
from hyprial.shell.impl.request_http.config import server_json_path
from hyprial.shell.impl.request_http.server import RequestHttpServer

from .support import OTHER, OWNER, Gate, PhaseDaemon, get, gui_config, with_owner, write_home


def _pair(tmp_path):
    daemon = PhaseDaemon(tmp_path / "home" / "state" / "daemon.sock", phase="serving", owner=OWNER)
    gate = Gate(daemon, "serving")
    write_home(tmp_path, owner=OWNER)
    server = RequestHttpServer(gui_config(tmp_path, owner=OWNER), request_once=gate)
    server.start()
    new_daemon = PhaseDaemon(daemon.socket_path, phase="serving", owner=OTHER, epoch="daemon-epoch-9")
    return gate, server, Gate(new_daemon, "serving")


def test_a_client_that_reads_the_published_bearer_at_once_is_served(tmp_path, monkeypatch):
    gate, server, new_gate = _pair(tmp_path)
    path = server_json_path(server.config.runtime_dir)
    old = json.loads(path.read_text())["token"]
    original = config_module.fsync_directory
    proceed = threading.Event()

    def held(directory):
        original(directory)
        proceed.wait(15)

    monkeypatch.setattr(config_module, "fsync_directory", held)
    gate.close()
    worker = threading.Thread(
        target=server.rebind,
        args=(with_owner(server.config, owner=OTHER),),
        kwargs={"request_once": new_gate},
    )
    answers: list = []
    try:
        worker.start()
        deadline = time.monotonic() + 15
        published = old
        while published == old and time.monotonic() < deadline:
            try:
                published = json.loads(path.read_text())["token"]
            except (OSError, ValueError):
                pass
            time.sleep(0.002)
        assert published != old
        caller = threading.Thread(target=lambda: answers.append(get(server, "/readyz", token=published)))
        caller.start()
        caller.join(0.5)
        proceed.set()
        caller.join(30)
        worker.join(30)
        assert not caller.is_alive() and not worker.is_alive()
        status, body = answers[0]
        assert status == 200, (status, body.get("code"))
        assert get(server, "/readyz", token=published)[0] == 200
        assert get(server, "/readyz", token=old)[0] == 401
    finally:
        proceed.set()
        worker.join(30)
        server.shutdown()


def test_a_failed_write_refuses_every_waiter_and_dispatches_nothing(tmp_path, monkeypatch):
    gate, server, new_gate = _pair(tmp_path)
    gate.close()
    answers: list = []
    started = threading.Event()
    released = threading.Event()

    def failing(*_args, **_kwargs):
        caller = threading.Thread(target=lambda: answers.append(get(server, "/readyz")))
        caller.start()
        started.set()
        time.sleep(0.2)
        released.set()
        raise OSError(28, "no space left")

    monkeypatch.setattr(server_module, "write_server_json", failing)
    try:
        try:
            server.rebind(with_owner(server.config, owner=OTHER), request_once=new_gate)
        except server_module.StartupRefusal as refusal:
            assert refusal.code == "RUNTIME_UNAVAILABLE"
        else:
            raise AssertionError("the failed write was swallowed")
        assert started.is_set() and released.is_set()
        deadline = time.monotonic() + 30
        while not answers and time.monotonic() < deadline:
            time.sleep(0.01)
        status, body = answers[0]
        assert status == 503 and body["code"] == "DAEMON_UNAVAILABLE", (status, body)
        assert not new_gate.dispatched("conversations.list")
        assert get(server, "/readyz")[0] == 503
    finally:
        server.shutdown()
