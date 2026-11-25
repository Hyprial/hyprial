"""The ownerless lifecycle loop: what follows each way a phase ends.

Decision: daemon-first-z2-loop-decisions-20261116.  The fakes below stand for
the wire host and the serving application only to pin the loop's decisions
deterministically; the same decisions are exercised against the real daemon in
``tests/integration/daemon_lifecycle``.
"""

import json
import os
import signal

import pytest

from hyprial.daemon.impl.application.onboarding.bootstrap import wire
from hyprial.daemon.impl.application.onboarding.lifecycle import ownerless as lifecycle
from hyprial.kernel import process_birth_identity


class _Runtime:
    def __init__(self, reasons, on_run=None) -> None:
        self.reasons = list(reasons)
        self.on_run = on_run
        self.owns_process_exit_seen: list[bool] = []
        self.exit_unless_logout_seen: list[bool] = []

    def run_daemon(self, factory, *, owns_process_exit, exit_unless_logout=False):
        self.owns_process_exit_seen.append(owns_process_exit)
        self.exit_unless_logout_seen.append(exit_unless_logout)
        if self.on_run is not None:
            self.on_run()
        return self.reasons.pop(0)


class _Dispatcher:
    def __init__(self, runtime, epoch) -> None:
        self.runtime = runtime
        self.epoch = epoch


class _Host:
    def __init__(self, outcome, runtime, epoch) -> None:
        self.outcome = outcome
        self.dispatcher = _Dispatcher(runtime, epoch)
        self.closed = False
        self.stop_requests = 0

    def serve(self):
        return self.outcome

    def request_stop(self):
        self.stop_requests += 1

    def close(self):
        self.closed = True


class _Loop:
    """Drives ``launch_daemon_lifecycle`` over scripted hosts."""

    def __init__(self, monkeypatch, tmp_path, outcomes, reasons=(), on_run=None) -> None:
        self.runtime = _Runtime(reasons, on_run)
        self.hosts = [
            _Host(outcome, self.runtime, f"epoch-{index}") for index, outcome in enumerate(outcomes)
        ]
        self.queue = iter(self.hosts)
        self.native_calls = 0
        self.epochs: list[str] = []
        monkeypatch.setattr(lifecycle.BootstrapWireHost, "open", lambda *_a, **_k: next(self.queue))
        monkeypatch.setattr(lifecycle, "_construct_promoted_application", lambda *_a: object())
        self.tmp_path = tmp_path

    def run(self, native_loader=None, **options):
        def native():
            self.native_calls += 1

        lifecycle.launch_daemon_lifecycle(
            home=self.tmp_path / "home",
            state_dir=self.tmp_path / "state",
            socket_path=self.tmp_path / "state" / "daemon.sock",
            application_factory=lambda *_args: object(),
            native_loader=native_loader or native,
            bind_epoch=self.epochs.append,
            **options,
        )


def test_lifecycle_reopens_bootstrap_after_logout(monkeypatch, tmp_path):
    loop = _Loop(
        monkeypatch, tmp_path, [wire.PROMOTE, wire.PROMOTE, wire.STOP], reasons=["logout", "logout"]
    )

    loop.run(owns_process_exit=False, exit_unless_logout=True)

    assert [host.closed for host in loop.hosts] == [True, True, True]
    assert loop.runtime.owns_process_exit_seen == [False, False]
    assert loop.runtime.exit_unless_logout_seen == [True, True]
    assert loop.native_calls == 1
    assert loop.epochs == ["epoch-0", "epoch-1", "epoch-2"]
    assert not any(host.stop_requests for host in loop.hosts)


def test_shutdown_after_promotion_exits_without_reopening_bootstrap(monkeypatch, tmp_path):
    loop = _Loop(monkeypatch, tmp_path, [wire.PROMOTE, wire.STOP], reasons=["shutdown"])

    loop.run()

    assert loop.hosts[0].closed
    assert next(loop.queue) is loop.hosts[1], "a second bootstrap wire was opened after a shutdown"


