"""AT07 I3-c: `transfer.quiesce` reaches the P2 chain only when it is asked to.

The P2 facts are additive, and the branch they open is **closed by default**:
it records a durable custody hold and stops the worker through it, which is a
capability the source does not have until a real two-daemon run proves the
harness.  Without the facts the call is the P0 path it has always been and no
ledger is touched; with them but without a proven (harness, mode) row it refuses
with the P0 refusal's own code and mode string.
"""

from __future__ import annotations

import threading

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.composition import (
    AgentIdentityProjectionView,
    CorrelatedDomainEvents,
    HarnessPortClient,
)
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.transfer.p2.orchestration import lift as lift_module
from hyprial.daemon.impl.transfer.p2.actor import Command, Operation
from hyprial.daemon.impl.transfer.p2.model import Role
from hyprial.kernel import HarnessLaunchSpec
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _Inbox as _BaseInbox,
)
from tests.daemon.application.composition.composition.test_final_composition_lifecycle import (
    _LocalDelivery,
    _Presence,
    _Transport,
    _close_application,
    _isolated_application,
)

PLAN_DIGEST = "a" * 64


class _Inbox(_BaseInbox):
    """The shared double plus the one read the transfer payload makes."""

    def pending_messages(self, _key: str) -> list[object]:
        return []


class Process:
    pid = None

    def __init__(self) -> None:
        self.running = True
        self.stopping = threading.Event()
        self.release = threading.Event()
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1
        self.stopping.set()
        assert self.release.wait(5), "test did not release process stop"
        self.running = False


class Launcher:
    def __init__(self) -> None:
        self.processes: list[Process] = []

    def start(self, _spec):
        process = Process()
        self.processes.append(process)
        return process


def _app(tmp_path):
    app = _isolated_application(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node",
        owner="alice",
        network_isolated=True,
    )
    launcher = Launcher()
    events = CorrelatedDomainEvents()
    actor = HarnessRuntimeActor(
        launcher,
        event_sink=events,
        desired_state=app.desired_state,
        agent_identity=AgentIdentityProjectionView(app._agent_session_domains),
        restart_backoff_seconds=0,
    )
    harnesses = HarnessPortClient(actor, events)
    app._harnesses, app._inbox, app._presence = harnesses, _Inbox(), _Presence()
    app._start_lifecycle_manager(
        transport=_Transport(),
        inbox=app._inbox,
        local_delivery=_LocalDelivery(),
        harnesses=harnesses,
    )
    return app, launcher


def _started(app, launcher, harness: str = "codex"):
    spec = HarnessLaunchSpec(harness, "worker", True)
    app.handle("lifecycle.start", spec.to_json())
    return launcher.processes[0]


def _p2_params(
    operation_id: str, *, target_node: str = "server", provider: str = "codex"
) -> dict[str, object]:
    return {
        "operationId": operation_id,
        "name": "worker",
        "provider": provider,
        "targetNode": target_node,
        "targetActor": "worker",
        "planDigest": PLAN_DIGEST,
    }


def _prove(monkeypatch, harness: str = "codex") -> None:
    """A test-only row, for a harness the production table may not name.

    ``chain_is_proven`` reads the table from the ``lift`` module itself, so the
    double has to land there -- patching the package would not be read.  Used
    with ``codex`` on purpose: the production rows are the ones a real
    two-daemon run proved, and this test keeps an unproven harness's fence
    closed without depending on which rows production happens to carry.
    """

    monkeypatch.setattr(
        lift_module,
        "PROVEN_CHAINS",
        (lift_module.ProvenChain(harness, lift_module.P2_CHAIN_MODE, "test-only"),),
    )


def _unprove(monkeypatch, harness: str = "claude") -> None:
    """The table without ``harness``'s own row: the fence must refuse it.

    ``chain_is_proven`` reads the table from the ``lift`` module itself, so the
    double has to land there -- patching the package would not be read.  With
    every production harness now proven, the unproven arm is pinned by removing
    the example's row in-process (the inverse of ``_prove``) rather than by
    naming a harness the production table happens to lack.
    """

    monkeypatch.setattr(
        lift_module,
        "PROVEN_CHAINS",
        tuple(row for row in lift_module.PROVEN_CHAINS if row.harness != harness),
    )


def test_a_plain_quiesce_opens_no_chain(tmp_path) -> None:
    """The negative control: the P0 path writes no admission, by construction."""

    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()

        payload = app.handle(
            "transfer.quiesce",
            {"operationId": "op-p0", "name": "worker", "provider": "codex"},
        )

        assert payload["stopped"] is True
        assert "chain" not in payload
        assert app._transfer_p2.submit(Command(Operation.GET, "op-p0")).result(5) is None
    finally:
        _close_application(app)


def test_the_chain_branch_is_closed_until_a_run_proves_it(tmp_path, monkeypatch) -> None:
    """The reachability fence: an IPC caller cannot open an unproven chain.

    The example's own row is removed in-process (``_unprove``): production now
    proves every harness it names, so the refusal is pinned to the row's
    absence rather than to which harnesses the table happens to carry.
    """

    _unprove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher, harness="claude")
        process.release.set()

        with pytest.raises(DaemonRequestError) as refused:
            app.handle("transfer.quiesce", _p2_params("op-closed", provider="claude"))

        # Same code and same mode string as the P0 refusal it stands in for.
        assert refused.value.code == "TRANSFER_UNSUPPORTED_HARNESS"
        assert refused.value.data["mode"] == "agent-home-p2"
        assert refused.value.data["harness"] == "claude"
        # Refused before anything: no admission, worker never stopped.
        assert app._transfer_p2.submit(Command(Operation.GET, "op-closed")).result(5) is None
        assert process.running is True
        assert process.stops == 0
    finally:
        process.release.set()
        _close_application(app)


