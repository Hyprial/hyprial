"""Injected native-runtime-home preparer port (IR-002) focus tests.

identity owns the home effect lane; the daemon owns the Claude/Codex native
preparers.  These tests pin the injected port contract and the lane wiring, and
guard against the lazy identity -> daemon import returning.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from hyprial.identity.impl.agents.home import effects as effects_module
from hyprial.identity.impl.agents.home.effects import (
    AgentHomeEffect,
    AgentHomeEffectLane,
    PrepareRuntimeHome,
    RuntimeHomePreparers,
)


@dataclass(frozen=True)
class _Context:
    harness: str
    authority_prepared: bool = False
    actor: str = "fixture-agent"


def test_runtime_home_preparers_is_a_mapping_and_skips_absent_harness() -> None:
    seen: list[_Context] = []
    preparers = RuntimeHomePreparers({"codex": seen.append, "claude": None})

    assert dict(preparers) == {"codex": seen.append}
    assert "pi" not in preparers
    assert len(preparers) == 1

    preparers.prepare(_Context(harness="pi"))
    assert seen == []  # no entry -> intentional no-op
    preparers.prepare(_Context(harness="codex"))
    assert [context.harness for context in seen] == ["codex"]

    empty = RuntimeHomePreparers()
    assert len(empty) == 0
    assert dict(empty) == {}


def test_home_effect_lane_runs_the_injected_runtime_preparer(
    monkeypatch,
) -> None:
    validated: list[object] = []

    monkeypatch.setattr(
        effects_module,
        "materialize_agent_runtime_context",
        lambda preparation: _Context(harness=preparation.harness),
    )

    class _Provisioner:
        def validate(self, receipt: object) -> None:
            validated.append(receipt)

    preparer_input: list[_Context] = []

    def preparer(context: _Context) -> None:
        preparer_input.append(context)

    lane = AgentHomeEffectLane(
        _Provisioner(),  # type: ignore[arg-type]
        complete=lambda _event: None,
        runtime_preparers={"codex": preparer},
    )
    receipt = object()
    try:
        completed = lane._execute(
            AgentHomeEffect(
                "runtime-home",
                1,
                PrepareRuntimeHome(
                    SimpleNamespace(harness="codex", home_receipt=receipt)
                ),
            )
        )
    finally:
        lane.close()

    assert completed.error_code is None and completed.settled
    assert completed.receipt is receipt
    assert validated == [receipt]
    assert [context.harness for context in preparer_input] == ["codex"]
    assert completed.runtime_context is not None
    assert completed.runtime_context.authority_prepared is True


def test_home_effect_lane_without_a_preparer_makes_no_daemon_import() -> None:
    for node in ast.walk(ast.parse(inspect.getsource(effects_module))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("hyprial.daemon")
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("hyprial.daemon")


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_native_home_requires_a_real_injected_preparer(harness) -> None:
    with pytest.raises(effects_module.AgentHomeError) as raised:
        RuntimeHomePreparers().prepare(_Context(harness=harness))
    assert raised.value.category == "runtime-preparer-missing"
    assert raised.value.actor == "fixture-agent"
    assert raised.value.phase == f"prepare-{harness}"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_missing_native_preparer_cannot_report_authority_prepared(
    harness, monkeypatch,
) -> None:
    monkeypatch.setattr(
        effects_module,
        "materialize_agent_runtime_context",
        lambda _preparation: _Context(harness=harness),
    )
    lane = AgentHomeEffectLane(
        SimpleNamespace(validate=lambda _receipt: None),
        complete=lambda _event: None,
    )
    try:
        completed = lane._execute(AgentHomeEffect(
            "missing-preparer", 1,
            PrepareRuntimeHome(SimpleNamespace(home_receipt=object())),
        ))
    finally:
        lane.close()
    assert completed.runtime_context is None
    assert completed.settled
    assert completed.error_code == "AGENT_ERROR"
    assert "runtime-preparer-missing" in completed.error_detail


def test_injected_preparer_failure_is_not_reported_as_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        effects_module,
        "materialize_agent_runtime_context",
        lambda _preparation: _Context(harness="codex"),
    )

    def fail(_context):
        raise OSError("native publication failed")

    lane = AgentHomeEffectLane(
        SimpleNamespace(validate=lambda _receipt: None),
        complete=lambda _event: None,
        runtime_preparers={"codex": fail},
    )
    try:
        completed = lane._execute(AgentHomeEffect(
            "failed-preparer", 1,
            PrepareRuntimeHome(SimpleNamespace(home_receipt=object())),
        ))
    finally:
        lane.close()
    assert completed.runtime_context is None
    assert not completed.settled
    assert completed.error_code == "OSError"
