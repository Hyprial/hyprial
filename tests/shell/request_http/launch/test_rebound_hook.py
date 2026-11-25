"""``on_rebound``: the one publication hook of ``RequestHttpServer.rebind``.

Decision: embedded-request-http-listen-false-20261119, B1 of 2822.  The hook is
part of the publication, in both modes, inside the ``try`` that closes the binding
on failure: a raising hook leaves neither the old nor the new bearer accepted.  It
runs once per successful rebind with the open copy installed (the spy reads the
server's own binding at that moment, it never calls back), never on a refused or
failed rebind, and receives a ``Rebound``, not the private binding.
"""

from __future__ import annotations

from dataclasses import replace
import json
import math
from typing import Any

import pytest

from hyprial.shell.impl.request_http.server import Rebound, RequestHttpServer, StartupRefusal
from tests.shell.request_http.launch.support import (
    ACTOR,
    daemon_pair,
    embedded,
    over_socket,
    status_of,
)
from tests.shell.request_http.rebind.support import OWNER, Gate, gui_config, with_owner

READ = ("POST", "/daemon/request/conversations.list", {})


def _refused(app: Any, token: str) -> bool:
    return status_of(app, *READ[:2], token, READ[2])[0] != 200


def test_a_raising_hook_closes_the_binding_for_both_bearers(tmp_path) -> None:
    seen: list[Rebound] = []

    def boom(value: Rebound) -> None:
        seen.append(value)
        raise RuntimeError("hook failed")

    server, daemon, gate = embedded(tmp_path, on_rebound=boom)
    server.start()
    app, old = server.asgi_app(), server.token
    assert not _refused(app, old)
    gate.close()
    with pytest.raises(RuntimeError, match="hook failed"):
        server.rebind(with_owner(server.config, owner=OWNER), request_once=Gate(daemon, "serving"))
    try:
        (value,) = seen
        assert value.token == server.token != old
        assert server._binding.admit_after == math.inf
        assert _refused(app, old) and _refused(app, value.token)
    finally:
        server.shutdown()


def test_the_hook_runs_once_with_the_open_copy_installed(tmp_path) -> None:
    moments: list[tuple[float, str, Rebound]] = []
    holder: list[RequestHttpServer] = []

    def spy(value: Rebound) -> None:
        moments.append((holder[0]._binding.admit_after, holder[0]._binding.token, value))

    server, daemon, gate = embedded(tmp_path, on_rebound=spy)
    holder.append(server)
    server.start()
    assert moments == []
    gate.close()
    server.rebind(with_owner(server.config, owner=OWNER), request_once=Gate(daemon, "serving"))
    try:
        (admit_after, token, value), = moments
        assert admit_after != math.inf and token == value.token == server.token
        assert isinstance(value, Rebound)
        assert (value.owner, value.actor) == (OWNER, ACTOR)
    finally:
        server.shutdown()


def test_the_hook_does_not_run_on_a_refused_or_failed_rebind(tmp_path) -> None:
    seen: list[Rebound] = []
    server, daemon, gate = embedded(tmp_path, on_rebound=seen.append)
    server.start()
    old = server.token
    try:
        moved = replace(with_owner(server.config, owner=OWNER), runtime_dir=tmp_path / "elsewhere")
        with pytest.raises(StartupRefusal):
            server.rebind(moved, request_once=Gate(daemon, "serving"))
        gate.close()
        with pytest.raises(StartupRefusal):
            server.rebind(with_owner(server.config, owner=OWNER), request_once=gate)
        assert seen == []
        assert server.token == old
    finally:
        server.shutdown()


def test_listener_mode_fires_the_hook_once_per_rebind_and_writes_the_file(tmp_path) -> None:
    seen: list[Rebound] = []
    daemon, gate = daemon_pair(tmp_path)
    server = RequestHttpServer(gui_config(tmp_path, owner=OWNER), request_once=gate, on_rebound=seen.append)
    server.start()
    try:
        gate.close()
        server.rebind(with_owner(server.config, owner=OWNER), request_once=Gate(daemon, "serving"))
        assert [value.token for value in seen] == [server.token]
        published = json.loads((server.config.runtime_dir / "server.json").read_text())
        assert published["token"] == server.token
        assert over_socket(server, *READ[:2], READ[2])[0] == 200
    finally:
        server.shutdown()


def test_listener_mode_raising_hook_closes_the_binding(tmp_path) -> None:
    def boom(value: Rebound) -> None:
        raise RuntimeError("hook failed")

    daemon, gate = daemon_pair(tmp_path)
    server = RequestHttpServer(gui_config(tmp_path, owner=OWNER), request_once=gate, on_rebound=boom)
    server.start()
    old = server.token
    try:
        gate.close()
        with pytest.raises(RuntimeError, match="hook failed"):
            server.rebind(with_owner(server.config, owner=OWNER), request_once=Gate(daemon, "serving"))
        assert over_socket(server, *READ[:2], READ[2], token=old)[0] != 200
        assert over_socket(server, *READ[:2], READ[2], token=server.token)[0] != 200
    finally:
        server.shutdown()