def test_a_reopen_outcome_admits_a_fresh_wire_without_promoting(monkeypatch, tmp_path):
    loop = _Loop(monkeypatch, tmp_path, [wire.REOPEN, wire.STOP])

    loop.run()

    assert loop.runtime.owns_process_exit_seen == []
    assert [host.closed for host in loop.hosts] == [True, True]


def test_unacknowledged_initialize_exits_with_a_stable_code(monkeypatch, tmp_path):
    loop = _Loop(monkeypatch, tmp_path, [wire.UNACKNOWLEDGED, wire.STOP])

    with pytest.raises(lifecycle.BootstrapLaunchError) as refused:
        loop.run()

    assert refused.value.code == "BOOTSTRAP_INITIALIZE_UNACKNOWLEDGED"
    assert loop.hosts[0].closed
    assert next(loop.queue) is loop.hosts[1]


def test_an_unexpected_failure_keeps_its_own_string_code(monkeypatch, tmp_path):
    class Refused(Exception):
        code = "SOME_REFUSAL"

    def boom():
        raise Refused("nope")

    loop = _Loop(monkeypatch, tmp_path, [wire.PROMOTE], on_run=boom)

    with pytest.raises(lifecycle.BootstrapLaunchError) as refused:
        loop.run()

    assert refused.value.code == "SOME_REFUSAL"
    assert loop.hosts[0].closed


@pytest.mark.parametrize("code", [None, 7, ""])
def test_a_non_string_failure_code_becomes_the_lifecycle_code(monkeypatch, tmp_path, code):
    def boom():
        failure = RuntimeError("nope")
        failure.code = code
        raise failure

    loop = _Loop(monkeypatch, tmp_path, [wire.PROMOTE], on_run=boom)

    with pytest.raises(lifecycle.BootstrapLaunchError) as refused:
        loop.run()

    assert refused.value.code == "BOOTSTRAP_LIFECYCLE_FAILED"


def test_sigterm_between_logout_and_the_next_wire_ends_in_exit(monkeypatch, tmp_path):
    loop = _Loop(
        monkeypatch,
        tmp_path,
        [wire.PROMOTE, wire.STOP],
        reasons=["logout"],
        on_run=lambda: os.kill(os.getpid(), signal.SIGTERM),
    )
    before = signal.getsignal(signal.SIGTERM)

    loop.run()

    assert loop.hosts[0].closed
    assert next(loop.queue) is loop.hosts[1], "a second bootstrap wire was opened after SIGTERM"
    assert signal.getsignal(signal.SIGTERM) is before


def test_sigint_before_the_first_wire_opens_exits_before_admission(monkeypatch, tmp_path):
    loop = _Loop(monkeypatch, tmp_path, [wire.STOP])

    loop.run(native_loader=lambda: os.kill(os.getpid(), signal.SIGINT))

    assert next(loop.queue) is loop.hosts[0], "a wire was opened although a stop was pending"


