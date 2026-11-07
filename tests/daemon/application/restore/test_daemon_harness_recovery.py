"""A daemon restart must bring back the connectors this node declared.

This property had a test -- ``contract/daemon-lifecycle`` -- and that test was
red for 38 hours on the integration branch without anyone noticing, because
contract runners are driven independently and are not in anybody's routine
gate. Two agents each correctly established "not introduced by my branch" and
each stopped there.

So the property lives here now, in the suite people actually run. What broke
was not the recovery machinery -- calling ``restore`` still works fine -- but
that nothing called it: ``DaemonEventBridge.start`` deliberately stopped
restoring (see ``test_event_bridge_start_never_restores_harness_processes``)
and the responsibility moved to whoever composes the daemon, where it was
never picked up.

Two halves partition the declaration, and the split is load-bearing rather
than cosmetic: ``_restore_adapters`` starts exactly the ``lark`` specs, and
``_restore_harnesses`` starts exactly the rest. Restoring Lark in both would
give adapter routes and liveliness two owners, which is what the original
``harness_selector=lambda spec: spec.harness != "lark"`` prevented before it
was deleted. Nothing in the types keeps the two filters complementary; the
first test below is what keeps them so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.harnesses.runtime.ports import HarnessRestoreProjection


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

NODE = "recovery-node"
OWNER = "tester"


class _RecordingHarnesses:
    """Records what the composer asks to be restored."""

    def __init__(self) -> None:
        self.restored: list[tuple[str, str]] = []
        self.calls = 0

    def restore(self, desired: tuple[Any, ...]) -> HarnessRestoreProjection:
        self.calls += 1
        self.restored.extend((spec.harness, spec.name) for spec in desired)
        # The real summary type, not a fabricated shape: restore's contract
        # is HarnessRestoreProjection (F1 added the ``deferred`` count), and
        # the daemon's recovery log reads its fields.
        return HarnessRestoreProjection(
            attempted=len(desired), restored=len(desired), failed=0, deferred=0
        )


def _write_desired(state_dir: Path, providers: list[dict[str, Any]]) -> None:
    desired = state_dir / "desired-state.json"
    desired.parent.mkdir(parents=True, exist_ok=True)
    Path(f"{desired}.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": providers,
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )


def _provider(harness: str, name: str) -> dict[str, Any]:
    return {
        "provider": harness,
        "name": name,
        "headless": True,
        "args": [],
        "ownership": "managed",
    }


def _application(
    tmp_path: Path, *, desired: list[dict[str, Any]] | None = None
) -> DaemonApplication:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state_dir = home / "state"
    state_dir.mkdir(parents=True)
    if desired is not None:
        # The seed must exist BEFORE the store constructs: construction is
        # what runs the .v1 -> SQLite migration (U0a-2).
        _write_desired(state_dir, desired)
    return DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id=NODE,
        hyprial_home=home,
        owner=OWNER,
        **_app_injections(),
    )


def test_declared_harnesses_come_back_and_lark_is_left_to_its_owner(
    tmp_path: Path, own_daemon_application,
) -> None:
    """Both directions in one declaration, because either alone is ambiguous.

    "the pi worker was restored" alone is equally explained by "it restores
    everything"; "lark was not restored" alone is equally explained by "it
    restores nothing". The declaration contains both kinds so the partition
    itself is what gets asserted.
    """

    app = own_daemon_application(_application(
        tmp_path,
        desired=[_provider("pi", "squire"), _provider("lark", "alicesmith-hyprial-e2e")],
    ))
    harnesses = _RecordingHarnesses()
    app._harnesses = harnesses  # type: ignore[assignment]

    app._restore_harnesses()

    assert harnesses.calls == 1
    assert harnesses.restored == [("pi", "squire")], (
        "expected exactly the non-Lark declaration to be restored here; Lark "
        "specs belong to _restore_adapters, and starting them in both places "
        f"gives adapter routes two owners: {harnesses.restored}"
    )


def test_a_declaration_with_nothing_to_restore_asks_for_nothing(
    tmp_path: Path, own_daemon_application,
) -> None:
    """The negative control for the test above."""

    app = own_daemon_application(
        _application(tmp_path, desired=[_provider("lark", "adapter-only")])
    )
    harnesses = _RecordingHarnesses()
    app._harnesses = harnesses  # type: ignore[assignment]

    app._restore_harnesses()

    assert harnesses.calls == 0
    assert harnesses.restored == []


def test_daemon_startup_actually_asks_for_the_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, not the method -- because the defect was a missing call.

    ``_restore_harnesses`` working in isolation is exactly the state the
    daemon was already in: ``restore`` was reachable and correct, and the
    startup sequence simply never reached for it. A test that only calls the
    method directly would have passed throughout the outage.
    """

    app = _application(tmp_path, desired=[_provider("pi", "squire")])
    reached: list[str] = []

    for name in (
        "_start_runtime",
        "_start_server",
        "_restore_adapters",
        "_write_pid_file",
        "_serve",
        "_start_maintenance_scheduler",
    ):
        monkeypatch.setattr(app, name, lambda: None)
    monkeypatch.setattr(
        app, "_restore_harnesses", lambda: reached.append("restore")
    )

    app.run()
    # Restore runs on its own thread now (accept no longer waits for it), so
    # `run()` returning proves nothing about restore: the gate opening is the
    # "restore completed" observation, and it must already have happened.
    assert app._restore_done.wait(2.0), (
        "daemon startup completed without asking for a harness restore; a "
        "restart would leave every declared connector down, and the only "
        "test that noticed last time was a contract runner nobody runs"
    )

    assert reached == ["restore"], (
        "daemon startup completed without asking for a harness restore; a "
        "restart would leave every declared connector down, and the only "
        "test that noticed last time was a contract runner nobody runs"
    )


def test_both_recovery_halves_reach_the_launch_summary() -> None:
    """The CLI's launch summary must not report one half and omit the other.

    ``_SAFE_DAEMON_STARTUP_EVENTS`` is an allowlist, so a new startup event is
    invisible there until someone remembers to add it. Reporting that adapters
    recovered while staying silent about harnesses is the exact shape of the
    outage this work exists to close, one layer up.
    """

    from hyprial.shell.impl.cli.commands.common.daemon_start import _SAFE_DAEMON_STARTUP_EVENTS

    missing = {"adapter.recovery.completed", "harness.recovery.completed"} - set(
        _SAFE_DAEMON_STARTUP_EVENTS
    )
    assert not missing, (
        f"startup recovery events missing from the CLI launch summary: {missing}"
    )
