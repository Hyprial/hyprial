"""agent_home_p2_migration: preflight scenarios."""

from __future__ import annotations
from pathlib import Path
import pytest
from hyprial.identity.impl.agents.migration._base import (
    AgentHomeMigrationError,
    MigrationEntry,
)
from hyprial.identity.impl.agents.migration.coordinator import (
    AgentMigrationCoordinator,
    )

from .support import (
    _Bindings,
    _authorization,
    _coordinator,
    _isolated_environment as _isolated_environment,
    _registry,
    _source,
    _support,
)


def test_t15_preflight_refuses_running_and_unknown_liveness_without_writes(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        bindings = _Bindings(registry)
        for stopped, category in ((False, "agent-running"), (None, "liveness-unknown")):
            source = _source(tmp_path / category)
            coordinator = _coordinator(registry, stopped=stopped)
            with pytest.raises(AgentHomeMigrationError) as refused:
                coordinator.preflight(
                    _authorization(registry, "worker"),
                    (MigrationEntry(str(source), "config", False),),
                    bindings=bindings,
                )
            assert refused.value.category == category
            assert source.exists()
            assert not any((Path(registry.home_receipt("worker").path) / "state").glob("migrations/*"))
    finally:
        registry.close()


def test_t15_liveness_is_reobserved_at_execute_not_replayed_from_plan(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        observed: list[tuple[str, str]] = []
        state: list[bool | None] = [True]

        def probe(uri: str, entity_token: str) -> bool | None:
            observed.append((uri, entity_token))
            return state[0]

        source = _source(tmp_path)
        coordinator = AgentMigrationCoordinator(
            registry,
            _support(),
            liveness_probe=probe,
            clock_ms=lambda: 1_000,
        )
        bindings = _Bindings(registry)
        plan = coordinator.preflight(
            _authorization(registry, "worker"),
            (MigrationEntry(str(source), "config", False),),
            bindings=bindings,
        )
        state[0] = None
        with pytest.raises(AgentHomeMigrationError) as refused:
            coordinator.execute(plan, bindings=bindings)
        assert refused.value.category == "liveness-unknown"
        assert len(observed) == 2
        assert observed[0] == observed[1]
        assert source.exists()
        assert bindings.events == ["preflight"]
    finally:
        registry.close()


def test_t17_liveness_probe_exception_is_visible_unknown_not_false_or_absent(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")

        def broken_probe(_uri: str, _entity_token: str) -> bool:
            raise OSError("injected observation failure")

        coordinator = AgentMigrationCoordinator(
            registry,
            _support(),
            liveness_probe=broken_probe,
            clock_ms=lambda: 1_000,
        )
        source = _source(tmp_path)
        with pytest.raises(AgentHomeMigrationError) as refused:
            coordinator.preflight(
                _authorization(registry, "worker"),
                (MigrationEntry(str(source), "config", False),),
                bindings=_Bindings(registry),
            )
        assert refused.value.category == "liveness-unknown"
        assert source.exists()
    finally:
        registry.close()
