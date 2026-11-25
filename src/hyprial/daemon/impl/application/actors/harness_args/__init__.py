"""Effective harness arguments shared by explicit start and restore."""

from __future__ import annotations

from hyprial.identity import Agent
from hyprial.kernel import (
    DaemonRequestError,
    HarnessArgValidationError,
    ipc_errors,
    managed_harness_args_supported,
    merge_harness_launch_args,
    validate_harness_launch_args,
)


def harness_args_invalid(
    harness: str, error: BaseException,
) -> DaemonRequestError:
    issues = (
        [
            {
                "code": issue.code,
                "message": issue.message,
                "token": issue.token,
            }
            for issue in error.issues
        ]
        if isinstance(error, HarnessArgValidationError)
        else [{"code": "invalid-option-value", "message": str(error), "token": None}]
    )
    return DaemonRequestError(
        ipc_errors.HARNESS_ARGS_INVALID,
        f"{harness} harness args are invalid for managed launch",
        {"harness": harness, "issues": issues},
    )


class _HarnessArgsLifecycleMixin:
    def _effective_harness_args(
        self,
        name: str,
        harness: str,
        explicit: tuple[str, ...],
        *,
        ignore_stored: bool,
        reject_invalid: bool = True,
    ) -> tuple[tuple[str, ...], Agent | None, tuple[str, ...]]:
        registered = self.agents.current(name)
        available = () if registered is None else registered.args_for(harness)
        stored = () if ignore_stored else available
        merged = merge_harness_launch_args(
            stored, explicit, harness=harness, validate=False
        )
        if not managed_harness_args_supported(harness):
            # dsh and test doubles keep their own command line; only the
            # managed harnesses have a phase-one argument policy.
            return merged, registered, available
        try:
            validated = validate_harness_launch_args(harness, merged)
        except HarnessArgValidationError as error:
            if reject_invalid:
                raise harness_args_invalid(harness, error) from error
            validated = merged
        return validated, registered, available


__all__ = ["_HarnessArgsLifecycleMixin", "harness_args_invalid"]
