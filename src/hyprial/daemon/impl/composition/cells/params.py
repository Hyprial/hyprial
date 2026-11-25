"""Compose the cell param view of a daemon generation (plan §1.1, S7a).

Decision notes (phase A, S7a):

- ``cell_params(app)`` returns the current ``CellParamSnapshot`` of the
  app's cell runtime, or None whenever ``cell_runtime(app)`` is None (closing,
  closed, composition failed or backing off).  It never raises; callers pass
  the result straight to ``resolve_param``, which turns None into the spec
  default.
- The view is cached on ``app._cell_params`` and bound to one runtime
  instance through its ``on_index`` hook.  When the app's runtime is a
  different instance (a recomposition), the old view is unbound and a new
  one is built, so a stale runtime's index never answers.
- The trusted owner is the runtime's author (the local OrgFS principal),
  the same principal S3 authors as (plan Q10).
- No shutdown step: the hook lives in the runtime's hook list and stops with
  the ``cell-runtime`` step.
"""

from __future__ import annotations

import threading
from typing import Any

from hyprial.daemon.impl.cells.params import CellParamSnapshot, CellParamView
from hyprial.daemon.impl.composition.cells.wiring import cell_runtime

_VIEW_GUARD = threading.Lock()


def _log(app: Any, level: str, event: str, **fields: Any) -> None:
    logger = getattr(app, "_log", None)
    if logger is None:
        return
    try:
        logger(level, "cells", event, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks composition
        pass


def _view(app: Any, runtime: Any) -> CellParamView:
    view = getattr(app, "_cell_params", None)
    if view is not None and view.runtime is runtime:
        return view
    with _VIEW_GUARD:
        view = getattr(app, "_cell_params", None)
        if view is not None and view.runtime is runtime:
            return view
        stale = view
        view = CellParamView(
            runtime, owner=runtime.identity.authority, logger=getattr(app, "_log", None)
        ).bind()
        app._cell_params = view
    if stale is not None:
        stale.close()
    return view


def cell_params(app: Any) -> CellParamSnapshot | None:
    """The app's current cell param snapshot; None without a cell runtime."""

    runtime = cell_runtime(app)
    if runtime is None:
        return None
    try:
        return _view(app, runtime).snapshot()
    except Exception as error:  # noqa: BLE001 - callers fall back to spec defaults
        _log(
            app,
            "warn",
            "cell.params_unavailable",
            errorType=type(error).__name__,
            detail=str(error)[:300],
        )
        return None


__all__ = ["cell_params"]
