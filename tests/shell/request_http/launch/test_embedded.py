"""``RequestHttpServer(listen=False)``: the dispatch served in-process, no listener.

Decision: spike-2822-in-process-asgi-20261119, B1 of 2822.  The real ``build_app``
and its middleware are driven by ``httpx.ASGITransport`` (helpers in
``support``).  A listener-free server binds no socket and touches no file: no
runtime directory, no lock, no ``server.json``, no birth observation; a runtime
directory the test seeds with a foreign ``server.json`` and ``server.lock`` keeps
those bytes through start, rebind and shutdown.  Its ``rebind`` swaps the bearer
under the same application object and publishes through ``on_rebound`` only.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any

import pytest

from hyprial.shell.impl.request_http import serve
from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.shell.request_http.launch.support import (
    ACTOR,
    CALLS,
    daemon_pair,
    embedded,
    over_socket,
    status_of,
    tree,
)
from tests.shell.request_http.rebind.support import OWNER, Gate, gui_config, with_owner


def test_start_binds_no_socket_and_writes_no_file(tmp_path, monkeypatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a listener-free server must not bind a socket")

    server, _, _ = embedded(tmp_path)
    before = tree(tmp_path)
    threads = {thread.ident for thread in threading.enumerate()}
    monkeypatch.setattr(socket.socket, "bind", refuse)
    server.start()
    try:
        assert server.asgi_app() is server.asgi_app()
        assert server.token and server._uvicorn is None and server._thread is None
        assert {thread.ident for thread in threading.enumerate()} == threads
        assert tree(tmp_path) == before
        assert not gui_config(tmp_path).runtime_dir.exists()
    finally:
        server.shutdown()
    assert tree(tmp_path) == before


def test_a_seeded_runtime_directory_is_untouched(tmp_path) -> None:
    server, daemon, gate = embedded(tmp_path)
    runtime = gui_config(tmp_path).runtime_dir
    runtime.mkdir(parents=True)
    foreign = {"server.json": b'{"token":"foreign"}', "server.lock": b"foreign-lock"}
    for name, content in foreign.items():
        (runtime / name).write_bytes(content)
    stamps = {name: (runtime / name).stat().st_mtime_ns for name in foreign}
    before = tree(tmp_path)
    server.start()
    gate.close()
    server.rebind(with_owner(server.config, owner=OWNER), request_once=Gate(daemon, "serving"))
    server.shutdown()
    assert tree(tmp_path) == before
    for name, content in foreign.items():
        assert (runtime / name).read_bytes() == content
        assert (runtime / name).stat().st_mtime_ns == stamps[name]


def test_the_answers_are_the_listener_answers(tmp_path) -> None:
    server, _, _ = embedded(tmp_path / "e")
    server.start()
    _, gate = daemon_pair(tmp_path / "r")
    real = RequestHttpServer(gui_config(tmp_path / "r", owner=OWNER), request_once=gate)
    real.start()
    try:
        in_process = [status_of(server.asgi_app(), m, p, server.token, b) for m, p, b in CALLS]
        through_socket = [over_socket(real, m, p, b) for m, p, b in CALLS]
    finally:
        real.shutdown()
        server.shutdown()
    assert in_process == through_socket
    assert [status for status, _ in in_process] == [200, 200, 200]
    assert in_process[2][1]["result"] == {
        "ok": True,
        "participant": ACTOR,
        "conversations": [],
        "nextCursor": None,
        "recordAvailable": True,
    }


def test_without_the_bearer_the_answer_is_401(tmp_path) -> None:
    server, _, _ = embedded(tmp_path)
    server.start()
    try:
        for token in (None, "wrong"):
            for method, path, body in CALLS:
                status, payload = status_of(server.asgi_app(), method, path, token, body)
                assert (status, payload["code"]) == (401, "HTTP_UNAUTHORIZED")
    finally:
        server.shutdown()


def test_rebind_swaps_the_bearer_writes_no_file_and_publishes_through_the_hook(tmp_path) -> None:
    seen: list[Any] = []
    server, daemon, gate = embedded(tmp_path, on_rebound=seen.append)
    server.start()
    before = tree(tmp_path)
    app = server.asgi_app()
    old = server.token
    assert [status for status, _ in (status_of(app, m, p, old, b) for m, p, b in CALLS)] == [200] * 3
    gate.close()
    fresh = Gate(daemon, "serving")
    try:
        server.rebind(with_owner(server.config, owner=OWNER), request_once=fresh)
        assert server.asgi_app() is app
        assert server.token != old
        assert [status for status, _ in (status_of(app, m, p, old, b) for m, p, b in CALLS)] == [401] * 3
        assert [status for status, _ in (status_of(app, m, p, server.token, b) for m, p, b in CALLS)] == [200] * 3
        assert fresh.dispatched("conversations.list")
        assert tree(tmp_path) == before
        assert [tuple(item) for item in seen] == [(server.token, OWNER, ACTOR)]
    finally:
        server.shutdown()
    assert tree(tmp_path) == before


def test_shutdown_stops_serving_and_is_idempotent(tmp_path) -> None:
    server, _, _ = embedded(tmp_path)
    server.start()
    app = server.asgi_app()
    token = server.token
    assert status_of(app, "GET", "/readyz", token)[0] == 200
    server.shutdown()
    server.shutdown()
    assert status_of(app, "GET", "/readyz", token)[0] != 200
    assert status_of(app, "POST", "/daemon/request/conversations.list", token, {})[0] != 200
    assert status_of(app, "GET", "/healthz", None)[0] == 200


def test_a_stopped_server_cannot_be_rebound_open(tmp_path) -> None:
    server, daemon, gate = embedded(tmp_path)
    server.start()
    server.shutdown()
    gate.close()
    with pytest.raises(RuntimeError):
        server.rebind(with_owner(server.config, owner=OWNER), request_once=Gate(daemon, "serving"))
    assert status_of(server.asgi_app(), "GET", "/readyz", server.token)[0] != 200


def test_the_asgi_application_exists_only_after_start(tmp_path) -> None:
    server, _, _ = embedded(tmp_path)
    with pytest.raises(RuntimeError, match="after start"):
        server.asgi_app()
    server.start()
    try:
        assert server.asgi_app() is not None
    finally:
        server.shutdown()


def test_serve_refuses_a_local_gui_config_file(tmp_path, capfd) -> None:
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    document = {
        "schemaVersion": 1,
        "backend": {"kind": "local-daemon", "socket": str(state / "daemon.sock")},
        "principal": {"kind": "local-gui", "owner": None, "actor": None},
        "listen": {"host": "127.0.0.1", "port": 0},
        "runtime": {"dir": str(tmp_path / "http")},
        "home": str(home),
        "stateDir": str(state),
    }
    path = tmp_path / "gui.json"
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream)
    before = tree(tmp_path)

    assert serve(path) == 2

    report = json.loads(capfd.readouterr().err.strip().splitlines()[-1])
    assert report["ok"] is False and report["code"] == "CONFIG_INVALID"
    assert "GUI host" in report["message"]
    assert tree(tmp_path) == before
