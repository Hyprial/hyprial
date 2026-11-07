"""AT07 I3-c: the target half of the chain is admitted and driven -- or refused.

The entry is the *existing* ``transfer.receive`` command, so the P2 branch adds
no new IPC surface; it is reached only when the caller brings chain facts, and
it is closed until a real two-daemon run proves the harness -- the same fence,
the same code and the same mode string the source side uses.

What it does when it is open is the point of the slice: it derives the target
binding from the caller's *one* fact set through ``chain_bindings`` (never a
second composition), opens the target admission in this daemon's own P2 ledger,
and then hands the bundle to the I2 driver.  Nothing here reads or writes the
source.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.transfer.p2.orchestration import receive as receive_module
from hyprial.daemon.impl.transfer.p2.actor import Command, Operation
from hyprial.daemon.impl.transfer.p2.model import Role
from hyprial.kernel import HarnessLaunchSpec
from tests.integration.transfer.p2.lift.test_source_quiesce_wiring import (
    PLAN_DIGEST,
    _app,
    _close_application,
    _prove,
    _unprove,
)


def _receive_params(
    operation_id: str, bundle_dir, *, source_node: str = "laptop", harness: str = "codex"
):
    return {
        "operationId": operation_id,
        "planDigest": PLAN_DIGEST,
        "sourceNode": source_node,
        "sourceActor": "worker",
        "sourceEntityToken": "source-incarnation",
        "targetActor": "worker",
        "bundleDir": str(bundle_dir),
        "spec": HarnessLaunchSpec(harness, "worker", True).to_json(),
    }


def test_an_unproven_target_refuses_before_any_row(tmp_path, monkeypatch) -> None:
    """The reachability fence on the target side: refuse, admit nothing.

    As on the source side, the example's own row is removed in-process
    (``_unprove``), so the arm does not depend on which rows production
    carries.
    """

    _unprove(monkeypatch)
    app, _launcher = _app(tmp_path)
    try:
        with pytest.raises(DaemonRequestError) as refused:
            # The bundle dir does not exist: the refusal must come first.
            app.handle(
                "transfer.receive",
                _receive_params("op-closed", tmp_path / "none", harness="claude"),
            )

        assert refused.value.code == "TRANSFER_UNSUPPORTED_HARNESS"
        assert refused.value.data["mode"] == "agent-home-p2"
        assert refused.value.data["harness"] == "claude"
        assert app._transfer_p2.submit(Command(Operation.GET, "op-closed")).result(5) is None
    finally:
        _close_application(app)


def test_a_proven_target_admits_its_half_then_drives_the_driver(
    tmp_path, monkeypatch
) -> None:
    _prove(monkeypatch)
    seen: dict[str, object] = {}

    class _Landing:
        actor = "worker"
        phases = ("landed", "active")

        def to_json(self) -> dict[str, object]:
            return {"actor": "worker", "phases": ["landed", "active"]}

    def _fake_land(operation_id, **arguments):
        seen["operation_id"] = operation_id
        seen.update(arguments)
        # A real driver records land then activate; the double records the same
        # two steps so the response carries a report the source side can parse.
        store = arguments["store"]
        start = store.journal.begin(
            operation_id, step="land", fingerprint=PLAN_DIGEST, actor="worker"
        )
        store.journal.finish(
            operation_id, expected=start.state, receipt="landed", entity_token="target-token"
        )
        second = store.journal.begin(
            operation_id, step="activate", fingerprint="landed", actor="worker"
        )
        store.journal.finish(
            operation_id, expected=second.state, receipt="active", entity_token="target-token"
        )
        return _Landing()

    monkeypatch.setattr(receive_module, "land_and_activate", _fake_land)
    app, _launcher = _app(tmp_path)
    try:
        payload = app.handle(
            "transfer.receive", _receive_params("op-p2", tmp_path / "bundle")
        )

        assert payload["ok"] is True
        assert payload["chain"]["operationId"] == "op-p2"
        # The response carries the target's own report of its half, in exactly
        # the projection the source side parses.
        from hyprial.daemon.impl.transfer.p2.orchestration.lift import (
            half_from_json,
        )

        shipped = half_from_json(payload["chain"]["report"], operation_id="op-p2")
        assert shipped.row.phase == "active"
        assert shipped.row.step == "activate"
        assert shipped.binding.plan_digest == PLAN_DIGEST
        assert shipped.binding.target_node == "node"
        assert seen["operation_id"] == "op-p2"
        assert seen["expected_actor"] == "worker"
        assert str(seen["bundle_dir"]) == str(tmp_path / "bundle")

        # The admission exists, and it is the *derived* target half: the same
        # facts the source sent, with the roles swapped and this node as target.
        record = app._transfer_p2.submit(Command(Operation.GET, "op-p2")).result(5)
        assert record is not None
        binding = record.binding
        assert binding.role is Role.TARGET
        assert binding.owner == "alice"
        assert binding.source_node == "laptop"
        assert binding.source_agent_id == "worker"
        assert binding.source_entity_token == "source-incarnation"
        assert binding.target_node == "node"
        assert binding.target_actor == "worker"
        assert binding.plan_digest == PLAN_DIGEST
    finally:
        _close_application(app)


def test_the_resolved_cwd_reaches_the_row_and_the_context(tmp_path, monkeypatch) -> None:
    """B7: the operator resolved a target cwd; the landing must not drop it.

    The P0 receive hands ``spec.cwd`` to the row it mints; the chain used to
    pass it and echo it while the activation silently fell back to a row whose
    cwd was ``None``.  The value is now carried into both.
    """

    _prove(monkeypatch)
    seen: dict[str, object] = {}
    work = tmp_path / "work"

    class _Landing:
        actor = "worker"
        phases = ("landed", "active")

        def to_json(self) -> dict[str, object]:
            return {"actor": "worker", "phases": ["landed", "active"]}

    def _fake_land(operation_id, **arguments):
        seen.update(arguments)
        store = arguments["store"]
        start = store.journal.begin(
            operation_id, step="land", fingerprint=PLAN_DIGEST, actor="worker"
        )
        store.journal.finish(
            operation_id, expected=start.state, receipt="landed", entity_token="target-token"
        )
        second = store.journal.begin(
            operation_id, step="activate", fingerprint="landed", actor="worker"
        )
        store.journal.finish(
            operation_id, expected=second.state, receipt="active", entity_token="target-token"
        )
        return _Landing()

    monkeypatch.setattr(receive_module, "land_and_activate", _fake_land)
    app, _launcher = _app(tmp_path)
    try:
        params = _receive_params("op-cwd", tmp_path / "bundle")
        params["spec"] = HarnessLaunchSpec(
            "codex", "worker", True, cwd=str(work)
        ).to_json()
        payload = app.handle("transfer.receive", params)

        assert payload["ok"] is True
        assert seen["cwd"] == str(work)
        assert seen["activation"].cwd == str(work)
    finally:
        _close_application(app)


def test_the_source_adapters_are_re_pinned_on_the_landed_actor(
    tmp_path, monkeypatch
) -> None:
    """B1: the pins the source sent are applied here, not dropped.

    The landing mints the row, so the pin's foreign key resolves; the pinned
    URI is this daemon's own, never a string the caller spelled.
    """

    _prove(monkeypatch)
    seen: dict[str, object] = {}

    class _Landing:
        actor = "worker"
        phases = ("landed", "active")

        def to_json(self) -> dict[str, object]:
            return {"actor": "worker", "phases": ["landed", "active"]}

    def _fake_land(operation_id, **arguments):
        seen.update(arguments)
        arguments["registry"].create("worker", cwd=arguments["cwd"])
        store = arguments["store"]
        start = store.journal.begin(
            operation_id, step="land", fingerprint=PLAN_DIGEST, actor="worker"
        )
        store.journal.finish(
            operation_id, expected=start.state, receipt="landed", entity_token="target-token"
        )
        second = store.journal.begin(
            operation_id, step="activate", fingerprint="landed", actor="worker"
        )
        store.journal.finish(
            operation_id, expected=second.state, receipt="active", entity_token="target-token"
        )
        return _Landing()

    monkeypatch.setattr(receive_module, "land_and_activate", _fake_land)
    app, _launcher = _app(tmp_path)
    try:
        registry = app._agent_registry
        params = _receive_params("op-pins", tmp_path / "bundle")
        # One adapter per agent is the schema's rule; a worker carries at most
        # one pin, so the arm is one adapter, not a list.
        params["pins"] = ["slack"]
        payload = app.handle("transfer.receive", params)

        assert payload["ok"] is True
        assert registry.pins() == {"slack": registry.uri_for("worker")}
    finally:
        _close_application(app)


def test_an_adapter_held_elsewhere_refuses_before_anything_is_recorded(
    tmp_path, monkeypatch
) -> None:
    """B1's negative arm: a re-pin is a MOVE, so the holder is named and kept.

    The refusal is a bad-request refusal: it comes before this daemon records
    its own half, and the holder's pin is untouched.
    """

    _prove(monkeypatch)
    app, _launcher = _app(tmp_path)
    try:
        registry = app._agent_registry
        registry.create("holder")
        registry.pin("slack", "holder")
        held = {"slack": registry.uri_for("holder")}

        def _explode(*_args, **_kwargs):
            raise AssertionError("the driver must never run for a pin conflict")

        monkeypatch.setattr(receive_module, "land_and_activate", _explode)
        params = _receive_params("op-pin-conflict", tmp_path / "bundle")
        params["pins"] = ["slack"]
        with pytest.raises(DaemonRequestError) as refused:
            app.handle("transfer.receive", params)

        assert refused.value.code == "TRANSFER_PIN_CONFLICT"
        assert "holder" in str(refused.value)
        assert registry.pins() == held
        assert app._transfer_p2.submit(Command(Operation.GET, "op-pin-conflict")).result(5) is None
    finally:
        _close_application(app)


def test_the_source_node_cannot_be_this_node(tmp_path, monkeypatch) -> None:
    """One node id on both ends is the dual-active shape; refused before a row."""

    _prove(monkeypatch)
    app, _launcher = _app(tmp_path)
    try:
        with pytest.raises(DaemonRequestError) as refused:
            app.handle(
                "transfer.receive",
                _receive_params("op-bad", tmp_path / "bundle", source_node="node"),
            )

        assert refused.value.code == "INVALID_ARGUMENT"
        assert app._transfer_p2.submit(Command(Operation.GET, "op-bad")).result(5) is None
    finally:
        _close_application(app)
