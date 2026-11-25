"""``watch_principal``: the principal watcher over a started server.

Decision: embedded-request-http-listen-false-20261119, B2 of 2822.  The watcher
takes the server, its config, a ``LiveHost`` and a stop event; it needs no signal
handler and no listener.  The server is a listener-free ``RequestHttpServer`` over
the rebind suite's ``PhaseDaemon`` whose own ``on_rebound`` publishes each new
binding; the host is scripted, its ping moving bootstrap to serving.  The timer
that sets ``stop`` after a few seconds is a test-only bound so a regression fails
the assertion instead of hanging the run.  Decision (2822, B4): the foreground GUI serve and its drain-before-close cases are deleted; the embedding host
closes the live host only after the server has drained, which
``tests/shell/gui/host/local`` pins for the real host.
"""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Any

from hyprial.shell.impl.request_http.bootstrap import gui
from hyprial.shell.impl.request_http.server import Rebound, RequestHttpServer
from tests.shell.request_http.launch.support import status_of
from tests.shell.request_http.rebind.support import OWNER, Gate, PhaseDaemon, gui_config, write_home


class ScriptedHost(Gate):
    def __init__(self, daemon: PhaseDaemon, phase: str, home: Path) -> None:
        super().__init__(daemon, phase)
        self.home = home
        self.owner = daemon.owner
        self.daemon_owner = daemon.owner
        self.changed = False
        self.events: list[tuple[str, int]] | None = None
        self.on_poll: Any = None

    def daemon_changed(self) -> bool:
        if self.on_poll is not None:
            self.on_poll()
        return self.changed

    def close(self) -> None:
        if self.events is not None:
            self.events.append(("close", id(self)))
        super().close()


def test_a_bootstrap_to_serving_ping_rebinds_with_a_new_bearer(tmp_path, monkeypatch) -> None:
    home = write_home(tmp_path, owner=OWNER)
    daemon = PhaseDaemon(home / "state" / "daemon.sock", phase="bootstrap")
    first = ScriptedHost(daemon, "bootstrap", home)
    config = gui_config(tmp_path)
    stop = threading.Event()
    seen: list[Rebound] = []

    def on_rebound(value: Rebound) -> None:
        seen.append(value)
        stop.set()

    server = RequestHttpServer(config, request_once=first, listen=False, on_rebound=on_rebound)
    server.start()
    old = server.token
    app = server.asgi_app()
    assert status_of(app, "GET", "/readyz", old)[0] == 200
    daemon.become_serving()
    first.changed = True
    attached: list[ScriptedHost] = []

    def new_host(candidate: Any, **extra: Any) -> ScriptedHost:
        assert extra == {"attach_only": True}
        host = ScriptedHost(daemon, "serving", home)
        attached.append(host)
        return host

    monkeypatch.setattr(gui, "new_host", new_host)
    bound = threading.Timer(5.0, stop.set)
    bound.start()
    live = gui.LiveHost(first)
    try:
        gui.watch_principal(server, config, live, stop)
        assert seen, "the hook did not fire before the test-only bound"
        assert first.closed and live.host is attached[0] and not live.host.closed
        (value,) = seen
        assert value.token == server.token != old
        assert value.owner == OWNER
        assert status_of(app, "GET", "/readyz", old)[0] == 401
        assert status_of(app, "GET", "/readyz", server.token)[0] == 200
    finally:
        bound.cancel()
        server.shutdown()
        for host in (first, *attached):
            host.close()
    assert not (tmp_path / "http").exists()

