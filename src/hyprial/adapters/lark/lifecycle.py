"""Measured lifecycle bounds for Lark adapters (fd73140a, spec v2 2026-09-04).

Where the number comes from (2026-09-04, hq production daemon restart at
15:19; spec notes/spec-adapter-lifecycle-guards-2026-09-04.md §2):

* a single adapter's worker signals ready on its dedicated fd in well under
  60s in the common case -- that is the measured SINGLE-EPISODE bound;
* during full-fleet bootstrap the daemon observed lark adapters reaching
  ``lark.adapter.online`` at ~2.3 min for most connectors and up to 10 min
  for the slowest one.  Those figures are the adapter's multi-episode path
  to online (worker handshake timeouts plus reconcile backoff across
  repeated spawns), NOT the lifetime of one transition -- reconcile-spawned
  restarts hold no transition at all, so they do not lengthen any
  write-to-pop age.

The deadline object (spec v2 G2, per h2b-developer review) is ONE
transition's write-to-pop age.  Only ``_start``/``_remove`` write
transitions, and the completion each waits for is a single effect run:
spawn + the 2s start-confirm wait, plus at most one compensating stop
(~8s bound) and effect-pool queueing.  That makes the measured single
episode (<60s) the honest distribution to draw N from -- 60s keeps a 4-6x
margin over the ~10-15s effect bound while still failing loud in about a
minute instead of a quarter hour.  (An earlier draft used the 10-min fleet
tail x1.5 = 900s; that was the wrong object -- 15x too generous for a
single transition, stretching every leak to 15 minutes of unavailability.)
"""

from __future__ import annotations

#: One lifecycle transition's write-to-pop deadline: the measured single
#: adapter episode (<60s, 2026-09-04 15:19) with margin for effect-pool
#: queueing; see the module docstring for why the fleet figures do not apply.
START_DEADLINE_SECONDS = 60.0

#: G2: the transition exceeded :data:`START_DEADLINE_SECONDS` (the pending
#: caller is failed with this code and the lifecycle is released).
ADAPTER_START_TIMEOUT = "ADAPTER_START_TIMEOUT"

__all__ = [
    "ADAPTER_START_TIMEOUT",
    "START_DEADLINE_SECONDS",
]
