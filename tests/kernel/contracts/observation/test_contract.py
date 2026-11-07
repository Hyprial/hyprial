"""The monitor port carries immutable values, not executable callbacks."""

import ast
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from hyprial.kernel import (
    LifecycleCursor, LifecycleEvent, LifecycleObservationPort, LifecycleSnapshot,
    LifecycleTarget, ObservationBatch, ObservationSubscription,
)


def test_values_are_frozen_and_read_port_has_no_execution_operations():
    target = LifecycleTarget("host", "harness.lifecycle", "canonical/incarnation")
    with pytest.raises(FrozenInstanceError):
        target.key = "replacement"
    for value in (LifecycleTarget, LifecycleCursor, LifecycleEvent, LifecycleSnapshot,
                  ObservationBatch, ObservationSubscription):
        assert fields(value)
        assert all("Callable" not in str(field.type) for field in fields(value))
    assert not any(hasattr(LifecycleObservationPort, name) for name in (
        "spawn", "send", "receive", "link", "trap_exit", "register_handler", "publish",
    ))


def test_new_observation_modules_have_no_mailbox_or_callback_registration():
    root = Path(__file__).resolve().parents[4]
    paths = list((root / "src/hyprial/kernel/impl/observation").glob("*.py"))
    paths += list((root / "src/hyprial/daemon/impl/lifecycle/observation").glob("*.py"))
    assert paths
    forbidden = {"ActorSpec", "ActorRuntime", "Queue", "Thread", "Timer", "Callable",
                 "handler_factory", "event_sink", "register_handler", "trap_exit", "link"}
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in forbidden, (path, node.id)
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, (path, node.attr)
            if isinstance(node, ast.alias):
                assert node.name not in forbidden, (path, node.name)
            if isinstance(node, ast.keyword):
                assert node.arg not in forbidden, (path, node.arg)
