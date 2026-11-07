"""P24 T08/T15-T18: per-agent migration, failure, and enablement gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import (
    AgentConfig,
    AgentRegistry,
)
from hyprial.identity.impl.agents.migration._base import (
    AgentMigrationAuthorization,
    MigrationEntry,
    SupportKey,
    SupportMatrix,
    SupportRow,
)
from hyprial.identity.impl.agents.migration.coordinator import (
    AgentMigrationCoordinator,
    )


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "process-home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "process-state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="owner",
        machine="node",
        hyprial_home=tmp_path / "hyprial-home",
    )


def _support_key(harness: str = "codex") -> SupportKey:
    return SupportKey(harness, "test-version", "darwin", "headless", "file")


def _support(*rows: SupportRow) -> SupportMatrix:
    return SupportMatrix(tuple(sorted(rows, key=lambda row: row.key)))


class _Bindings:
    def __init__(self, registry: AgentRegistry, *, legacy_in_use: bool = False) -> None:
        self.registry = registry
        self.legacy_in_use = legacy_in_use
        self.events: list[str] = []

    def preflight(self, plan) -> None:  # noqa: ANN001
        self.events.append("preflight")

    def activate(self, plan) -> None:  # noqa: ANN001
        self.events.append("activate")
        target = Path(self.registry.home_receipt(plan.authorization.actor).path) / "config"
        self.registry.update(plan.authorization.actor, config=AgentConfig(str(target)))

    def rollback(self, plan) -> None:  # noqa: ANN001
        self.events.append("rollback")
        self.registry.update(plan.authorization.actor, config=None)

    def legacy_sources_in_use(self, plan) -> tuple[str, ...]:  # noqa: ANN001
        self.events.append("source-check")
        if not self.legacy_in_use:
            return ()
        return tuple(item.entry.source for item in plan.entries)


def _authorization(registry: AgentRegistry, actor: str):
    agent = registry.require(actor)
    receipt = registry.home_receipt(actor)
    return AgentMigrationAuthorization(
        actor,
        agent.entity_token,
        receipt.resource_token,
        "authorized-window-1",
        "operator@example.test",
        2_000,
    )


def _coordinator(
    registry: AgentRegistry,
    matrix: SupportMatrix | None = None,
    *,
    stopped: bool | None = True,
    copy_file=None,  # noqa: ANN001
) -> AgentMigrationCoordinator:
    return AgentMigrationCoordinator(
        registry,
        matrix or _support(),
        liveness_probe=lambda _uri, _token: stopped,
        clock_ms=lambda: 1_000,
        copy_file=copy_file,
    )


def _source(tmp_path: Path, *, secret: str = "NON_PRODUCTION_SENTINEL") -> Path:
    source = tmp_path / "legacy-config"
    source.mkdir(mode=0o700, parents=True)
    native = source / "native" / "codex"
    native.mkdir(mode=0o700, parents=True)
    (native / "settings.json").write_text(
        json.dumps({"sentinel": secret}) + "\n", encoding="utf-8"
    )
    (native / "settings.json").chmod(0o600)
    skill = source / "shared" / "skills" / "portable"
    skill.mkdir(mode=0o700, parents=True)
    for directory in (
        source / "native",
        native,
        source / "shared",
        source / "shared" / "skills",
        skill,
    ):
        directory.chmod(0o700)
    (skill / "SKILL.md").write_text("test skill\n", encoding="utf-8")
    (skill / "SKILL.md").chmod(0o600)
    return source


def _plan(
    tmp_path: Path,
    registry: AgentRegistry,
    bindings: _Bindings,
    *,
    stopped: bool | None = True,
    matrix: SupportMatrix | None = None,
    required: tuple[SupportKey, ...] = (),
):
    source = _source(tmp_path)
    coordinator = _coordinator(registry, matrix, stopped=stopped)
    plan = coordinator.preflight(
        _authorization(registry, "worker"),
        (MigrationEntry(str(source), "config", False),),
        required_support=required,
        bindings=bindings,
    )
    return coordinator, plan, source
