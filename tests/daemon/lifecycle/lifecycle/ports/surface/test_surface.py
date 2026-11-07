from __future__ import annotations

import inspect
from hyprial.identity.impl.agents import AgentRegistry
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from tests.support.gates.atomic_lifecycle_authority_gate import scan_agent_store, scan_composition, scan_harness_custody

def test_composition_has_no_pre_read_or_independent_lifecycle_journal() -> None:
    source = inspect.getsource(DaemonApplication._start_lifecycle_manager)
    assert scan_composition(source) == []


def test_reverse_mutation_pre_read_is_rejected() -> None:
    source = inspect.getsource(DaemonApplication._start_lifecycle_manager)
    mutated = source.replace(
        "router = CorrelationEventRouter()",
        "router = CorrelationEventRouter()\n        resource_exists = self.agents.get",
    )
    assert mutated != source
    assert scan_composition(mutated) == [
        "composition pre-reads or executes domain state: resource_exists"
    ]


def test_reverse_mutations_old_token_and_missing_compare_are_rejected() -> None:
    source = inspect.getsource(AgentRegistry.apply_lifecycle)
    assert scan_agent_store(source) == []
    old_token = source.replace(
        "if not actual_active:\n                        token = uuid.uuid4().hex",
        "if not actual_active:\n                        token = token",
    )
    no_compare = source.replace(
        "elif actual_active and (expected is None or token == expected):",
        "elif actual_active:",
    )
    assert scan_agent_store(old_token) == [
        "Agent create no longer rotates the prior tombstone token"
    ]
    assert scan_agent_store(no_compare) == [
        "Agent delete lost expected-resource-token compare fence"
    ]


def test_harness_reconcile_and_close_reverse_mutations_are_rejected() -> None:
    # The actor borrows its custody state through ``self`` from the behavior
    # mixins in its MRO, so the fences live across all of them; scanning only
    # the leaf class would miss the fences that moved into a mixin.
    source = "\n".join(
        inspect.getsource(base)
        for base in HarnessRuntimeActor.__mro__
        if base is not object
    )
    assert scan_harness_custody(source) == []
    no_start_fence = source.replace(
        "if key in self._lifecycle_effect_resources and not allow_lifecycle:",
        "if False:",
    )
    no_close_terminal = source.replace(
        "self._terminalize_incomplete_lifecycle(",
        "self._ignored_terminalize_incomplete_lifecycle(",
    )
    no_completion_fence = source.replace(
        "AdmissionResult.CLOSED\n                if self._closing",
        "AdmissionResult.ACCEPTED\n                if self._closing",
    )
    no_dedupe_bound = source.replace(
        "if len(self._settled_lifecycle_order) >= _LIFECYCLE_SETTLED_CAPACITY:",
        "if False:",
    )
    no_terminal_replay = source.replace(
        "if durable_receipt is not None and durable_receipt.completed:",
        "if False:",
    )
    evict_active_replay = source.replace(
        "if len(self._lifecycle_replay_claims) >= self._lifecycle_replay_capacity:",
        "if False:",
    )
    assert "Harness start lost central lifecycle custody fence" in scan_harness_custody(
        no_start_fence
    )
    assert (
        "Harness close no longer terminalizes incomplete lifecycle effects"
        in scan_harness_custody(no_close_terminal)
    )
    assert (
        "Harness completion admission lost its closing fence"
        in scan_harness_custody(no_completion_fence)
    )
    assert (
        "Harness settled lifecycle dedupe lost its capacity bound"
        in scan_harness_custody(no_dedupe_bound)
    )
    assert (
        "Harness terminal receipt replay was hidden by memory dedupe"
        in scan_harness_custody(no_terminal_replay)
    )
    assert (
        "Harness active replay claims lost capacity backpressure"
        in scan_harness_custody(evict_active_replay)
    )
