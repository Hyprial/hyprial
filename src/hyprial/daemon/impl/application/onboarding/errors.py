"""Wire-level failure raised by the onboarding request port.

This module has no imports so request handlers can use the type without pulling
in the application assembly or the onboarding runtime.
"""

from __future__ import annotations

from typing import Any


class OnboardingGatewayError(Exception):
    """A refused onboarding call carrying a public wire code and data.

    The port chooses a code from the MethodSpec's declared error set.  The
    handler still verifies the declaration and downgrades an undeclared code to
    ``DAEMON_ERROR``; internal receipt/recovery/store errors are never assigned
    a new wire code here.
    """

    def __init__(
        self, code: str, message: str, data: Any | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


__all__ = ["OnboardingGatewayError"]
