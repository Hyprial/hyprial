"""Admitted onboarding contexts and the application assembly mixin.

The frame is attached by the public transport for exactly one handler and is
never derived from ``trusted_message_origin`` or a request body.  The only
filesystem object is an empty temporary home used to keep the ready boundary on
that same bootstrap decision.
"""

from __future__ import annotations

from pathlib import Path
import threading

import pytest

from hyprial.daemon.impl.application.onboarding import (
    _OnboardingAssemblyMixin,
    admitted_onboarding_context,
    current_onboarding_context,
)
from hyprial.daemon.impl.request_catalog.onboarding import (
    CallContext,
    Surface,
)
from hyprial.kernel import DaemonRequestError, ipc_errors

SURFACE = Surface("home-binding", "host-control", "view", "generation-1")


def _context(phase: str = "bootstrap", caller: str = "local-bootstrap") -> CallContext:
    return CallContext(
        surface=SURFACE,
        phase=phase,
        caller=caller,
        owner=None,
    )


class FakePort:
    def __init__(self) -> None:
        self.ready_calls = 0

    def status(self, context):
        return {}

    def begin_auth(self, context, *, intent, operation_key):
        return {}

    def get(self, context, *, operation_id):
        return {}

    def cancel(self, context, *, operation_id):
        return {}

    def ensure_device(self, context, *, operation_key):
        return {}

    def initialize(self, context, *, operation_key):
        return {}

    def take_post_reply(self):
        return None

    def on_daemon_ready(self) -> None:
        self.ready_calls += 1


class _BaseApplication:
    """A minimal application: an empty home, so assembly decides "bootstrap".

    ``_write_pid_file`` now owns the account-port assembly, which reads durable
    state.  An empty home keeps these cases on the "no port is assembled" path
    without opening a journal or inventing an owner.
    """

    def __init__(self, home: Path) -> None:
        self.events: list[str] = []
        self.hyprial_home = home
        self.state_dir = home / "state"
        self.state_dir.mkdir(mode=0o700, exist_ok=True)

    def _write_pid_file(self) -> None:
        self.events.append("pid-file")


class _Application(_OnboardingAssemblyMixin, _BaseApplication):
    pass


def test_no_frame_is_visible_outside_the_admitted_block():
    assert current_onboarding_context() is None
    with admitted_onboarding_context(_context()):
        assert current_onboarding_context() == _context()
    assert current_onboarding_context() is None


def test_exact_pairs_are_admitted_and_the_innermost_frame_wins():
    bootstrap = _context()
    serving = _context("serving", "local-gui")
    with admitted_onboarding_context(bootstrap):
        assert current_onboarding_context() is bootstrap
        with admitted_onboarding_context(serving):
            assert current_onboarding_context() is serving
        assert current_onboarding_context() is bootstrap
    assert current_onboarding_context() is None


@pytest.mark.parametrize(
    ("phase", "caller"),
    [
        ("bootstrap", "local-gui"),
        ("serving", "local-bootstrap"),
    ],
)
def test_a_mixed_phase_caller_pair_is_refused(phase, caller):
    with pytest.raises(ValueError, match="phase/caller pair"):
        with admitted_onboarding_context(_context(phase, caller)):
            pass
    assert current_onboarding_context() is None


def test_a_non_context_frame_is_refused():
    with pytest.raises(TypeError, match="CallContext"):
        with admitted_onboarding_context({"phase": "bootstrap"}):
            pass


def test_the_frame_is_thread_local():
    seen: dict[str, CallContext | None] = {}

    def observe() -> None:
        seen["context"] = current_onboarding_context()

    with admitted_onboarding_context(_context()):
        worker = threading.Thread(target=observe)
        worker.start()
        worker.join()

    assert seen["context"] is None
    assert current_onboarding_context() is None


def test_uninstalled_port_is_daemon_not_ready(tmp_path: Path) -> None:
    app = _Application(tmp_path)

    with pytest.raises(DaemonRequestError) as refused:
        _ = app.onboarding

    assert refused.value.code == ipc_errors.DAEMON_NOT_READY


def test_install_requires_every_port_method_and_installs_once(tmp_path: Path) -> None:
    app = _Application(tmp_path)

    class Partial:
        def status(self, context):
            return {}

    with pytest.raises(ValueError, match="missing begin_auth"):
        app.install_onboarding(Partial())

    port = FakePort()
    app.install_onboarding(port)
    assert app.onboarding is port
    with pytest.raises(RuntimeError, match="already assembled"):
        app.install_onboarding(FakePort())


def test_context_acquisition_ignores_the_trusted_origin_and_never_invents_one(tmp_path: Path) -> None:
    app = _Application(tmp_path)
    app.install_onboarding(FakePort())

    with pytest.raises(DaemonRequestError) as refused:
        app.onboarding_context(trusted_message_origin="ipc")
    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED

    context = _context()
    with admitted_onboarding_context(context):
        assert (
            app.onboarding_context(trusted_message_origin="spoofed-origin")
            is context
        )

    with pytest.raises(DaemonRequestError) as refused:
        app.onboarding_context(trusted_message_origin="ipc")
    assert refused.value.code == ipc_errors.CALLER_NOT_AUTHORIZED


def test_pid_file_boundary_runs_the_port_ready_hook_after_the_pid_file(tmp_path: Path) -> None:
    app = _Application(tmp_path)
    port = FakePort()
    app.install_onboarding(port)

    app._write_pid_file()

    assert app.events == ["pid-file"]
    assert port.ready_calls == 1


def test_pid_file_boundary_without_a_port_installed_only_writes_the_pid_file(tmp_path: Path) -> None:
    app = _Application(tmp_path)

    app._write_pid_file()

    assert app.events == ["pid-file"]
