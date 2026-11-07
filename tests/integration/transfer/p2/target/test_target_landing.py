"""AT07 I2: the target-side landing driver.

The unit below drives ``land_and_activate`` against a real ``AgentRegistry``,
a real exported bundle, and the real P2 ledger, so every assertion is about a
durable row or a file on disk rather than about a mock's call log.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.identity.impl.agents.registry.config_binding import (
    AgentConfigCasMismatch,
    AgentIncarnationMismatch,
)
from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
from hyprial.daemon.impl.transfer.execution.sandbox_smoke import (
    SMOKE_FAIL,
    SMOKE_NOT_RUN,
    SMOKE_PASS,
)
from hyprial.daemon.impl.transfer.landing.owner import LandingNameTaken
from hyprial.daemon.impl.transfer.p2.model import Binding, Role
from hyprial.daemon.impl.transfer.p2.orchestration.target import (
    ActivationError,
    ActivationReceipt,
    RuntimeContextActivation,
    TargetLandingError,
    land_and_activate,
)
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore
from tests.support.transfer.rehearsal import UNSIGNED_REHEARSAL
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE

OWNER = "alice"
MACHINE = "server"
SOURCE_NODE = "laptop"
ACTOR = "wk"
CREATED_AT = "2026-09-30T23:30:00+08:00"
PLAN_DIGEST = "a" * 64
GRANT = json.dumps({"capability": "see-actors", "scope": json.dumps(["user:alice"])})


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "source"
    for name in ("state", "config", "secrets", "workspace"):
        (home / name).mkdir(parents=True)
    (home / "state" / "home-receipt.json").write_text(
        json.dumps({"actor": ACTOR, "path": "/laptop/agents/wk", "resource": "src"})
    )
    native = home / "config" / "native" / "pi"
    native.mkdir(parents=True)
    (native / "settings.txt").write_text("pi-setting\n", encoding="utf-8")
    (home / "state" / "sessions.json").write_text('{"session": 1}\n')
    (home / "secrets" / "token.txt").write_text("s3cret\n")
    return home


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _bundle(tmp_path: Path, home: Path, *, grants: tuple[str, ...] = (GRANT,)) -> Path:
    bundle_dir = tmp_path / "bundle"
    export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine=SOURCE_NODE,
        created_at=CREATED_AT,
        grants=grants,
        credential_envelope="",
    )
    return bundle_dir


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(
        tmp_path / "agents.sqlite3", owner=OWNER, machine=MACHINE, hyprial_home=tmp_path
    )


def _store(tmp_path: Path) -> AdmissionStore:
    return AdmissionStore(
        StateDatabase(tmp_path / "lifecycle-operations.sqlite3"),
        node=MACHINE,
        owner=OWNER,
        clock_ms=lambda: 1000,
    )


def _target_binding() -> Binding:
    return Binding(
        role=Role.TARGET,
        owner=OWNER,
        source_node=SOURCE_NODE,
        source_agent_id=ACTOR,
        source_entity_token="source-incarnation",
        target_node=MACHINE,
        target_actor=ACTOR,
        plan_digest=PLAN_DIGEST,
    )


def _activation(registry: AgentRegistry, tmp_path: Path) -> RuntimeContextActivation:
    return RuntimeContextActivation(
        registry,
        harnesses=("pi",),
        tool_profile=TEST_TOOL_PROFILE,
        base_environment={"PATH": "/usr/bin:/bin", "HOME": "/ambient/home"},
        cwd=str(tmp_path / "workspace"),
    )


@dataclass(frozen=True)
class _Smoke:
    status: str = SMOKE_PASS
    harness: str = "pi"
    detail: str = "stub smoke"


def _land(tmp_path: Path, registry: AgentRegistry, store: AdmissionStore, bundle_dir: Path, **overrides):
    arguments: dict[str, object] = {
        "sandbox_smoke": lambda: _Smoke(),
        "store": store,
        "bundle_dir": bundle_dir,
        "registry": registry,
        "home_root": tmp_path / "agents",
        "staging": tmp_path / "staging" / "landing-1",
        "granted_by": f"user:{OWNER}",
        "activation": _activation(registry, tmp_path),
        "signature": UNSIGNED_REHEARSAL,
    }
    arguments.update(overrides)
    return land_and_activate("op-1", **arguments)


def test_land_then_activate_records_both_steps_and_leaves_the_source_alone(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    before = _snapshot(home)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())
        result = _land(tmp_path, registry, store, bundle_dir)

        assert result.active
        assert result.phases == ("landed", "active")
        assert result.actor == ACTOR
        assert result.harnesses == ("pi",)
        assert result.replayed is False
        assert len(result.grants) == 1

        state = store.journal.get("op-1")
        assert state.phase == "active"
        assert state.receipt == f"active:pi:{result.files}:{result.bytes}"

        recorded = registry.capability_grants(ACTOR)
        assert [item.capability for item in recorded] == ["see-actors"]
        assert tuple(item.grant_id for item in recorded) == result.grants

        landed = registry.require(ACTOR)
        assert landed.config is not None
        assert landed.config.source == str(tmp_path / "agents" / ACTOR / "config")
        assert (tmp_path / "agents" / ACTOR / "config" / "native" / "pi" / "settings.txt").exists()
        assert _snapshot(home) == before
    finally:
        registry.close()


def test_activation_reports_a_migration_notice_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of f98c63e248, R4: the receive path must surface the one-shot
    migration notice through the injected reporter, exactly once, and never
    again on a repeat activation."""

    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()
    (owner_home / ".gitconfig").write_text(
        "[user]\n\tname = Receiver Owner\n\temail = receiver@example.test\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(owner_home))
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())
        assert _land(tmp_path, registry, store, bundle_dir).active

        reported: list[object] = []

        def report(context: object) -> None:
            reported.extend(getattr(context, "notices", ()))

        entity_token = registry.require(ACTOR).entity_token
        activation = RuntimeContextActivation(
            registry,
            harnesses=("pi",),
            tool_profile=None,
            base_environment={"PATH": "/usr/bin:/bin", "HOME": str(owner_home)},
            cwd=str(tmp_path / "workspace"),
            notice_reporter=report,
        )
        receipt = activation.activate(ACTOR, entity_token)
        assert receipt.harnesses == ("pi",)
        assert [notice.code for notice in reported] == ["AGENT_GIT_PROFILE_MIGRATED"]

        reported.clear()
        activation.activate(ACTOR, entity_token)
        assert reported == []
    finally:
        registry.close()


