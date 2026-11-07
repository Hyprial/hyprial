"""The slots directory exists before any slots-carrying ``up`` leaves the daemon.

The e2e 2026-10-08 finding: a fresh home ships without
``<home>/state/tailcat/slots``, and from the daemon to the Go sidecar nobody
created it -- so the default unix slot transport was dead on a new home
(diagnostic fixtures had pre-built the directory, masking the gap).  The
daemon now calls :func:`ensure_slots_dir` on the up-sending start path,
unix-transport only, before the supervisor exists; the sidecar's own
listen-time ``MkdirAll`` is the second line of defense (Go suite
``TestListenSlotsCreatesMissingSlotsDirectory``).
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

from pathlib import Path

import pytest

import hyprial.daemon.impl.application.netendpoints.forwarding as forwarding_ops
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.forwarding.slots import (
    SLOT_TRANSPORT_ENV,
    ensure_slots_dir,
    slots_dir,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


class _Supervisor:
    """Stands where the mixin resolves the real class; records whether the
    slots directory already existed when it was constructed, so the test
    can pin the guard ran before any up request could be sent."""

    dir_present_at_construction: bool

    def __init__(self, environ: object, **kwargs: object) -> None:
        self.environ = environ
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def ensure_started(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


def _build_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> DaemonApplication:
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    # The home a fresh `hyprial login` leaves behind: existing, but with
    # no state/tailcat/slots chain.
    home = tmp_path / "home"
    home.mkdir()
    return DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state/daemon.sock",
        node_id="slots-dir-node",
        hyprial_home=home,
        owner="tester",
        **_deep_module_daemon_dependencies(),
    )


def test_up_start_path_creates_the_slots_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path, monkeypatch)
    made: list[_Supervisor] = []

    def factory(environ: object, **kwargs: object) -> _Supervisor:
        supervisor = _Supervisor(environ, **kwargs)
        # The up request would leave through ensure_started; capture the
        # guard's outcome at the moment the sender came into being.
        supervisor.dir_present_at_construction = slots_dir(app.hyprial_home).is_dir()
        made.append(supervisor)
        return supervisor

    monkeypatch.setattr(forwarding_ops, "ForwardingSidecarSupervisor", factory)
    app._forwarding_environment = {"HYPRIAL_FORWARDING_TEST": "1"}

    app._start_forwarding_supervisor()

    # The fresh home gained the whole missing chain.
    assert slots_dir(app.hyprial_home).is_dir()
    # The start path completed, unix transport was named for the sender,
    # and the directory pre-dated it -- the guard runs before any
    # slots-carrying up can leave the process.
    assert made and made[0].started
    assert made[0].dir_present_at_construction is True
    assert made[0].kwargs["slot_transport"] == "unix"
    # Calling the start path again stays quiet and idempotent.
    app._start_forwarding_supervisor()
    assert len(made) == 1


def test_tcp_transport_never_creates_the_slots_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SLOT_TRANSPORT_ENV, "tcp")
    app = _build_app(tmp_path, monkeypatch)
    made: list[_Supervisor] = []

    def factory(environ: object, **kwargs: object) -> _Supervisor:
        supervisor = _Supervisor(environ, **kwargs)
        made.append(supervisor)
        return supervisor

    monkeypatch.setattr(forwarding_ops, "ForwardingSidecarSupervisor", factory)
    app._forwarding_environment = {"HYPRIAL_FORWARDING_TEST": "1"}

    app._start_forwarding_supervisor()

    # TCP slots are kernel-assigned loopback ports: no socket directory
    # is involved, so none may be created.
    assert made and made[0].started
    assert not slots_dir(app.hyprial_home).exists()


def test_ensure_slots_dir_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = ensure_slots_dir(home)
    second = ensure_slots_dir(home)
    assert first == second == slots_dir(home)
    assert first.is_dir()