def test_a_proven_harness_records_the_chain_and_stops_through_the_hold(
    tmp_path, monkeypatch
) -> None:
    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()

        payload = app.handle("transfer.quiesce", _p2_params("op-p2"))

        assert payload["stopped"] is True
        assert payload["chain"]["operationId"] == "op-p2"
        assert payload["chain"]["targetNode"] == "server"
        assert payload["chain"]["targetActor"] == "worker"
        assert payload["chain"]["quiesce"]["state"] == "quiesced"
        # The source half is durable on this daemon's ledger, before anything
        # else in the chain can be true.
        admission = app._transfer_p2.submit(Command(Operation.GET, "op-p2")).result(5)
        assert admission is not None
        assert admission.binding.role is Role.SOURCE
        assert admission.binding.source_entity_token
        assert process.running is False
        assert process.stops == 1
    finally:
        _close_application(app)


def test_the_same_node_on_both_ends_is_refused_before_any_row(tmp_path, monkeypatch) -> None:
    _prove(monkeypatch)
    app, launcher = _app(tmp_path)
    try:
        process = _started(app, launcher)
        process.release.set()

        with pytest.raises(DaemonRequestError) as refused:
            app.handle("transfer.quiesce", _p2_params("op-bad", target_node="node"))

        assert refused.value.code == "INVALID_ARGUMENT"
        # Refused *before* anything: no admission, and the worker is still up.
        assert app._transfer_p2.submit(Command(Operation.GET, "op-bad")).result(5) is None
        assert process.running is True
        assert process.stops == 0
    finally:
        process.release.set()
        _close_application(app)


def test_the_p2_mode_string_has_one_spelling() -> None:
    """The fence and the result it stands in for must name the same thing.

    ``P2_CHAIN_MODE`` is spelled once in the P2 package and once as a literal in
    the agent-home chain's payload (``agent_home_chain.py``, which the A1 split
    moved out of ``orchestrator.py``); the P0-side refusal in ``transfer.py``
    names the same constant rather than a second literal.  Nothing keeps the
    remaining literal equal except this assertion, and a silent drift would make
    the negative arm compare against a string the lift never sees.
    """

    from pathlib import Path

    from hyprial.daemon.impl.transfer.p2.orchestration.lift import P2_CHAIN_MODE

    root = Path(__file__).resolve().parents[5]
    chain = (root / "src/hyprial/daemon/impl/transfer/agent_home_chain.py").read_text()
    assert f'"mode": "{P2_CHAIN_MODE}"' in chain
    # The P0 orchestrator no longer repeats the literal at all; a fresh copy
    # there would be a second spelling, which is the drift this guards.
    orchestrator = (root / "src/hyprial/daemon/impl/transfer/orchestrator.py").read_text()
    assert f'"mode": "{P2_CHAIN_MODE}"' not in orchestrator


def test_the_fence_is_scoped_to_the_entrypoint(monkeypatch) -> None:
    """A proven row opens exactly its (harness, mode, entrypoint) tuple.

    The TUI entrypoint is a separate proof from headless: a row for one must
    never widen into the other, so each combination is asserted on its own.
    """

    monkeypatch.setattr(
        lift_module,
        "PROVEN_CHAINS",
        (
            lift_module.ProvenChain(
                "pi", lift_module.P2_CHAIN_MODE, "headless-row"
            ),
            lift_module.ProvenChain(
                "pi",
                lift_module.P2_CHAIN_MODE,
                "tui-row",
                lift_module.ENTRYPOINT_TUI,
            ),
        ),
    )

    assert lift_module.chain_is_proven("pi", lift_module.P2_CHAIN_MODE) is True
    assert lift_module.chain_is_proven(
        "pi", lift_module.P2_CHAIN_MODE, lift_module.ENTRYPOINT_HEADLESS
    ) is True
    assert lift_module.chain_is_proven(
        "pi", lift_module.P2_CHAIN_MODE, lift_module.ENTRYPOINT_TUI
    ) is True
    assert lift_module.chain_is_proven(
        "claude", lift_module.P2_CHAIN_MODE, lift_module.ENTRYPOINT_TUI
    ) is False


def test_a_row_without_entrypoint_proves_only_headless(monkeypatch) -> None:
    """缺字段 != 全通行: an old row defaults to headless, never to all."""

    monkeypatch.setattr(
        lift_module,
        "PROVEN_CHAINS",
        (lift_module.ProvenChain("pi", lift_module.P2_CHAIN_MODE, "old-row"),),
    )

    assert lift_module.chain_is_proven(
        "pi", lift_module.P2_CHAIN_MODE, lift_module.ENTRYPOINT_HEADLESS
    ) is True
    assert lift_module.chain_is_proven(
        "pi", lift_module.P2_CHAIN_MODE, lift_module.ENTRYPOINT_TUI
    ) is False