def test_a_taken_name_refuses_and_the_operation_is_not_silently_retryable(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    before = _snapshot(home)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())
        resident = registry.create(ACTOR)

        with pytest.raises(LandingNameTaken):
            _land(tmp_path, registry, store, bundle_dir)

        state = store.journal.get("op-1")
        assert state.phase == "land-pending"
        assert state.needs_attention is True
        assert store.journal.events("op-1")[-1].action == "land-failed"
        assert registry.require(ACTOR).entity_token == resident.entity_token
        assert _snapshot(home) == before
    finally:
        registry.close()


class _RefusingActivation:
    """A context switch that refuses after the row already exists."""

    def __init__(self) -> None:
        self.deactivated: list[tuple[str, str]] = []

    def activate(self, actor: str, entity_token: str) -> ActivationReceipt:
        raise ActivationError("runtime-context-unavailable", "no usable pi context")

    def deactivate(self, actor: str, entity_token: str) -> None:
        self.deactivated.append((actor, entity_token))


def test_a_failed_activation_undoes_the_landing_and_stays_visible(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    before = _snapshot(home)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    activation = _RefusingActivation()
    try:
        store.prepare("op-1", _target_binding())
        with pytest.raises(TargetLandingError) as refusal:
            _land(tmp_path, registry, store, bundle_dir, activation=activation)

        assert refusal.value.code == "activation-failed"
        assert activation.deactivated == [(ACTOR, store.journal.get("op-1").entity_token)]
        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "agents" / ACTOR).exists()

        state = store.journal.get("op-1")
        assert state.phase == "activate-pending"
        assert state.needs_attention is True
        assert store.journal.events("op-1")[-1].action == "activate-failed"
        assert _snapshot(home) == before
    finally:
        registry.close()


def test_replaying_an_accepted_operation_does_not_land_again(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())
        first = _land(tmp_path, registry, store, bundle_dir)
        second = _land(tmp_path, registry, store, bundle_dir)

        assert first.replayed is False
        assert second.replayed is True
        assert second.phases == ("landed", "active")
        # The replay reports the recorded outcome, not invented zeros.
        assert (second.files, second.bytes) == (first.files, first.bytes)
        assert second.harnesses == ("pi",)
        assert store.journal.get("op-1").generation == 2
    finally:
        registry.close()


