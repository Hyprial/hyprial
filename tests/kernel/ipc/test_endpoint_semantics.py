"""Semantics the relocation must preserve, and the old layer it must remove.

Two behaviours were flagged by review as regressions when the client moved out
of ``shell/impl/cli``:

A. An authoritative ``HARNESS_SOCKET_PATH`` is honoured **without evaluating
   the state root at all**.  The base implementation never called
   ``_state_dir()`` on that path, and callers deliberately make it unavailable
   (isolation roots).
B. A transport ``OSError`` that is neither a peer-gone condition nor a timeout
   is re-raised unchanged.  Only EPIPE/ECONNRESET become
   ``DaemonDisconnectedError``.

The second half of this file pins the *cleanup*: the shell keeps only the
CLI request port (endpoint/clock/id injection plus the user-facing error
translation); framing, connect/backlog patience, budgets, and errno rules are
kernel-owned, and the transport names are gone from the shell module surface.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import hyprial.kernel as kernel_module
import hyprial.kernel.impl.ipc.client as client_module
from hyprial.kernel.impl.contracts.daemon import ipc_errors
from hyprial.kernel.impl.contracts.ports import RequestPortError
from hyprial.kernel.impl.ipc.endpoint import daemon_socket_path
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services


def _explode() -> Path:
    raise RuntimeError("STATE_DIR_SHOULD_NOT_BE_TOUCHED")


def test_explicit_socket_path_never_evaluates_the_state_root() -> None:
    env = {"HARNESS_SOCKET_PATH": "/private/tmp/explicit-review.sock"}

    assert daemon_socket_path(_explode, env=env) == Path(
        "/private/tmp/explicit-review.sock"
    )


def test_state_root_wins_when_it_is_set() -> None:
    env = {
        "HARNESS_SOCKET_PATH": "/private/tmp/ignored.sock",
        "HARNESS_STATE_DIR": "/private/tmp/state-root",
    }

    assert daemon_socket_path(lambda: Path("/private/tmp/state-root"), env=env) == Path(
        "/private/tmp/state-root/daemon.sock"
    )


def test_state_root_provider_is_only_called_when_needed() -> None:
    calls: list[str] = []

    def state_dir() -> Path:
        calls.append("resolved")
        return Path("/private/tmp/state-root")

    assert daemon_socket_path(state_dir, env={}) == Path(
        "/private/tmp/state-root/daemon.sock"
    )
    assert calls == ["resolved"]

    calls.clear()
    assert daemon_socket_path(state_dir, env={"HARNESS_SOCKET_PATH": "/private/tmp/x.sock"}) == Path(
        "/private/tmp/x.sock"
    )
    assert calls == []


class _FailingSocket:
    def __init__(self, errno_value: int, *, fail_at: str = "send") -> None:
        self.errno_value = errno_value
        self.fail_at = fail_at
        self.closed = False

    def sendall(self, _data: bytes) -> None:
        if self.fail_at == "send":
            raise OSError(self.errno_value, "io fault")

    def recv(self, _size: int) -> bytes:
        if self.fail_at == "recv":
            raise OSError(self.errno_value, "io fault")
        return b'{"version":1,"id":"x","result":{}}\n'

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("errno_value", "expect_disconnect"),
    [
        (errno.EIO, False),  # re-raised unchanged, exactly like the base
        (errno.EPIPE, True),  # peer gone, classed as a disconnect
        (errno.ECONNRESET, True),
    ],
)
def test_transport_errnos_keep_their_base_classification(
    monkeypatch: pytest.MonkeyPatch, errno_value: int, expect_disconnect: bool
) -> None:
    failing = _FailingSocket(errno_value)
    monkeypatch.setattr(
        client_module, "_connect_daemon_socket", lambda *_a, **_k: failing
    )

    try:
        client_module.daemon_request_once(
            "ping", {}, socket_path=Path("/private/tmp/never-connected.sock")
        )
    except BaseException as raised:  # noqa: BLE001 - the type IS the assertion
        observed = type(raised).__name__
        is_oserror = isinstance(raised, OSError)
        errno_seen = getattr(raised, "errno", None)
    else:  # pragma: no cover - the fake always fails
        raise AssertionError("daemon_request_once returned instead of failing")

    if expect_disconnect:
        assert observed == "DaemonDisconnectedError", (
            f"errno {errno_value} must become DaemonDisconnectedError, got {observed}"
        )
    else:
        assert observed == "OSError" and is_oserror, (
            f"errno {errno_value} must be re-raised unchanged, got {observed}"
        )
        assert errno_seen == errno_value, (errno_seen, errno_value)
    assert failing.closed is True


@pytest.mark.parametrize("errno_value", [errno.EPIPE, errno.ECONNRESET])
def test_peer_gone_at_read_time_becomes_a_disconnect(
    monkeypatch: pytest.MonkeyPatch, errno_value: int
) -> None:
    failing = _FailingSocket(errno_value, fail_at="recv")
    monkeypatch.setattr(
        client_module, "_connect_daemon_socket", lambda *_a, **_k: failing
    )

    with pytest.raises(ipc_errors.DaemonDisconnectedError) as caught:
        client_module.daemon_request_once(
            "ping", {}, socket_path=Path("/private/tmp/never-connected.sock")
        )

    assert caught.value.__cause__ is not None
    assert caught.value.__cause__.errno == errno_value  # type: ignore[attr-defined]


def test_restore_wait_retries_reresolve_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon that moves its socket during DAEMON_RESTORING must be followed.

    The base client re-resolved the endpoint on every retry; a frozen path would
    keep knocking on the socket that no longer answers.
    """

    attempts: list[str] = []
    paths = iter(["/tmp/sock-1.sock", "/tmp/sock-2.sock", "/tmp/sock-3.sock"])

    def fake_once(_method, _params, *, socket_path, **_kwargs):  # type: ignore[no-untyped-def]
        attempts.append(str(socket_path))
        if len(attempts) < 3:
            raise ipc_errors.DaemonRestoringError("DAEMON_RESTORING", "restoring")
        return {"ok": True}

    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    monkeypatch.setattr(client_module, "daemon_request_once", fake_once)

    assert client_module.daemon_request(
        "ping",
        {},
        socket_path=lambda: Path(next(paths)),
        clock=_Clock(),
        restore_wait=10.0,
    ) == {"ok": True}

    assert attempts == ["/tmp/sock-1.sock", "/tmp/sock-2.sock", "/tmp/sock-3.sock"]


