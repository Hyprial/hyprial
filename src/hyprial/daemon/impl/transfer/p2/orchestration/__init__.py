"""P2 (agent-home) cross-host orchestration.

One-way dependency by design: this package may import the P2 authority
(``p2.journal``/``p2.store``/``p2.source``) but **nothing here may import the P0
transfer chain** (``daemon.impl.transfer.orchestrator``).  P0 may call into this
package when the refusal is lifted for a proven harness/mode.
"""

from __future__ import annotations

from .orchestrator import (
    CONFIG,
    IDENTITY,
    NATIVE_ROOT,
    SESSION,
    P2Item,
    P2Preflight,
    preflight_plan,
)

__all__ = [
    "CONFIG",
    "IDENTITY",
    "NATIVE_ROOT",
    "SESSION",
    "P2Item",
    "P2Preflight",
    "preflight_plan",
]