def test_a_source_side_admission_cannot_drive_a_target_landing(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    source_store = AdmissionStore(
        StateDatabase(tmp_path / "source-operations.sqlite3"),
        node=SOURCE_NODE,
        owner=OWNER,
        clock_ms=lambda: 1000,
    )
    try:
        # A genuinely admissible admission -- but for the source side of the
        # transfer, so the target driver must refuse to drive it.
        source_binding = replace(_target_binding(), role=Role.SOURCE)
        source_store.prepare("op-1", source_binding)

        with pytest.raises(TargetLandingError) as refusal:
            _land(tmp_path, registry, source_store, bundle_dir)

        assert refusal.value.code == "not-a-target-operation"
        assert source_store.journal.get("op-1") is None
    finally:
        registry.close()


def test_activation_refuses_an_incarnation_that_is_not_the_landed_one(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())
        _land(tmp_path, registry, store, bundle_dir)

        with pytest.raises(ActivationError) as refusal:
            _activation(registry, tmp_path).activate(ACTOR, "some-other-incarnation")

        assert refusal.value.code == "incarnation-drift"
    finally:
        registry.close()


def test_a_landing_intent_without_a_result_is_not_an_activation_problem(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())
        # Exactly the state a crash between begin and finish leaves behind.
        store.journal.begin("op-1", step="land", fingerprint=PLAN_DIGEST, actor=ACTOR)

        with pytest.raises(TargetLandingError) as refusal:
            _land(tmp_path, registry, store, bundle_dir)

        assert refusal.value.code == "land-unresolved"
        assert store.journal.get("op-1").phase == "land-pending"
        assert registry.exists(ACTOR) is False
    finally:
        registry.close()


class _KilledActivation:
    """A context switch that dies where a killed process dies.

    The first call raises ``KeyboardInterrupt``, which ``target.py`` re-raises
    *before* it undoes the landing and before it flags attention -- so this is
    the durable state a signal leaves.  Later calls work, which is what the
    restarted daemon's port does.
    """

    def __init__(self, inner: RuntimeContextActivation) -> None:
        self.inner = inner
        self.crashed = False
        self.deactivated: list[tuple[str, str]] = []

    def activate(self, actor: str, entity_token: str) -> ActivationReceipt:
        if not self.crashed:
            self.crashed = True
            raise KeyboardInterrupt
        return self.inner.activate(actor, entity_token)

    def deactivate(self, actor: str, entity_token: str) -> None:
        self.deactivated.append((actor, entity_token))
        return self.inner.deactivate(actor, entity_token)


def test_a_killed_activation_stands_and_is_refused_by_name(tmp_path: Path) -> None:
    """The window a kill leaves *inside* the call: landed, activation begun.

    ``land_and_activate`` writes the activate row before it calls the port, so
    a kill there leaves ``activate-pending`` -- not a finished ``landed`` row --
    with nothing undone and nothing flagged.  Re-entry must not retry an
    unknown outcome, and it must say which unknown outcome it is: without the
    named refusal the same call died inside ``journal.begin`` as a raw
    ``AdmissionConflict(PHASE_CONFLICT)`` that ``receive.py`` cannot map.
    """

    home = _source_home(tmp_path)
    before = _snapshot(home)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    activation = _KilledActivation(_activation(registry, tmp_path))
    try:
        store.prepare("op-1", _target_binding())
        with pytest.raises(KeyboardInterrupt):
            _land(tmp_path, registry, store, bundle_dir, activation=activation)

        crashed = store.journal.get("op-1")
        assert (crashed.step, crashed.phase, crashed.generation) == (
            "activate",
            "activate-pending",
            2,
        )
        assert crashed.needs_attention is False
        assert [event.action for event in store.journal.events("op-1")] == []
        # The landing stands and the source is untouched: a kill is not a
        # failure to compensate.
        assert activation.deactivated == []
        assert registry.exists(ACTOR) is True
        assert _snapshot(home) == before

        with pytest.raises(TargetLandingError) as refusal:
            _land(
                tmp_path,
                registry,
                store,
                bundle_dir,
                staging=tmp_path / "staging" / "landing-2",
            )

        assert refusal.value.code == "activate-unresolved"
        assert store.journal.get("op-1") == crashed
    finally:
        registry.close()


class _StubReceipt:
    def __init__(self, path: Path) -> None:
        self.path = str(path)


class _StubAgent:
    def __init__(self, actor: str, home: Path, token: str) -> None:
        self.actor = actor
        self.uri = f"agent:{actor}"
        self.entity_token = token
        self.config = None
        self.cwd = str(home / "workspace")


class _StubRegistry:
    """A registry whose config write loses the race, deterministically."""

    owner = OWNER

    def __init__(self, home: Path, error: Exception) -> None:
        self._home = home
        self._error = error
        self.attempts = 0

    def require(self, actor: str) -> _StubAgent:
        return _StubAgent(actor, self._home, "landed-token")

    def get(self, actor: str) -> _StubAgent:
        return self.require(actor)

    def home_receipt(self, actor: str) -> _StubReceipt:
        return _StubReceipt(self._home)

    def bind_agent_config(self, actor: str, **arguments: object) -> None:
        self.attempts += 1
        raise self._error


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (AgentConfigCasMismatch("config moved"), "config-drift"),
        (AgentIncarnationMismatch("incarnation moved"), "incarnation-drift"),
    ],
)
def test_activation_uses_the_fenced_config_port(tmp_path: Path, error, code: str) -> None:
    home = tmp_path / "agents" / ACTOR
    (home / "config").mkdir(parents=True)
    registry = _StubRegistry(home, error)
    activation = RuntimeContextActivation(
        registry,
        harnesses=("pi",),
        tool_profile=TEST_TOOL_PROFILE,
        base_environment={"PATH": "/usr/bin:/bin"},
        cwd=str(tmp_path / "workspace"),
    )

    with pytest.raises(ActivationError) as refusal:
        activation.activate(ACTOR, "landed-token")

    assert refusal.value.code == code
    assert registry.attempts == 1


