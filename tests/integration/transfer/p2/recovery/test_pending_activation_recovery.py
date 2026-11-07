"""AT07 I5: the audited recovery, read beside an ``activate-pending`` half.

The I5 crash-window file drives the chain on two real daemons and reads the
call-boundary windows.  One shape cannot be reached from outside: the
land->activate boundary lives *inside* a single ``transfer.receive`` call
(``target.py`` begins the activate step, then calls the port), so a kill there
leaves ``activate-pending`` and no IPC entry can produce or advance it.  That
is also why the recovery reading needs both owners in one process: the target's
own landing rig (a real ``AgentRegistry``, a real exported bundle, the real P2
journal) is driven into the pending half by a port that dies where a killed
process dies, while the source's own daemon is composed for real -- the C2
custody hold, the owner-authenticated ``transfer.custody.override``, and the
``transfer.resume`` the chain's rollback calls.

What is measured is the operator's only safe move at this shape -- release the
source through the audited entry, resume it, leave the target half exactly
where it is -- plus the invariant the card names: exactly one live worker.

**NOT tested here**: two real machines, a real worker, strict resume (NOT_RUN),
and the same reading at the two-daemon venue (the pending half cannot be
produced there).  The port below dies exactly where ``target.py`` re-raises
``KeyboardInterrupt`` before its undo and before its attention flag, so the
durable row is the one a signal leaves.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.transfer.p2.orchestration.target import TargetLandingError
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _close_application,
)
from tests.integration.transfer.p2.target.test_target_landing import (
    ACTOR,
    OWNER,
    _KilledActivation,
    _activation,
    _bundle,
    _land,
    _registry,
    _snapshot,
    _source_home,
    _store,
    _target_binding,
)
from tests.integration.transfer.p2.test_override import _compose, _held_source

OPERATION = "op-1"
SOURCE_WORKER = "worker"


def _pending_half(tmp_path):
    """Drive the target's own rig until the half is ``activate-pending``.

    Returns the rig, the durable row, and the port -- the port is returned so
    the assertions can read that nothing was undone by the crash or by the
    recovery.
    """

    root = tmp_path / "target"
    root.mkdir()
    home = _source_home(root)
    before = _snapshot(home)
    bundle_dir = _bundle(root, home)
    registry = _registry(root)
    store = _store(root)
    activation = _KilledActivation(_activation(registry, root))
    store.prepare(OPERATION, _target_binding())
    with pytest.raises(KeyboardInterrupt):
        _land(root, registry, store, bundle_dir, activation=activation)
    return registry, store, activation, root, bundle_dir, home, before


def test_the_audited_recovery_releases_the_source_and_leaves_the_half_pending(
    tmp_path,
) -> None:
    registry, store, activation, root, bundle_dir, home, before = _pending_half(tmp_path)
    try:
        half = store.journal.get(OPERATION)
        assert (half.step, half.phase, half.generation) == ("activate", "activate-pending", 2)
        # A kill is not a handled failure: nothing was undone and nothing was
        # flagged, which is exactly why the half cannot advance on its own.
        assert half.needs_attention is False
        assert activation.deactivated == []
        assert registry.exists(ACTOR) is True

        # Re-entry is refused by name and writes nothing: the landing stands.
        with pytest.raises(TargetLandingError) as refusal:
            _land(
                root,
                registry,
                store,
                bundle_dir,
                staging=root / "staging" / "landing-2",
            )
        assert refusal.value.code == "activate-unresolved"
        assert store.journal.get(OPERATION) == half
        assert _snapshot(home) == before

        # The source's own daemon, with a real hold armed by a real quiesce.
        source_root = tmp_path / "source-daemon"
        source_root.mkdir()
        app, launcher, _harnesses, _inbox = _compose(source_root)
        try:
            spec, agent, _fact = _held_source(app, launcher)
            with pytest.raises(DaemonRequestError) as held:
                app.handle("lifecycle.start", spec.to_json())
            assert held.value.code == "TRANSFER_STARTS_HELD"
            assert launcher.processes and launcher.processes[-1].running is False

            # The audited recovery, in the order the chain's rollback uses it.
            receipt = app.handle(
                "transfer.custody.override",
                {
                    "principal": OWNER,
                    "actor": SOURCE_WORKER,
                    "operationId": "move",
                    "reason": "the target half is pending; release the source",
                },
            )
            assert receipt["ok"] and receipt["holdReleased"]
            assert receipt["custody"] == "held-overridden"
            events = app._transfer_p2._store.journal.events("move")
            assert [(event.principal, event.action) for event in events] == [
                (OWNER, "transfer-custody-override")
            ]
            resumed = app.handle("transfer.resume", {"spec": spec.to_json()})
            assert resumed["ok"] is True

            # Anti-dual-active: exactly one live worker, and it is the source's.
            # The target half was never promoted -- the recovery touched the
            # source only.
            live = [process for process in launcher.processes if process.running]
            assert len(live) == 1, launcher.processes
            assert app.agents.require(SOURCE_WORKER).entity_token == agent.entity_token
            assert store.journal.get(OPERATION) == half
            assert activation.deactivated == []
            assert registry.exists(ACTOR) is True
        finally:
            _close_application(app)
    finally:
        registry.close()
