"""Daemon composition starts the teleport target: purge, register, close.

The lightest fixture that holds the production registry, home and identity is
a constructed (not started) ``DaemonApplication`` with explicit temp
directories and a loopback-only listen endpoint, as in
``tests/daemon/application/composition/composition``.  Booting the full
startup graph needs a live transport and harness actors, so the startup call
site is pinned by reading the startup wiring source instead.

Contract pinned here:

- ``DaemonApplication._start_teleport_target(transport)`` builds the real
  ``RegistryTargetHost`` from the application (``node_id``, ``owner``, its
  agent registry, ``hyprial_home``, and ``runtime_available(h) =
  shutil.which(h) is not None``), calls ``start_teleport_target(transport,
  host=..., hyprial_home=app.hyprial_home)`` and keeps the result as
  ``app._teleport_target``.
- The daemon startup wiring under ``daemon/impl/application/wiring/`` calls
  ``self._start_teleport_target(`` with the startup transport.
- ``DaemonApplication._close()`` closes the teleport registration.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.transfer.archive.envelope import (
    ensure_recipient_key,
    recipient_key_path,
)
from hyprial.daemon.impl.transport import KeySpace
from hyprial.shell.impl.daemon_wiring import daemon_dependencies

from ..support import teleport
from ..target.support import RecordingSession, key, prepare_request, protocol

NODE = "node-teleport"
OWNER = "owner"


def _application(tmp_path: Path) -> DaemonApplication:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{probe.getsockname()[1]}"
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        hyprial_home=tmp_path / "home",
        zenoh_listen=(endpoint,),
        zenoh_connect=(),
        **daemon_dependencies(),
    )
    app.network_isolated = True
    return app


@pytest.fixture
def app(tmp_path: Path, request) -> DaemonApplication:
    application = _application(tmp_path)

    def close() -> None:
        application._close()
        application._close_home_lease()

    request.addfinalizer(close)
    return application


def _prepare(session: RecordingSession, operation_id: str, **overrides) -> dict:
    k = key("prepare", operation_id, node=NODE)
    request = prepare_request(operation_id, **{"target_node": NODE, "owner": OWNER, **overrides})
    replies = session.handler()(k, protocol().encode_prepare_request(request))
    assert len(replies) == 1
    assert replies[0][0] == k
    return teleport().decode_reply(replies[0][1]).header


def test_start_purges_staging_and_registers_the_handler(app: DaemonApplication) -> None:
    root = teleport().staging_root(app.hyprial_home, app.state_dir)
    (root / "op-stale").mkdir(parents=True)
    (root / "op-stale" / "stream").write_bytes(b"left by a previous daemon")
    session = RecordingSession()
    app._start_teleport_target(session)
    assert list(root.iterdir()) == []
    assert [declared for declared, _handler in session.declared] == [
        KeySpace().agent_teleport_all(NODE)
    ]
    assert app._teleport_target.registration is session.registrations[0]


def test_registered_service_speaks_for_this_daemon(
    app: DaemonApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake = binaries / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}")
    app.agents.create("taken")
    session = RecordingSession()
    app._start_teleport_target(session)

    wrong_owner = _prepare(session, "op-owner", owner="owner-other", dry_run=True)
    assert wrong_owner["error"]["code"] == "TELEPORT_OWNER_MISMATCH"

    exists = _prepare(session, "op-exists", agent="taken", dry_run=True)
    assert exists["error"]["code"] == "TELEPORT_AGENT_EXISTS"

    ok = _prepare(session, "op-ok", agent="fresh", dry_run=True)
    assert ok["ok"] is True
    reply = protocol().decode_prepare_reply(teleport().encode_frame(ok))
    assert reply.target_node == NODE
    assert reply.recipient_public_key == ensure_recipient_key(
        recipient_key_path(app.hyprial_home)
    )[1]
    assert list(teleport().staging_root(app.hyprial_home, app.state_dir).iterdir()) == []


def test_close_closes_the_teleport_registration(app: DaemonApplication) -> None:
    session = RecordingSession()
    app._start_teleport_target(session)
    app._close()
    assert session.registrations[0].closed is True


def test_startup_wiring_calls_the_teleport_target_start() -> None:
    wiring_dir = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "hyprial"
        / "daemon"
        / "impl"
        / "application"
        / "wiring"
    )
    callers = [
        path.name
        for path in sorted(wiring_dir.glob("*.py"))
        if "self._start_teleport_target(" in path.read_text(encoding="utf-8")
    ]
    assert callers != []
