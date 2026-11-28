"""Closed launch diagnostics across the existing text-only lifecycle bridge.

Only an exact adapter-owned code/phase/reason sentence is decoded. Unknown
stderr and lifecycle diagnostics never become structured model failure data.
"""

from hyprial.error import codes

MODEL_START_FAILURES = {
    codes.MODEL_SESSION_BUSY: ("admission", ("session-reserved",)),
    codes.MODEL_RESUME_MISMATCH: ("resume", ("binding-changed", "model-changed", "provenance-missing")),
    codes.MODEL_COMBINATION_UNSUPPORTED: ("admission", ("mode", "sdk-version", "runtime-option", "session-storage")),
    codes.MODEL_AUTH_UNAVAILABLE: ("authorize", ("missing",)),
}


class PiModelBindingError(ValueError):
    permanent_start_failure = True
    start_precondition_failure = True

    def __init__(self, code: str, phase: str, reason: str):
        expected, reasons = MODEL_START_FAILURES[code]
        if phase != expected or reason not in reasons:
            raise ValueError("invalid model launch diagnostic")
        self.code = code
        self.data = {"phase": phase, "reason": reason}
        super().__init__(f"{code}: {phase}/{reason}")


def failure_data(code: str, detail: str) -> dict[str, str]:
    detail = detail.removeprefix(f"harness.ensure failed: {code}: ")
    phase, reasons = MODEL_START_FAILURES.get(code, ("", ()))
    return next(({"phase": phase, "reason": reason} for reason in reasons
                 if detail == f"{code}: {phase}/{reason}"), {})


def start_failure(stderr: bytes) -> ValueError | None:
    lines = stderr.splitlines()
    if b"HYPRIAL_PI_START_FAILURE:MODEL_UNAVAILABLE" in lines:
        from ..loader import PiStartPreconditionError
        return PiStartPreconditionError("configured Pi model is unavailable; check the selected provider and model")
    for code, (phase, reasons) in MODEL_START_FAILURES.items():
        if f"HYPRIAL_PI_START_FAILURE:{code}".encode() not in lines:
            continue
        for reason in reasons:
            if f"{phase}/{reason}".encode() in lines:
                return PiModelBindingError(code, phase, reason)
    return None
