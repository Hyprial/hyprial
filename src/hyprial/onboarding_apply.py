"""The write half of first-run onboarding (desktop payload contract section 11).

``hyprial onboarding plan`` answers "what is still missing on this machine".  This
module acts on that answer: it runs the same primitives an operator would run by
hand, one step at a time, and records success nowhere except the machine's own
state.  There is deliberately no "done" bookkeeping: a step is finished when a
fresh read of the machine says it is, so an interrupted or half-applied step is
retried instead of being remembered as complete.

The rules this module enforces:

* a step whose ``requires`` are not satisfied performs no work at all;
* an interactive step is never run headless -- it refuses instead, so the desktop
  shell can carry the human part and this path stays safe to call from startup;
* a step with no automatable primitive yet says so (``MANUAL_ACTION_REQUIRED``)
  rather than reporting a local claim as evidence;
* after an action, the plan is re-read; if the machine does not show the step as
  done, that is a failure (``STEP_NOT_OBSERVED``), not a success.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hyprial.onboarding import STEPS, OnboardingStep, plan_first_run

#: ``settings.json`` switch that gates ``onboarding apply --auto``.  Default ON,
#: unlike ``autoUpgrade``: these steps are local, observable, re-entrant registration
#: actions (create an agent, configure Squire), and "a usable install on first launch"
#: does not exist without them.  ``autoUpgrade`` replaces the code that is about to
#: run, which is a different risk class and stays default OFF.
AUTO_ONBOARDING_SETTINGS_KEY = "autoOnboarding"

PREREQUISITE_NOT_MET = "PREREQUISITE_NOT_MET"
INTERACTIVE_REQUIRED = "INTERACTIVE_REQUIRED"
MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
STEP_FAILED = "STEP_FAILED"
STEP_NOT_OBSERVED = "STEP_NOT_OBSERVED"
UNKNOWN_STEP = "UNKNOWN_STEP"

_STEP_BY_ID = {step.id: step for step in STEPS}
_DONE_STATES = frozenset({"done", "skipped"})


class OnboardingApplyError(Exception):
    """A refusal or failure, carrying the contract's ``errorCode``."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details: dict[str, Any] = dict(details or {})


def auto_onboarding_enabled(hyprial_home: Path) -> bool:
    """Whether ``onboarding apply --auto`` may act without being asked.

    On by default; only an explicit ``"autoOnboarding": false`` disables it.  A
    settings.json we cannot read is treated as **disabled**, not as "default on":
    this is a write path, and a machine whose settings cannot be parsed is not a
    machine whose consent to mutate can be assumed.  The caller gets the reason.
    """

    path = Path(hyprial_home) / "settings.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    return record.get(AUTO_ONBOARDING_SETTINGS_KEY) is not False


def _step(step_id: str) -> OnboardingStep:
    step = _STEP_BY_ID.get(step_id)
    if step is None:
        raise OnboardingApplyError(
            UNKNOWN_STEP, f"unknown onboarding step: {step_id}"
        )
    return step


def _plan_entry(plan: Mapping[str, object], step_id: str) -> Mapping[str, Any]:
    steps = plan.get("steps")
    if isinstance(steps, list):
        for entry in steps:
            if isinstance(entry, Mapping) and entry.get("id") == step_id:
                return entry
    raise AssertionError(f"plan is missing its own step: {step_id}")


def _outstanding_requirements(
    step: OnboardingStep, plan: Mapping[str, object]
) -> list[str]:
    return [
        requirement
        for requirement in step.requires
        if _plan_entry(plan, requirement)["state"] not in _DONE_STATES
    ]


