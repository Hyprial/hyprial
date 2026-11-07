# CI flake fix (2026-11-03, workflow wf-ff332cef #implDF): harness actor
# settlement and shutdown deadlines were hand-coded 2.0s wall-clock bets in
# test_lifecycle.py and went red under CI load (runs 45211/45455: 3-CPU cgroup
# quota, host load ~9-10). Same class the CI workflow already declared the
# HYPRIAL_TEST_WAIT_SECONDS knob for on 2026-10-09 ("deadlines only bound the
# waits; assertions check state, not speed"). Sourced from the same knob so
# shard stability is stated in one place; assertions keep checking state.
from __future__ import annotations

import os
import subprocess
import sys
import time
import ast
import inspect
import textwrap
from collections import Counter
from dataclasses import dataclass
from typing import Any, get_args


from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.actor import (
    HarnessRuntimeActor,
    ProcessIoPort,
)
from hyprial.daemon.impl.harnesses.runtime.ports import (
    HarnessCommand,
)


@dataclass
class _Process:
    running: bool = True
    pid: int | None = None
    stops: int = 0

    def stop(self) -> None:
        self.stops += 1
        self.running = False


class _Launcher:
    def __init__(self) -> None:
        self.processes: list[_Process] = []

    def start(self, _spec: HarnessLaunchSpec) -> _Process:
        process = _Process()
        self.processes.append(process)
        return process


class _AgentIdentity:
    def __init__(self, token: str) -> None:
        self.token = token

    def entity_token(self, _actor: str) -> str | None:
        return self.token


def _spec(name: str = "worker") -> HarnessLaunchSpec:
    return HarnessLaunchSpec("codex", name, True)


WAIT_TIMEOUT_SECONDS = float(os.environ.get("HYPRIAL_TEST_WAIT_SECONDS", "5.0"))


def _wait_for(predicate: Any, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(0.01)






















def _dead_pid() -> int:
    """A PID that is positively gone (spawned, waited, never signalled)."""

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _stopping_port(identity_reader: Any) -> ProcessIoPort:
    return ProcessIoPort(
        _Launcher(),
        emit=lambda event: (PortAdmission.ACCEPTED, 0),
        delivery_failed=lambda event, error: None,
        generation_reader=lambda: 0,
        identity_reader=identity_reader,
    )
































def _handled_frozen_commands(source: str) -> Counter[type[object]]:
    tree = ast.parse(textwrap.dedent(source))
    handled: list[type[object]] = []
    by_name = {
        command_type.__name__: command_type for command_type in get_args(HarnessCommand)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "isinstance":
            continue
        if len(node.args) != 2 or not isinstance(node.args[1], ast.Name):
            continue
        command_type = by_name.get(node.args[1].id)
        if command_type is not None:
            handled.append(command_type)
    return Counter(handled)


def _actor_dispatch_source() -> str:
    """Combined source of the semantic phases of the actor receive path.

    ``receive`` keeps the generation fence, closing guard, publish and receipt
    bookkeeping; the lifecycle-command prefix and the remaining command
    dispatch live in the two private phase methods on the same canonical actor.
    """

    return "\n".join(
        (
            inspect.getsource(HarnessRuntimeActor.receive),
            inspect.getsource(HarnessRuntimeActor._receive_lifecycle_command),
            inspect.getsource(HarnessRuntimeActor._dispatch_command),
        )
    )






def _safety_contract_errors(
    handoff_source: str, stop_source: str, session_ref_source: str
) -> tuple[str, ...]:
    errors: list[str] = []
    if "receipt.wait" not in handoff_source:
        errors.append("completion receipt missing")
    if "receipt.claimed_by" not in handoff_source:
        errors.append("same-generation claim missing")
    if "handoff remained overloaded" in handoff_source:
        errors.append("successful completion has a false deadline")
    if "command.deadline_ms" not in stop_source:
        errors.append("stop deadline missing")
    if "_terminate_pending" not in stop_source:
        errors.append("pending caller termination missing")
    if "process" in session_ref_source:
        errors.append("session ref dynamically reads process")
    return tuple(errors)










# 104164aa: the old pid-gate/deferred-sweep tests are replaced by the
# independent E/H no-replacement and restart tests in test_lifecycle_failed_remove.py.


# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    '_AgentIdentity',
    '_Launcher',
    '_Process',
    '_actor_dispatch_source',
    '_dead_pid',
    '_handled_frozen_commands',
    '_safety_contract_errors',
    '_spec',
    '_stopping_port',
    '_wait_for',
]
