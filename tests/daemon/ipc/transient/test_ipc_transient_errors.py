"""PR #332 F4②: ONE transient hierarchy, ONE registration, BOTH ends.

Allen 2026-09-03 ruling (spec-pr332-fixes §F4 ②): contracts/ipc_errors.py
defines ``TransientDaemonError`` plus one subclass per transient code with a
single code↔class registration point; the daemon serialises the envelope
code FROM the class; the four clients (CLI / MCP agent-channel / Lark
adapter worker / e2e runner -- one codebase) deserialise through the SAME
module back into the SAME class; an unknown code falls to the caller's
fallback path without crashing.

Each client gets its own test below: receive a DAEMON_RESTORING envelope ⇒
the raised error is the registered ``DaemonRestoringError`` -- the SAME
class instance type for all four.  An unknown code ⇒ the client's existing
permanent-error class, code preserved verbatim (the ruled fallback; no
other tolerance was added).
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import importlib.util
import json
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from hyprial.kernel import ipc_errors
from hyprial.kernel.impl.contracts.daemon.ipc_errors import (
    DaemonRestoringError,
    TransientDaemonError,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.support as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

REPO = Path(__file__).resolve().parents[4]


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "_daemon_runner", REPO / "contract/e2e/_daemon_runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "contract/e2e"))
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# The registry itself: one hierarchy, one registration point, bijection.
# --------------------------------------------------------------------------- #


def test_transient_registry_is_the_four_codes_with_unique_classes() -> None:
    assert set(ipc_errors.TRANSIENT_CODE_CLASSES) == {
        ipc_errors.DAEMON_RESTORING,
        ipc_errors.DAEMON_UNAVAILABLE,
        ipc_errors.DAEMON_DISCONNECTED,
        ipc_errors.IPC_TIMEOUT,
    }
    classes = list(ipc_errors.TRANSIENT_CODE_CLASSES.values())
    assert len(set(classes)) == len(classes), "one class per code, no sharing"
    for code, cls in ipc_errors.TRANSIENT_CODE_CLASSES.items():
        assert issubclass(cls, TransientDaemonError)
        assert issubclass(cls, ipc_errors.DaemonRequestError)
        assert cls.CODE == code
        assert cls("m").code == code


def test_transient_error_from_code_round_trips_the_class() -> None:
    error = ipc_errors.transient_error_from_code(
        ipc_errors.DAEMON_RESTORING, "restoring", {"gate": True}
    )
    assert type(error) is DaemonRestoringError
    assert error.code == ipc_errors.DAEMON_RESTORING
    assert str(error) == "restoring"
    assert error.data == {"gate": True}


@pytest.mark.parametrize(
    "code",
    [
        ipc_errors.INVALID_ARGUMENT,  # registered, permanent
        ipc_errors.SESSION_SUPERSEDED,  # registered, permanent
        "DAEMON_STILL_COMPILING",  # unknown: a newer peer minted one
        "",
    ],
)
def test_unknown_or_permanent_codes_fall_back_without_crashing(code: str) -> None:
    # The ruled fallback: None routes the caller down its existing
    # permanent-error class; the lookup itself never raises.
    assert ipc_errors.transient_error_from_code(code, "m") is None


# --------------------------------------------------------------------------- #
# Shared wire double: speaks the real version-1 newline-delimited protocol
# and emits the exact envelope shapes the daemon's ``_response()`` produces
# (mirrors tests/test_cli_restore_wait.py -- stubs must not invent shapes).
# --------------------------------------------------------------------------- #


class EnvelopeDaemon:
    """One-shot IPC double answering every request with one error envelope."""

    def __init__(self, socket_path: Path, code: str) -> None:
        self.socket_path = socket_path
        self.code = code
        self._stop = threading.Event()
        self._stopped = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(socket_path))
        self._server.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                self._server.settimeout(0.2)
                try:
                    client, _ = self._server.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(5.0)
                    buffer = bytearray()
                    while b"\n" not in buffer:
                        chunk = client.recv(64 * 1024)
                        if not chunk:
                            break
                        buffer.extend(chunk)
                    if not buffer:
                        continue
                    frame = json.loads(buffer.partition(b"\n")[0])
                    response = {
                        "version": 1,
                        "id": frame.get("id"),
                        "error": {
                            "code": self.code,
                            "message": "fixture envelope",
                        },
                    }
                    client.sendall(
                        json.dumps(response, separators=(",", ":")).encode() + b"\n"
                    )
        finally:
            self._stopped.set()

    def close(self) -> None:
        self._stop.set()
        self._stopped.wait(2.0)
        self._server.close()


@pytest.fixture
def root() -> Path:
    path = Path(tempfile.mkdtemp(prefix="hyprial-f4b-", dir=hyprial_test_tmp_root()))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def env(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HYPRIAL_HOME", str(root / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    return state


# --------------------------------------------------------------------------- #
# Client 1: CLI -- a DAEMON_RESTORING envelope raises the registered class.
# --------------------------------------------------------------------------- #


def test_cli_deserialises_restoring_envelope_to_registered_class(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS", "0")
    daemon = EnvelopeDaemon(env / "daemon.sock", ipc_errors.DAEMON_RESTORING)
    try:
        with pytest.raises(DaemonRestoringError) as caught:
            _cli_support._daemon_request("ps", restore_wait=0.0)
        assert type(caught.value) is DaemonRestoringError
        assert caught.value.code == ipc_errors.DAEMON_RESTORING
    finally:
        daemon.close()


def test_cli_unknown_code_falls_back_to_cli_error(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS", "0")
    daemon = EnvelopeDaemon(env / "daemon.sock", "DAEMON_STILL_COMPILING")
    try:
        with pytest.raises(_cli_get_services().CliError) as caught:
            _cli_support._daemon_request("ps", restore_wait=0.0)
        assert not isinstance(caught.value, TransientDaemonError)
        assert caught.value.code == "DAEMON_STILL_COMPILING"
    finally:
        daemon.close()


def test_cli_json_failure_renders_transient_code() -> None:
    # A surfaced transient must render exactly like the old CliError did:
    # ok:false with the code -- the runner classifies the CLI's JSON output.
    error = DaemonRestoringError("gate closed")
    failure = _cli_owner_1._json_failure(error)
    assert failure == {
        "ok": False,
        "code": ipc_errors.DAEMON_RESTORING,
        "error": "gate closed",
    }


# --------------------------------------------------------------------------- #
# Client 2: MCP agent-channel (mcp/unix.py) transport.
# --------------------------------------------------------------------------- #


def test_mcp_channel_deserialises_restoring_envelope_to_registered_class(
    env: Path,
) -> None:
    from hyprial.daemon.impl.mcp.api import DaemonRequestRejected
    from hyprial.daemon.impl.mcp.unix import UnixDaemonConnection

    daemon = EnvelopeDaemon(env / "daemon.sock", ipc_errors.DAEMON_RESTORING)
    try:
        connection = UnixDaemonConnection(env / "daemon.sock", timeout=5.0)
        with pytest.raises(DaemonRestoringError) as caught:
            connection._request_sync("message.pending.list", {}, None)
        assert type(caught.value) is DaemonRestoringError
        assert not isinstance(caught.value, DaemonRequestRejected)
    finally:
        daemon.close()


def test_mcp_channel_unknown_code_falls_back_to_rejected(env: Path) -> None:
    from hyprial.daemon.impl.mcp.api import DaemonRequestRejected
    from hyprial.daemon.impl.mcp.unix import UnixDaemonConnection

    daemon = EnvelopeDaemon(env / "daemon.sock", "DAEMON_STILL_COMPILING")
    try:
        connection = UnixDaemonConnection(env / "daemon.sock", timeout=5.0)
        with pytest.raises(DaemonRequestRejected) as caught:
            connection._request_sync("message.pending.list", {}, None)
        assert caught.value.code == "DAEMON_STILL_COMPILING"
    finally:
        daemon.close()


# --------------------------------------------------------------------------- #
# Client 3: Lark adapter worker (adapters/lark/worker.py _ipc).
# --------------------------------------------------------------------------- #


def test_worker_deserialises_restoring_envelope_to_registered_class(
    env: Path,
) -> None:
    from hyprial.daemon.impl.adapters.lark.worker.daemon_link import _ipc

    daemon = EnvelopeDaemon(env / "daemon.sock", ipc_errors.DAEMON_RESTORING)
    try:
        with pytest.raises(DaemonRestoringError) as caught:
            _ipc(env / "daemon.sock", "message.send", {})
        assert type(caught.value) is DaemonRestoringError
    finally:
        daemon.close()


def test_worker_unknown_code_falls_back_to_request_error(env: Path) -> None:
    from hyprial.daemon.impl.adapters.lark.worker.daemon_link import _ipc
    from hyprial.kernel import DaemonRequestError

    daemon = EnvelopeDaemon(env / "daemon.sock", "DAEMON_STILL_COMPILING")
    try:
        with pytest.raises(DaemonRequestError) as caught:
            _ipc(env / "daemon.sock", "message.send", {})
        assert type(caught.value) is DaemonRequestError
        assert caught.value.code == "DAEMON_STILL_COMPILING"
    finally:
        daemon.close()


# --------------------------------------------------------------------------- #
# Client 4: e2e runner (_daemon_runner.py) -- reads the CLI's JSON output.
# --------------------------------------------------------------------------- #


def test_runner_deserialises_cli_transient_json_to_registered_class() -> None:
    runner = _load_runner()
    stdout = json.dumps(
        {"ok": False, "code": ipc_errors.DAEMON_RESTORING, "error": "gate"}
    )
    error = runner.transient_cli_error(stdout, "evidence")
    assert type(error) is DaemonRestoringError
    assert error.code == ipc_errors.DAEMON_RESTORING


def test_runner_unknown_code_is_not_transient() -> None:
    runner = _load_runner()
    stdout = json.dumps({"ok": False, "code": "DAEMON_STILL_COMPILING"})
    assert runner.transient_cli_error(stdout, "evidence") is None


# --------------------------------------------------------------------------- #
# Both ends: the daemon mint IS the class; its ``.code`` is the envelope.
# --------------------------------------------------------------------------- #


def test_daemon_mint_is_the_registered_class() -> None:
    # ``DaemonApplication._response`` serialises ``getattr(error, "code")``
    # into the envelope; the restore gate mints the registered class, so the
    # wire code and the class are one declaration (the daemon-side envelope
    # bytes themselves are pinned by tests/test_daemon_restore_gate.py).
    minted = DaemonRestoringError("daemon is restoring its connectors")
    assert minted.code == ipc_errors.DAEMON_RESTORING
    # The envelope serialisation seam reads exactly that attribute.
    serialized_code = str(getattr(minted, "code", ipc_errors.DAEMON_ERROR))
    assert serialized_code == ipc_errors.DAEMON_RESTORING
    # The four clients all raise THIS class for that code.
    registry = ipc_errors.TRANSIENT_CODE_CLASSES[ipc_errors.DAEMON_RESTORING]
    assert registry is DaemonRestoringError


def test_all_four_clients_raise_the_same_class_instance_type() -> None:
    """The ruling's headline: one envelope code ⇒ one class, everywhere."""

    runner = _load_runner()
    cli_error = ipc_errors.transient_error_from_code(ipc_errors.DAEMON_RESTORING, "cli")
    runner_error = runner.transient_cli_error(
        json.dumps({"ok": False, "code": ipc_errors.DAEMON_RESTORING, "error": "e"}),
        "runner",
    )
    # The socket-driven tests above pin CLI / MCP / worker individually; the
    # shared-module calls here pin that they cannot diverge: every
    # deserialisation goes through the same registry lookup.
    assert type(cli_error) is type(runner_error) is DaemonRestoringError