def test_restore_wait_retries_reresolve_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each retry uses a freshly resolved clock binding, not the first one."""

    class _Clock:
        def __init__(self, name: str) -> None:
            self.name = name
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clocks = [_Clock(f"clock-{index}") for index in range(4)]
    resolved: list[_Clock] = []
    attempt_clocks: list[_Clock] = []

    def resolver() -> _Clock:
        clock = clocks[len(resolved)]
        resolved.append(clock)
        return clock

    def fake_once(_method, _params, *, socket_path, clock, **_kwargs):  # type: ignore[no-untyped-def]
        attempt_clocks.append(clock)
        if len(attempt_clocks) < 3:
            raise ipc_errors.DaemonRestoringError("DAEMON_RESTORING", "restoring")
        return {"ok": True}

    monkeypatch.setattr(client_module, "daemon_request_once", fake_once)

    assert client_module.daemon_request(
        "ping",
        {},
        socket_path=Path("/tmp/never.sock"),
        clock=resolver,
        restore_wait=10.0,
    ) == {"ok": True}

    # One resolution anchors the restore deadline; every attempt resolves its
    # own binding, so a moved daemon or a swapped service is followed.
    assert resolved[0] is clocks[0]
    assert attempt_clocks == clocks[1:4]
    assert len({id(clock) for clock in attempt_clocks}) == 3


def test_restore_wait_zero_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_once(method, _params, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(method)
        raise ipc_errors.DaemonRestoringError("DAEMON_RESTORING", "restoring")

    monkeypatch.setattr(client_module, "daemon_request_once", fake_once)

    with pytest.raises(ipc_errors.DaemonRestoringError):
        client_module.daemon_request(
            "message.send",
            {},
            socket_path=Path("/tmp/never.sock"),
            restore_wait=0.0,
        )

    assert calls == ["message.send"]


def test_restore_wait_budget_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    calls: list[str] = []

    def fake_once(method, _params, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(method)
        raise ipc_errors.DaemonRestoringError("DAEMON_RESTORING", "restoring")

    monkeypatch.setattr(client_module, "daemon_request_once", fake_once)

    with pytest.raises(ipc_errors.DaemonRestoringError):
        client_module.daemon_request(
            "message.send",
            {},
            socket_path=Path("/tmp/never.sock"),
            clock=_Clock(),
            restore_wait=0.5,
        )

    # Deadline 0.5s, poll 0.25s: attempts at 0.0, 0.25 and 0.5, then the
    # refusal surfaces at the deadline instead of retrying forever.
    assert calls == ["message.send", "message.send", "message.send"]


class _ScriptedSocket:
    """A socket double that returns one scripted payload then reports EOF."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False

    def sendall(self, _data: bytes) -> None:
        return None

    def recv(self, _size: int) -> bytes:
        payload, self.payload = self.payload, b""
        return payload

    def close(self) -> None:
        self.closed = True


