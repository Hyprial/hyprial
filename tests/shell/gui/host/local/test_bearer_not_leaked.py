"""Neither upstream bearer of an in-process host crosses its output, across a rebind.

Decision: host-serves-daemon-in-process-20261119 (2822, B3).  The bearer lives
only in memory: ``server.token`` at start and the new one after a bootstrap to
serving rebind.  What the launcher keeps of the host is its stdout and stderr
(the ready line included), written to ``host.log``; so this test serves the host
in process through the rebind and asserts that neither token occurs in that
output.  The bound timer is the test-only end of a hung run.
"""

from __future__ import annotations

import json
import signal
import threading

from hyprial.shell.impl.gui.host import __main__ as host_main
from hyprial.shell.impl.gui.host.app import HostApp
from hyprial.shell.impl.gui.host.local import LocalDaemon
from tests.shell.gui.host.local.support import world
from tests.shell.gui.host.local.test_serve_local import RUN_BOUND_SECONDS, arguments, launcher_environment
from tests.shell.request_http.rebind.support import OWNER, write_home

__all__ = ["launcher_environment"]


def test_neither_bearer_reaches_the_host_output_across_a_rebind(tmp_path, monkeypatch, capfd) -> None:
    daemon = world(tmp_path, monkeypatch, phase="bootstrap")
    tokens: list[str] = []
    config_for = LocalDaemon._config_for
    publish = LocalDaemon._publish

    def recording_config_for(self, token, owner, actor):
        tokens.append(token)
        return config_for(self, token, owner, actor)

    def publish_then_stop(self, rebound) -> None:
        publish(self, rebound)
        threading.Timer(0, lambda: signal.raise_signal(signal.SIGINT)).start()

    startup = HostApp.startup

    async def serving_after_startup(self: HostApp) -> None:
        await startup(self)
        write_home(tmp_path, owner=OWNER)
        daemon.become_serving()

    monkeypatch.setattr(LocalDaemon, "_config_for", recording_config_for)
    monkeypatch.setattr(LocalDaemon, "_publish", publish_then_stop)
    monkeypatch.setattr(HostApp, "startup", serving_after_startup)
    bound_fired = threading.Event()

    def end_hung_run() -> None:
        bound_fired.set()
        signal.raise_signal(signal.SIGINT)

    bound = threading.Timer(RUN_BOUND_SECONDS, end_hung_run)
    bound.start()
    try:
        code = host_main.serve(arguments(tmp_path))
    finally:
        bound.cancel()
    captured = capfd.readouterr()

    assert not bound_fired.is_set() and code == 0, captured.err
    assert len(tokens) == 2 and tokens[0] != tokens[1] and all(len(token) >= 16 for token in tokens)
    ready = [line for line in captured.out.splitlines() if line.startswith("HYPRIAL_GUI_READY ")]
    assert len(ready) == 1 and json.loads(ready[0].split(" ", 1)[1])["driver"] == "daemon"
    for token in tokens:
        assert token not in captured.out
        assert token not in captured.err
        assert token not in ready[0]
