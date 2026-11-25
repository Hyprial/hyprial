"""The composed smoke instrument and its top-level dispatcher."""

from __future__ import annotations

from .apply import ApplyMixin
from .base import SmokeBase
from .common import EXIT_FAILED, EXIT_ROLLBACK_FAILED, EXIT_USAGE, FAIL
from .gui import GuiMixin
from .gui_probe import GuiProbeMixin
from .preflight import PreflightMixin
from .service import ServiceMixin


class Smoke(PreflightMixin, GuiProbeMixin, GuiMixin, ApplyMixin, ServiceMixin, SmokeBase):
    def run(self) -> int:
        if self.options.mode == "preflight":
            if getattr(self.options, "gui", False):
                self.check("usage.gui", FAIL, "--gui is only valid with apply")
                self.result = "ABORTED"
                return self.finish(EXIT_USAGE)
            if not self.prepare():
                return self.finish_refusal()
            self.isolated_preflight()
            self.result = self.status()
            return self.finish(self.exit_code())
        guard = self.apply_guard()
        if guard is not None:
            self.check("apply.guard", FAIL, guard)
            self.result = "ABORTED"
            return self.finish(EXIT_USAGE)
        if self.options.gui_hold_minutes > 0 and not self.options.gui:
            self.check("usage.gui", FAIL, "--gui-hold-minutes needs --gui")
            self.result = "ABORTED"
            return self.finish(EXIT_USAGE)
        if not self.prepare():
            return self.finish_refusal()
        self.isolated_preflight()
        if self.status() == FAIL:
            self.result = "FAILED_PREFLIGHT"
            return self.finish(EXIT_FAILED)
        try:
            return self.finish(self.run_apply())
        except Exception as error:  # noqa: BLE001 - mutate/rollback boundary
            self.check("exception", FAIL, f"{type(error).__name__}: {error}")
            if not self.mutated:
                self.result = "FAILED"
                return self.finish(EXIT_FAILED)
            try:
                return self.finish(
                    self.rollback_after_failure(
                        f"unhandled {type(error).__name__}: {error}"
                    )
                )
            except Exception as rollback_error:  # noqa: BLE001 - report and stop
                self.check(
                    "rollback.exception",
                    FAIL,
                    f"{type(rollback_error).__name__}: {rollback_error}",
                )
                self.result = "ROLLBACK_FAILED"
                return self.finish(EXIT_ROLLBACK_FAILED)