def test_a_signal_during_wire_admission_stops_the_new_wire(monkeypatch, tmp_path):
    loop = _Loop(monkeypatch, tmp_path, [wire.STOP])
    opened = lifecycle.BootstrapWireHost.open

    def open_after_signal(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        return opened(*args, **kwargs)

    monkeypatch.setattr(lifecycle.BootstrapWireHost, "open", open_after_signal)

    loop.run()

    assert loop.hosts[0].stop_requests == 1
    assert loop.hosts[0].closed


def test_a_signal_during_promotion_stops_the_promoted_application(monkeypatch, tmp_path):
    requested: list[tuple[str, int | None]] = []
    installed: list[dict] = []

    class Application:
        epoch = "serving-epoch"

        def _request_stop(self, source, signal_number=None):
            requested.append((source, signal_number))

    class Runtime:
        state_dir = tmp_path / "state"
        socket_path = tmp_path / "state" / "daemon.sock"

        def verified_owner(self):
            return "owner@example.test"

    class Surface:
        home_binding, host_control_session_id = "h", "c"
        view_session_id, transport_generation = "v", "g"

    class Host:
        dispatcher = type(
            "Dispatcher", (), {"context": type("Context", (), {"surface": Surface})(), "first_login": None}
        )()

    monkeypatch.setattr(lifecycle, "replace", lambda surface, **_kw: surface)
    monkeypatch.setattr(lifecycle, "CallContext", lambda **kw: kw)
    monkeypatch.setattr(
        lifecycle, "install_serving_port",
        lambda runtime, application, **kw: installed.append(kw["context"]) or application,
    )
    signals = lifecycle._LoopSignals()
    signals.requested = True
    epochs: list[str] = []

    application = lifecycle._construct_promoted_application(
        Runtime(), Host(), lambda *_a: Application(), epochs.append, signals
    )

    assert isinstance(application, Application)
    assert requested == [("signal", signal.SIGTERM)]
    assert epochs == ["serving-epoch"]
    assert installed[0]["phase"] == "serving" and installed[0]["owner"] == "owner@example.test"


def test_the_ready_marker_names_the_process_and_is_removed_only_when_it_is_ours(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    socket = state / "daemon.sock"

    lifecycle._publish_bootstrap_ready(state, socket, "launch-1")
    marker = json.loads((state / "daemon.json").read_text(encoding="utf-8"))
    assert marker == {
        "schemaVersion": 1, "pid": os.getpid(), "socket": str(socket), "phase": "bootstrap",
        "birthIdentity": process_birth_identity(os.getpid()), "launchId": "launch-1",
    }

    lifecycle._remove_bootstrap_ready(state, "another-launch")
    assert (state / "daemon.json").exists()
    lifecycle._remove_bootstrap_ready(state, "launch-1")
    assert not (state / "daemon.json").exists()

    serving = {"schemaVersion": 1, "pid": os.getpid(), "socket": str(socket), "epoch": "e"}
    (state / "daemon.json").write_text(json.dumps(serving), encoding="utf-8")
    lifecycle._remove_bootstrap_ready(state, None)
    assert json.loads((state / "daemon.json").read_text(encoding="utf-8")) == serving


def test_daemon_process_entry_sets_private_umask_once_across_phases(monkeypatch, tmp_path):
    import sys
    import hyprial.kernel as kernel_module
    from hyprial.daemon.impl.application import startup as startup_module
    from hyprial.shell.impl.cli.runtime import entry as cli_entry
    from hyprial.shell.impl.gui.host import __main__ as gui_host_main

    assert not hasattr(lifecycle, "apply_private_umask")
    assert not hasattr(startup_module, "apply_private_umask")

    umask_calls: list[str] = []
    monkeypatch.setattr(
        kernel_module,
        "apply_private_umask",
        lambda mask=0o077: umask_calls.append(f"kernel:{oct(mask)}") or 0,
    )
    monkeypatch.setattr(
        gui_host_main,
        "apply_private_umask",
        lambda mask=0o077: umask_calls.append(f"gui-host:{oct(mask)}") or 0,
    )

    loop = _Loop(
        monkeypatch,
        tmp_path,
        [wire.PROMOTE, wire.PROMOTE, wire.STOP],
        reasons=["logout", "logout"],
    )
    monkeypatch.setattr(
        cli_entry,
        "app",
        lambda **_kwargs: loop.run(owns_process_exit=False, exit_unless_logout=True) or 0,
    )
    monkeypatch.setattr(sys, "argv", ["hyprial", "daemon", "run", "--json"])
    cli_entry.main()

    assert umask_calls == ["kernel:0o77"]
    assert loop.epochs == ["epoch-0", "epoch-1", "epoch-2"]

    assert gui_host_main.serve(["serve", "--root", str(tmp_path), "--host", "0.0.0.0"]) == 2
    assert umask_calls == ["kernel:0o77", "gui-host:0o77"]