def _detail(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class OnboardingApplier:
    """Run the automatable steps against injected reads, writes and settings."""

    def __init__(
        self,
        *,
        snapshot: Callable[[], Mapping[str, object]],
        actions: Mapping[str, Callable[[], object]],
        auto_enabled: Callable[[], bool],
    ) -> None:
        self._snapshot = snapshot
        self._actions = dict(actions)
        self._auto_enabled = auto_enabled

    def _plan(self) -> dict[str, object]:
        return plan_first_run(self._snapshot())

    def apply_step(self, step_id: str) -> dict[str, object]:
        """Apply exactly one step; refusal and failure both raise."""

        return self._apply(_step(step_id), self._plan())

    def apply_auto(self) -> dict[str, object]:
        """Apply every ready, non-interactive step the plan offers, in plan order."""

        if not self._auto_enabled():
            return {
                "ok": True,
                "mode": "auto",
                "state": "disabled",
                "changed": [],
                "alreadyDone": [],
                "stopped": None,
                "reason": f"settings.json {AUTO_ONBOARDING_SETTINGS_KEY} is off",
                "plan": self._plan(),
            }

        changed: list[str] = []
        already_done: list[str] = []
        attempted: set[str] = set()
        stopped: dict[str, object] | None = None
        while True:
            plan = self._plan()
            candidate = self._next_actionable(plan, attempted)
            if candidate is None:
                break
            attempted.add(candidate)
            try:
                outcome = self._apply(_step(candidate), plan)
            except OnboardingApplyError as error:
                stopped = {
                    "step": candidate,
                    "errorCode": error.code,
                    "message": str(error),
                    **error.details,
                }
                break
            (changed if outcome["changed"] else already_done).append(candidate)

        return {
            "ok": stopped is None,
            "mode": "auto",
            "changed": changed,
            "alreadyDone": already_done,
            "stopped": stopped,
            "plan": self._plan(),
        }

    def _next_actionable(
        self, plan: Mapping[str, object], attempted: set[str]
    ) -> str | None:
        """The next step ``--auto`` may take, or None when the rest needs a person.

        Interactive steps and steps without a primitive are deliberately skipped
        rather than treated as failure: auto finishes everything it can and the
        plan carries the human remainder.  ``attempted`` also bounds the loop when
        a primitive claims success that the machine does not show.
        """

        steps = plan.get("steps")
        if not isinstance(steps, list):
            return None
        for entry in steps:
            if not isinstance(entry, Mapping) or entry.get("state") != "ready":
                continue
            step_id = entry.get("id")
            if not isinstance(step_id, str):
                continue
            step = _STEP_BY_ID.get(step_id)
            if step is None or step.interactive or step_id not in self._actions:
                continue
            if step_id in attempted:
                continue
            return step_id
        return None

    def _apply(
        self, step: OnboardingStep, plan: Mapping[str, object]
    ) -> dict[str, object]:
        entry = _plan_entry(plan, step.id)
        state = entry["state"]
        if state == "done":
            return self._envelope(
                step, "already-done", changed=False, detail={"reason": "already satisfied"}
            )
        if state == "skipped":
            return self._envelope(
                step,
                "skipped",
                changed=False,
                detail={"reason": entry.get("reason")},
            )
        if state == "blocked":
            blocked_by = _outstanding_requirements(step, plan)
            raise OnboardingApplyError(
                PREREQUISITE_NOT_MET,
                f"{step.id} is waiting on {', '.join(blocked_by)}",
                details={"blockedBy": blocked_by},
            )
        if step.interactive:
            raise OnboardingApplyError(
                INTERACTIVE_REQUIRED,
                f"{step.id} needs a person and was not run",
                details={"action": step.action, "interactive": True},
            )

        action = self._actions.get(step.id)
        if action is None:
            raise OnboardingApplyError(
                MANUAL_ACTION_REQUIRED,
                f"{step.id} has no automatable primitive yet",
                details={"action": step.action},
            )
        try:
            detail = action()
        except OnboardingApplyError:
            raise
        except Exception as error:  # noqa: BLE001 - one boundary for primitive failures
            upstream = getattr(error, "code", None)
            raise OnboardingApplyError(
                STEP_FAILED,
                f"{step.id} failed: {error}",
                details={
                    "cause": type(error).__name__,
                    "upstreamCode": upstream if isinstance(upstream, str) else None,
                },
            ) from error

        observed = _plan_entry(self._plan(), step.id)["state"]
        if observed != "done":
            raise OnboardingApplyError(
                STEP_NOT_OBSERVED,
                f"{step.id} ran but the machine still reports it as {observed}",
                details={"observed": observed},
            )
        return self._envelope(step, "applied", changed=True, detail=_detail(detail))

    @staticmethod
    def _envelope(
        step: OnboardingStep,
        state: str,
        *,
        changed: bool,
        detail: Mapping[str, Any],
    ) -> dict[str, object]:
        return {
            "ok": True,
            "mode": "step",
            "step": step.id,
            "state": state,
            "changed": changed,
            "interactive": step.interactive,
            "detail": dict(detail),
        }


__all__ = [
    "AUTO_ONBOARDING_SETTINGS_KEY",
    "INTERACTIVE_REQUIRED",
    "MANUAL_ACTION_REQUIRED",
    "OnboardingApplier",
    "OnboardingApplyError",
    "PREREQUISITE_NOT_MET",
    "STEP_FAILED",
    "STEP_NOT_OBSERVED",
    "UNKNOWN_STEP",
    "auto_onboarding_enabled",
]