def _install_socket(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> _ScriptedSocket:
    import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (service resolver)

    socket = _ScriptedSocket(payload)
    monkeypatch.setattr(
        client_module, "_connect_daemon_socket", lambda *_a, **_k: socket
    )
    return socket


def test_invalid_json_keeps_the_decode_error_as_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The malformed-frame refusal chained the decode error in the base client."""

    import hyprial.shell.impl.cli.commands.common.support as support

    socket = _install_socket(monkeypatch, b"{not json}\n")

    with pytest.raises(support.CliError) as caught:
        support._daemon_request("ping")

    assert caught.value.code == "INVALID_JSON"
    assert isinstance(caught.value.__cause__, (ValueError, json.JSONDecodeError)), (
        "INVALID_JSON must chain the decode error, like the base client did; "
        f"got {caught.value.__cause__!r}"
    )
    assert isinstance(caught.value.__context__, RequestPortError)
    assert socket.closed is True


def test_other_refusals_keep_no_explicit_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every non-JSON refusal was raised bare in the base client."""

    import hyprial.shell.impl.cli.commands.common.support as support

    _install_socket(
        monkeypatch,
        b'{"version": 2, "id": "x", "result": {}}\n',
    )

    with pytest.raises(support.CliError) as caught:
        support._daemon_request("ping")

    assert caught.value.code == ipc_errors.VERSION_MISMATCH
    assert caught.value.__cause__ is None, (
        "VERSION_MISMATCH must stay capless, like the base client; "
        f"got {caught.value.__cause__!r}"
    )


def test_cli_request_port_delegates_to_the_kernel_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shell adapter must not re-implement transport decisions."""

    import hyprial.shell.impl.cli.commands.common.support as support
    from hyprial.shell.impl.cli.commands.common.services import get_services

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    seen: dict[str, Any] = {}

    def fake_request(method: str, params: Any = None, **kwargs: Any) -> Any:
        seen["method"] = method
        seen["params"] = params
        seen["socket_path"] = kwargs["socket_path"]()
        seen["timeout"] = kwargs["timeout"]
        seen["restore_wait"] = kwargs["restore_wait"]
        seen["clock"] = kwargs["clock"]()
        seen["request_id"] = kwargs["uuid4"]()
        return {"ok": True}

    monkeypatch.setattr(support, "daemon_request", fake_request)

    assert support._daemon_request("ping", {"value": 1}, restore_wait=0.0) == {
        "ok": True
    }
    assert seen["method"] == "ping"
    assert seen["params"] == {"value": 1}
    assert seen["socket_path"] == state / "daemon.sock"
    assert seen["timeout"] is None
    assert seen["restore_wait"] == 0.0
    assert seen["clock"] is get_services().time
    assert isinstance(seen["request_id"], UUID)


def test_cli_request_uses_the_injected_service_endpoint_on_every_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One composition-root endpoint feeds requests and daemon commands.

    ``support._daemon_request`` must not call the module-local ``_socket_path``
    directly: a ``CliServices`` binding injected by the composition root (or a
    test) is what every daemon-backed command reads, so the request must read
    the same provider -- and the kernel re-resolves it on every restore retry.
    """

    import hyprial.shell.impl.cli.commands.common.support as support
    from hyprial.shell.impl.cli.commands.common.services import get_services

    paths = iter(
        [
            tmp_path / "first.sock",
            tmp_path / "second.sock",
            tmp_path / "third.sock",
        ]
    )

    def injected_socket_path() -> Path:
        return next(paths)

    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    monkeypatch.setattr(_cli_get_services(), "_socket_path", injected_socket_path)
    monkeypatch.setattr(_cli_get_services(), "time", _Clock())
    assert get_services()._socket_path is injected_socket_path

    attempts: list[Path] = []

    def fake_once(_method, _params, *, socket_path, **_kwargs):  # type: ignore[no-untyped-def]
        attempts.append(socket_path)
        if len(attempts) < 3:
            raise ipc_errors.DaemonRestoringError("DAEMON_RESTORING", "restoring")
        return {"ok": True}

    monkeypatch.setattr(client_module, "daemon_request_once", fake_once)

    assert support._daemon_request("ping", restore_wait=10.0) == {"ok": True}
    assert attempts == [
        tmp_path / "first.sock",
        tmp_path / "second.sock",
        tmp_path / "third.sock",
    ]


def test_cli_socket_path_does_not_evaluate_the_state_root_for_an_explicit_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shell route keeps the kernel endpoint rule, not just the function."""

    import hyprial.shell.impl.cli.commands.common.support as support

    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    monkeypatch.delenv("HYPRIAL_HOME", raising=False)
    explicit = tmp_path / "explicit.sock"
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(explicit))

    def explode() -> Path:
        raise AssertionError("the state root must not be resolved")

    monkeypatch.setattr(_cli_get_services(), "_state_dir", explode)

    assert support._socket_path() == explicit.expanduser().resolve()


def test_cli_socket_path_uses_the_isolation_state_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hyprial.shell.impl.cli.commands.common.support as support

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "ignored.sock"))

    assert support._socket_path() == state / "daemon.sock"


_REMOVED_SHELL_TRANSPORT_NAMES = (
    "_PEER_GONE_ERRNOS",
    "_CONNECT_BACKLOG_ERRNOS",
    "_CONNECT_BACKLOG_RETRY_INTERVAL",
    "_connect_daemon_socket",
    "_peer_gone",
    "_daemon_request_once",
    "_RESTORE_WAIT_POLL_SECONDS",
    "_RESTORE_WAIT_DEFAULT_SECONDS",
    "_RESTORE_WAIT_MAX_SECONDS",
    "_restore_wait_budget",
    "_DAEMON_IPC_ROUNDTRIP_SECONDS",
    "_daemon_ipc_roundtrip_seconds",
)


def test_shell_no_longer_owns_the_transport() -> None:
    """The old shell copy, its constants, and the entry re-export are gone."""

    import hyprial.shell.impl.cli.runtime.entry as entry
    import hyprial.shell.impl.cli.commands.common.support as support

    for name in _REMOVED_SHELL_TRANSPORT_NAMES:
        assert not hasattr(support, name), f"shell still defines {name}"
        assert not hasattr(entry, name), f"entry still re-exports {name}"
        assert name not in entry.__all__, f"entry still advertises {name}"

    support_source = Path(support.__file__).read_text(encoding="utf-8")
    for token in (
        "def _connect_daemon_socket(",
        "def _peer_gone(",
        "def _daemon_request_once(",
        "def _restore_wait_budget(",
        "def _daemon_ipc_roundtrip_seconds(",
    ):
        assert token not in support_source, f"shell still contains {token!r}"

    assert support.daemon_request is kernel_module.daemon_request
    assert support.daemon_socket_path is kernel_module.daemon_socket_path
