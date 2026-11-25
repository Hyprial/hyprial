"""Desired-state rows, live digests and the startup barrier of member reconcile.

Decision notes:

- Split out of ``composition/cells/reconcile.py`` when I6-5 (own member,
  policy) was merged with dev's I6-2 restore fixes, so the effect adapter
  stays below the structural line limit.  The generic effect path and the
  own-member path (``own_member/effects.py``) read rows and compute live
  digests here, so both digest sides of every ensure use one normalised
  launch: a row's args and a live harness's args both go through the app's
  ``_effective_harness_args`` with the agent record's stored
  ``harnessArgs`` merged in, the call daemon restore makes before it
  launches a row.
- ``live_member_digest`` takes the policy digest the live process
  acknowledged as an input instead of the intent's, so an adopted process
  that never acknowledged the desired policy reads as differing and takes
  the reconcile actor's bounded drift restart.
- ``CellReconcileStartBarrier`` (I6-2 restore fix, 2026-11-23) is the
  explicit startup barrier between daemon restore and member reconcile.
  Daemon transport wiring closes it right before it composes the cell
  runtime and the restore thread opens it once restore has seeded agent
  bindings and the harness port is bound.  Until then the reconcile
  coordinator is told nothing (no initial ``on_index``, no index-change
  tell and no launch nudge), so no effect can reach a missing harness port.
  Opening runs the deferred initial tell once over the then-current index.
  The barrier is open by default, like ``_restore_done``: an app whose
  startup does not run transport wiring has no restore to wait for.  No
  sleep and no polling: the callback runs on the thread that opens it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from hyprial.daemon.impl.cells.reconcile import compute_member_config_digest


def _model_vendor(spec: Any) -> str | None:
    to_json = getattr(spec, "to_json", None)
    if not callable(to_json):
        return None
    value = to_json().get("modelProvider")
    return value if isinstance(value, str) else None


def _agent_for_id(app: Any, agent_id: str) -> Any:
    agents = getattr(app, "agents", None)
    listing = getattr(agents, "list", None)
    if not callable(listing):
        return None
    try:
        items = tuple(listing())
    except Exception:  # noqa: BLE001 - directory failures map to missing local facts
        return None
    for item in items:
        if getattr(item, "agent_id", None) == agent_id:
            return item
    return None


def _matching_desired_spec(
    harnesses: tuple[Any, ...],
    agent: Any,
    member: str,
) -> Any | None:
    if agent is None:
        return None
    agent_names: set[str] = set()
    for attr in ("actor", "uri", "agent_id"):
        value = getattr(agent, attr, None)
        if isinstance(value, str) and value:
            agent_names.add(value)
    if not agent_names:
        return None
    for item in harnesses:
        item_member = getattr(item, "member", None) or getattr(item, "name", None)
        if item_member != member:
            continue
        if getattr(item, "name", None) in agent_names:
            return item
    if member == getattr(agent, "uri", None):
        actor = getattr(agent, "actor", None)
        for item in harnesses:
            item_member = getattr(item, "member", None)
            if getattr(item, "name", None) == actor and item_member in {None, actor}:
                return item
    return None


def _desired_row(app: Any, agent_id: str, member: str) -> Any | None:
    agent = _agent_for_id(app, agent_id)
    loader = getattr(getattr(app, "desired_state", None), "load", None)
    if agent is None or not callable(loader):
        return None
    try:
        harnesses = tuple(loader().harnesses)
    except Exception:  # noqa: BLE001 - unreadable desired state falls back to the intent spec
        return None
    return _matching_desired_spec(harnesses, agent, member)


def _restore_launch_row(app: Any, row: Any) -> Any:
    args = _effective_args(app, row)
    return row if args == tuple(row.args) else replace(row, args=args)


def _effective_args(app: Any, spec: Any) -> tuple[str, ...]:
    args = tuple(getattr(spec, "args", ()) or ())
    effective = getattr(app, "_effective_harness_args", None)
    if not callable(effective):
        return args
    try:
        merged, _agent, _stored = effective(
            spec.name,
            spec.harness,
            args,
            ignore_stored=False,
            reject_invalid=False,
        )
    except Exception:  # noqa: BLE001 - an unreadable agent record keeps the row's own args
        return args
    return tuple(merged)


def _log(app: Any, level: str, event: str, **fields: Any) -> None:
    logger = getattr(app, "_log", None)
    if logger is None:
        return
    try:
        logger(level, "cells", event, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks an effect
        pass


def live_member_digest(
    app: Any,
    *,
    agent_id: str,
    member: str,
    env_ref: str | None,
    policy_digest: str | None,
    live_spec: Any,
) -> str:
    """The config digest of a live harness, over the normalised launch."""

    args = _effective_args(app, live_spec)
    return compute_member_config_digest(
        agent_id=agent_id,
        member=member,
        harness=live_spec.harness,
        model=live_spec.model,
        model_vendor=_model_vendor(live_spec),
        cwd=live_spec.cwd or "",
        command=tuple(live_spec.command),
        args=args,
        approval="approved" if "--approve" in args else None,
        env_ref=env_ref,
        policy_digest=policy_digest,
    )


class CellReconcileStartBarrier:
    """Defers reconcile tells until daemon restore has finished; never blocks."""

    def __init__(self, *, opened: bool = True) -> None:
        self._lock = threading.Lock()
        self._opened = opened
        self._waiting: dict[int, Callable[[], None]] = {}
        self._next = 0

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened

    def close(self) -> None:
        with self._lock:
            self._opened = False

    def open(self) -> tuple[Exception, ...]:
        """Open the barrier and run every deferred callback; returns their errors."""

        with self._lock:
            self._opened = True
            waiting = tuple(self._waiting.values())
            self._waiting.clear()
        errors: list[Exception] = []
        for callback in waiting:
            try:
                callback()
            except Exception as error:  # noqa: BLE001 - one callback never skips the others
                errors.append(error)
        return tuple(errors)

    def when_open(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Run *callback* now when open, else once on ``open``; returns a cancel."""

        with self._lock:
            if not self._opened:
                self._next += 1
                token = self._next
                self._waiting[token] = callback

                def cancel() -> None:
                    with self._lock:
                        self._waiting.pop(token, None)

                return cancel
        callback()
        return lambda: None


__all__ = [
    "CellReconcileStartBarrier",
    "live_member_digest",
]
