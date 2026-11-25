"""Bind S7a's ``delivery.bodyRetentionMs`` into the S5 delivery facade (phase A, I1).

Decision notes (phase A, integration I1; plan §2.0 "Retention sources", Q9):

- Delivery never imports cells.  Composition hands the facade a retention
  source ``(subject) -> int`` through ``app._inbox.bind_body_retention``;
  the facade asks it only for a grant without its own ``retention_ms`` and
  passes the granted row's local agent as the subject.
- The source resolves ``DELIVERY_BODY_RETENTION_MS`` (combine ``min``,
  clamp 1 h..365 d) from ``cell_params(app)`` for that one agent.  A missing
  subject is never resolved as None (None would apply every trusted source,
  the minimum over all agents): it answers the 7 d default without reading.
  No snapshot (``cell_params`` is None) resolves to the spec default, and a
  raising read falls back to it too.  The facade clamps once more.
- ``cell_params`` may compose the cell runtime: grants come from the
  conversation recorder (S6), which has composed it to write the record.
- SYSTEM rows are never granted, so they keep the code default (Q18).
- ``bind_body_retention_params`` runs once the inbox facade exists
  (``_startup_adopt_domain_services``); an app without an inbox facade is a
  no-op, and binding never raises into startup.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyprial.daemon.impl.cells.params import DELIVERY_BODY_RETENTION_MS, resolve_param
from hyprial.daemon.impl.composition.cells import cell_params

__all__ = ["bind_body_retention_params", "body_retention_source"]


def body_retention_source(app: Any) -> Callable[[object], int]:
    """The per-agent ``delivery.bodyRetentionMs`` source over *app*'s cell params."""

    default = int(DELIVERY_BODY_RETENTION_MS.default)

    def retention_ms(subject: object) -> int:
        if not isinstance(subject, str) or not subject:
            return default
        try:
            value = resolve_param(cell_params(app), DELIVERY_BODY_RETENTION_MS, subject)
        except Exception:  # noqa: BLE001 - an unreadable param keeps the code default
            return default
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    return retention_ms


def bind_body_retention_params(app: Any) -> bool:
    """Bind the retention source to ``app._inbox``; False when there is none."""

    binder = getattr(getattr(app, "_inbox", None), "bind_body_retention", None)
    if not callable(binder):
        return False
    try:
        binder(retention_ms=body_retention_source(app))
    except Exception as error:  # noqa: BLE001 - binding never breaks startup
        logger = getattr(app, "_log", None)
        if logger is not None:
            try:
                logger("warn", "delivery", "delivery.retention_bind_failed",
                       errorType=type(error).__name__)
            except Exception:  # noqa: BLE001 - logging never breaks startup
                pass
        return False
    return True
