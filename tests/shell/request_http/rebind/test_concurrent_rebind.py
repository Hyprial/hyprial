"""Z-6c review F3: the swap is one step, and a racing request never crosses it.

Through the real HTTP transport (``_require_bearer`` included): a request that is
authenticated with the old bearer and waits on the refresh lock while ``rebind``
runs is refused ``DAEMON_UNAVAILABLE``, it never returns the new view under the
old principal block.  The swap itself is observed on the binding: no binding
ever pairs the old bearer with the new principal.  The hooks are events on the
fake daemon and on the refresh lock, not sleeps.
"""

from __future__ import annotations

import threading
import time

from hyprial.shell.impl.request_http.server import RequestHttpServer

from .support import OTHER, OWNER, Gate, PhaseDaemon, get, gui_config, list_result, with_owner, write_home


class RecordingServer(RequestHttpServer):
    @property
    def _binding(self):
        return self.__dict__["_current"]

    @_binding.setter
    def _binding(self, value):
        self.__dict__["_current"] = value
        self.__dict__.setdefault("history", []).append((value.token, value.config.owner, value.admit_after))
        self.__dict__.setdefault("at", []).append(time.monotonic())
        hook = self.__dict__.get("on_assign")
        if hook is not None:
            hook(value)


class ObservedLock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def __enter__(self):
        if self._lock.locked():
            self.contended.set()
        self._lock.acquire()
        return self

    def __exit__(self, *_exc):
        self._lock.release()


def _setup(tmp_path, cls=RequestHttpServer):
    daemon = PhaseDaemon(tmp_path / "home" / "state" / "daemon.sock", phase="serving", owner=OWNER)
    gate = Gate(daemon, "serving")
    write_home(tmp_path, owner=OWNER)
    server = cls(gui_config(tmp_path, owner=OWNER), request_once=gate)
    server.start()
    new_daemon = PhaseDaemon(daemon.socket_path, phase="serving", owner=OTHER, epoch="daemon-epoch-9")
    return gate, server, new_daemon, Gate(new_daemon, "serving")


def test_no_binding_ever_pairs_the_old_bearer_with_the_new_principal(tmp_path):
    gate, server, _new_daemon, new_gate = _setup(tmp_path, RecordingServer)
    try:
        old = server.token
        gate.close()
        server.rebind(with_owner(server.config, owner=OTHER), request_once=new_gate)
        history = server.history
        assert history[0][:2] == (old, OWNER)
        assert {(token, owner) for token, owner, _ in history} == {(old, OWNER), (server.token, OTHER)}
        assert server.token != old
        assert all(admit == float("-inf") or owner == OTHER for _, owner, admit in history[1:])
        assert history[-1][2] != float("inf")
    finally:
        server.shutdown()


def test_a_request_authenticated_before_the_change_is_refused_not_served_the_new_view(tmp_path):
    gate, server, new_daemon, new_gate = _setup(tmp_path)
    lock = ObservedLock()
    server._refresh_lock = lock
    in_fetch, release = threading.Event(), threading.Event()
    calls = []

    def hold():
        calls.append(1)
        if len(calls) == 1:
            in_fetch.set()
            assert release.wait(30)

    new_daemon.before["daemon.catalog"] = hold
    old = server.token
    answers: list = []
    gate.close()
    rebinding = threading.Thread(
        target=server.rebind,
        args=(with_owner(server.config, owner=OTHER),),
        kwargs={"request_once": new_gate},
    )
    racer = threading.Thread(target=lambda: answers.append(get(server, "/daemon/request", token=old)))
    try:
        rebinding.start()
        assert in_fetch.wait(30)
        racer.start()
        assert lock.contended.wait(30)
        release.set()
        rebinding.join(30)
        racer.join(30)
        assert not rebinding.is_alive() and not racer.is_alive()
        status, body = answers[0]
        assert status == 503 and body["code"] == "DAEMON_UNAVAILABLE", (status, body.get("code"))
        assert "principal" not in body and "backend" not in body
        status, body = get(server, "/daemon/request")
        assert status == 200 and body["principal"]["owner"] == OTHER and body["backend"]["owner"] == OTHER
        assert get(server, "/daemon/request", token=old)[0] == 401
    finally:
        release.set()
        rebinding.join(30)
        racer.join(30)
        server.shutdown()


def test_a_request_stamped_at_the_install_moment_is_refused(tmp_path):
    gate, server, new_daemon, new_gate = _setup(tmp_path, RecordingServer)
    new_daemon.handlers["conversations.list"] = list_result
    try:
        gate.close()
        server.rebind(with_owner(server.config, owner=OTHER), request_once=new_gate)
        installed_at = server.at[1]
        envelope = server._invoke("conversations.list", "http-at-install", installed_at, {}, None)
        assert envelope.status == 503 and envelope.code == "DAEMON_UNAVAILABLE"
        assert not new_gate.dispatched("conversations.list")
        late = server._invoke("conversations.list", "http-after", time.monotonic(), {}, None)
        assert late.status == 200, late.payload
    finally:
        server.shutdown()


def test_the_closed_binding_refuses_a_new_bearer_request_at_once(tmp_path):
    gate, server, _new_daemon, new_gate = _setup(tmp_path, RecordingServer)
    outcome: dict = {}

    def probe(value):
        if value.config.owner != OTHER or outcome:
            return
        caller = threading.Thread(target=lambda: outcome.setdefault("answer", get(server, "/readyz", token=value.token)))
        caller.start()
        caller.join(5)
        outcome["finished"] = not caller.is_alive()

    server.on_assign = probe
    try:
        gate.close()
        server.rebind(with_owner(server.config, owner=OTHER), request_once=new_gate)
        assert outcome["finished"] is True
        status, body = outcome["answer"]
        assert status == 503 and body["code"] == "DAEMON_UNAVAILABLE"
    finally:
        server.on_assign = None
        server.shutdown()


def test_an_old_bearer_never_sees_the_new_principal_over_http(tmp_path):
    gate, server, _new_daemon, new_gate = _setup(tmp_path, RecordingServer)
    old = server.token
    answers: list = []
    callers: list = []

    def at_new_principal(value):
        if value.config.owner != OTHER:
            return
        caller = threading.Thread(target=lambda: answers.append(get(server, "/daemon/request", token=old)))
        caller.start()
        callers.append(caller)

    server.on_assign = at_new_principal
    try:
        gate.close()
        server.rebind(with_owner(server.config, owner=OTHER), request_once=new_gate)
        for caller in callers:
            caller.join(30)
        assert callers and not any(caller.is_alive() for caller in callers)
        leaks = [body for status, body in answers if status == 200]
        assert not leaks, leaks[0].get("principal")
        assert {status for status, _ in answers} <= {401, 503}
        assert get(server, "/daemon/request", token=old)[0] == 401
    finally:
        server.on_assign = None
        server.shutdown()
