"""``start_teleport_target``: purge staging, then register the query handler.

Contract pinned here (module ``hyprial.daemon.impl.teleport.wiring.target``):

- ``start_teleport_target(session, *, host, hyprial_home, routines=None,
  key_space=None) -> TeleportTargetRuntime``.  It builds a
  ``StagingReceiver`` on ``staging_root(hyprial_home)``, calls ``purge()``
  **before** declaring anything, builds ``TeleportTargetService`` and binds it
  with ``bind_target_service``.
- ``TeleportTargetRuntime`` exposes ``service``, ``receiver`` and
  ``registration``; ``close()`` closes the registration and the service, and
  is idempotent.
- No authentication or allow-list gates the handler (handoff §6, KP-047).
"""

from __future__ import annotations

import importlib
import stat
from pathlib import Path

from hyprial.daemon.impl.transport import KeySpace

from ..support import teleport
from ..target.support import (
    NODE,
    FakeHost,
    RecordingSession,
    key,
    prepare_request,
    protocol,
)

WIRING = "hyprial.daemon.impl.teleport.wiring.target"


def wiring():
    return importlib.import_module(WIRING)


def _residue(home: Path) -> Path:
    root = teleport().staging_root(home)
    (root / "op-stale").mkdir(parents=True)
    (root / "op-stale" / "stream").write_bytes(b"half a stream")
    (root / "stray-file").write_bytes(b"x")
    return root


def test_start_purges_staging_before_declaring(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = _residue(home)
    seen: list[list[str]] = []
    session = RecordingSession(
        on_declare=lambda _key: seen.append(sorted(p.name for p in root.iterdir()))
    )
    runtime = wiring().start_teleport_target(session, host=FakeHost(), hyprial_home=home)
    try:
        assert seen == [[]]
        assert [declared for declared, _handler in session.declared] == [
            KeySpace().agent_teleport_all(NODE)
        ]
        assert Path(runtime.receiver.root) == root
        assert runtime.service.node_id == NODE
    finally:
        runtime.close()


def test_registered_handler_serves_prepare_without_authentication(tmp_path: Path) -> None:
    session = RecordingSession()
    runtime = wiring().start_teleport_target(
        session, host=FakeHost(), hyprial_home=tmp_path / "home"
    )
    try:
        k = key("prepare", "op-1")
        payload = protocol().encode_prepare_request(prepare_request("op-1"))
        replies = session.handler()(k, payload)
        assert len(replies) == 1
        assert replies[0][0] == k
        assert protocol().decode_prepare_reply(replies[0][1]).target_node == NODE
        assert runtime.service.state("op-1") == "prepared"
    finally:
        runtime.close()


def test_close_is_idempotent_and_closes_the_registration(tmp_path: Path) -> None:
    session = RecordingSession()
    runtime = wiring().start_teleport_target(
        session, host=FakeHost(), hyprial_home=tmp_path / "home"
    )
    runtime.close()
    runtime.close()
    assert session.registrations[0].closed is True
    assert runtime.registration is session.registrations[0]


def test_start_teleport_target_creates_private_state_and_staging_dirs(
    tmp_path: Path,
) -> None:
    home = tmp_path / "private-home"
    home.mkdir(mode=0o700)
    assert not (home / "state").exists()
    runtime = wiring().start_teleport_target(
        RecordingSession(), host=FakeHost(), hyprial_home=home
    )
    try:
        assert stat.S_IMODE((home / "state").stat().st_mode) == 0o700
        assert stat.S_IMODE((home / "state" / "teleport").stat().st_mode) == 0o700
        assert (
            stat.S_IMODE((home / "state" / "teleport" / "staging").stat().st_mode)
            == 0o700
        )
    finally:
        runtime.close()