def test_deactivating_a_replaced_incarnation_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "agents" / ACTOR
    (home / "config").mkdir(parents=True)
    registry = _StubRegistry(home, AgentConfigCasMismatch("unused"))
    activation = RuntimeContextActivation(
        registry,
        harnesses=("pi",),
        tool_profile=TEST_TOOL_PROFILE,
        base_environment={"PATH": "/usr/bin:/bin"},
        cwd=str(tmp_path / "workspace"),
    )

    with pytest.raises(ActivationError) as refusal:
        activation.deactivate(ACTOR, "some-other-incarnation")

    assert refusal.value.code == "incarnation-drift"


def test_a_failing_sandbox_smoke_refuses_before_anything_exists(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    before = _snapshot(home)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())

        with pytest.raises(TargetLandingError) as refusal:
            _land(
                tmp_path,
                registry,
                store,
                bundle_dir,
                sandbox_smoke=lambda: _Smoke(status=SMOKE_FAIL, detail="write was allowed"),
            )

        assert refusal.value.code == "sandbox-smoke-failed"
        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "agents" / ACTOR).exists()
        # Nothing was journalled either: no step ran, so there is nothing to
        # resolve and nothing to undo.
        assert store.journal.get("op-1") is None
        assert _snapshot(home) == before
    finally:
        registry.close()


def test_a_not_run_smoke_is_reported_and_does_not_veto_by_itself(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())

        result = _land(
            tmp_path,
            registry,
            store,
            bundle_dir,
            sandbox_smoke=lambda: _Smoke(status=SMOKE_NOT_RUN, detail="no entry point"),
        )

        assert result.active is True
        assert result.sandbox_smoke == SMOKE_NOT_RUN
        assert result.to_json()["sandboxSmoke"] == SMOKE_NOT_RUN
    finally:
        registry.close()


def test_the_tui_entrypoint_is_recorded_and_activation_launches_nothing(
    tmp_path: Path,
) -> None:
    """S3 §3.1: activate positions a TUI; the daemon never starts it.

    The proof that nothing launched is the journal: only ``land`` and
    ``activate`` exist, and the outcome lives entirely in the record.  The
    target CLI starts the TUI later (``target_tui.drive_target_start``).
    """

    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())
        result = _land(tmp_path, registry, store, bundle_dir, entrypoint="tui")

        assert result.active is True
        assert result.entrypoint == "tui"
        assert result.to_json()["entrypoint"] == "tui"
        assert [state.step for state in store.journal.history("op-1")] == [
            "land",
            "activate",
        ]
        # No launch step is recorded and no interactive session exists: the
        # landed identity is positioned, not started.
        assert registry.get(ACTOR) is not None
    finally:
        registry.close()


def test_an_unknown_entrypoint_is_refused_before_any_row(tmp_path: Path) -> None:
    home = _source_home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())
        with pytest.raises(TargetLandingError) as refusal:
            _land(tmp_path, registry, store, bundle_dir, entrypoint="bogus")

        assert refusal.value.code == "invalid-entrypoint"
        assert store.journal.get("op-1") is None
    finally:
        registry.close()


def test_the_p2_driver_forwards_the_harness_that_gates_launch_wiring(
    tmp_path: Path,
) -> None:
    """F-a: the chain's harness reaches the landing authority.

    The bundle carries the source row's launch wiring; the driver must pass
    the harness it is landing so the authority can bind (or refuse) it.  A
    driver that dropped ``harness`` would land a mismatched bundle silently.
    """

    home = _source_home(tmp_path)
    bundle_dir = tmp_path / "bundle"
    export_bundle(
        home,
        bundle_dir,
        owner=OWNER,
        actor=ACTOR,
        source_machine=SOURCE_NODE,
        created_at=CREATED_AT,
        grants=(GRANT,),
        identity={
            "launch": {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "harness": "pi",
            }
        },
    )
    registry = _registry(tmp_path)
    store = _store(tmp_path)
    try:
        store.prepare("op-1", _target_binding())
        result = _land(tmp_path, registry, store, bundle_dir, harness="pi")

        assert result.harnesses == ("pi",)
        landed = registry.require(ACTOR)
        assert landed.provider == "deepseek"
        assert landed.model == "deepseek-flash"
        assert landed.preferred_harness == "pi"
    finally:
        registry.close()
