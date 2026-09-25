"""Per-IPC-method and per-thread-side CPU cost counters (``ps`` ipcStats).

Why this exists
---------------
The production daemon idles at ~0.6 core.  In-process profiling explains
~0.2 core (``session.heartbeat`` ~23 ms CPU, ``message.pending.list`` ~2 ms);
the remaining ~0.4 core cannot be attributed by stack samplers, because the
cost is spread over ~9 short-lived IPC client threads a second.  These
counters are the instrument that can attribute it: each handled request adds
its own thread's CPU to one fixed key, and ``ps --json`` exposes the totals
with the process CPU clock read at the same moment, so two snapshots are
enough to reconcile.

Attribution rule (each piece of work is counted on exactly one side)
--------------------------------------------------------------------
The side is decided by the thread that executed the work; every side reads
only ``time.thread_time()`` of its own thread:

* **IPC side** (``methods``): the IPC client thread, around
  ``DaemonApplication.handle``, keyed by method.  While a handler waits for a
  domain actor (``call_session`` blocks in ``CorrelatedDomainEvents.wait``)
  the IPC thread is parked and accrues no thread CPU, so actor work is NOT
  included here.
* **Session-actor side** (``sessionActor``): the session actor's own thread,
  around one dispatch in ``_SessionGeneration.__call__``, keyed by command
  type.
* **Agent-actor side** (``agentActor``): the ``agent-registry-authority``
  actor thread, around one dispatch in ``_AgentGeneration.__call__``, keyed
  ``<CommandType>/session`` when a SessionActor effect sent the command
  (decided from the SessionActor's live attempt table, not the id's shape)
  and ``<CommandType>/other`` otherwise.
* **Effect-admission side** (``effectAdmission``): the
  ``hyprial-session-agent-effects`` thread, around admitting one effect,
  keyed by operation.  Everything it does is session-originated.

Partition rule: each side counts only CPU inside its marked span on its own
thread.  CPU on a side's thread outside that span is uncovered, not the
side's -- e.g. IPC framing (accept, per-connection thread start, ``recv``,
JSON parse/serialise, ``sendall``) runs on the IPC thread but outside the
``handle`` span.  Spans on different threads cannot overlap and a span is
counted once, so "no overlap, no gap" is checkable: every CPU second is
either inside exactly one marked span or in a named uncovered category.

No side reads another side's thread clock, so a breach of the upper bound
below has one meaning: double counting inside a side.

Calibration contract (pre-registered with e2e-verifier)
-------------------------------------------------------
Take two ``ps --json`` snapshots over one window.  Definitions:

* ``Δ = processCpuSeconds₂ − processCpuSeconds₁`` (``time.process_time``,
  the finer clock; ``ps -o time`` with its 0.01 s display is only a
  cross-check, allowed display error ±0.02 s per window).  Δ is the
  denominator for EVERY check below, including the upper bound.
* **Instrumented CPU** (``instrumentedCpuMs`` delta) = IPC + session actor +
  Agent actor + effect admission.
* **IPC-path CPU** (``ipcPathCpuMs`` delta) = IPC + session actor + the
  session-originated Agent-actor share (``*/session`` keys) + effect
  admission.  Agent-actor work with ``/other`` origin is instrumented but not
  IPC path.
* **Coverage** = instrumented CPU / Δ.  **IPC-path fraction** = IPC-path
  CPU / Δ.

Checks:

* **Upper bound:** instrumented CPU ≤ Δ.  Exceeding it means double
  counting inside a side; the counters are wrong.
* **Readout:**
  - coverage < 70 % ⇒ "incomplete": extend coverage first and do not
    interpret the residual;
  - coverage ≥ 70 % and IPC-path fraction ≥ 0.80 ⇒ the answer is in the IPC
    paths;
  - coverage ≥ 70 % and IPC-path fraction ≤ 0.40 ⇒ the whole IPC path (IPC
    handler + session actor + session-originated Agent-actor work + effect
    admission) is under 40 %: the residual is elsewhere; go to the uncovered
    categories next;
  - coverage ≥ 70 % and 0.40–0.80 ⇒ no conclusion; extend coverage or
    lengthen the window.

Known uncovered categories, named up front (disjoint from the four sides
above): maintenance/runtime timer ticks, lark worker threads, the PAC actor,
the lifecycle process manager, IPC framing outside the handler (accept,
per-connection thread start, ``recv``, JSON parse and serialise,
``sendall``), and GC/interpreter overhead outside handler bodies.

Cost and bounds
---------------
One call costs two ``thread_time()`` reads, one ``perf_counter()`` pair
(IPC side only) and one short critical section under one lock per side;
the Agent-actor side also takes the SessionActor's effect lock once per
command to decide the origin.  Keys
are a fixed set: anything outside it folds into :data:`OTHER_KEY`, so a
client sending arbitrary method names cannot grow the map.
"""

from __future__ import annotations

import time
from typing import Any

from hyprial.cost_counters import OTHER_KEY, CallCostCounters

__all__ = [
    "OTHER_KEY",
    "CallCostCounters",
    "ipc_stats_payload",
]


#: The Agent-actor key suffix for work a SessionActor effect asked for; the
#: session-originated share is part of the IPC path (calibration contract).
_SESSION_ORIGIN_SUFFIX = "/session"


def _cpu_ms(block: dict[str, dict[str, Any]], *, suffix: str = "") -> float:
    return sum(
        row["threadCpuMs"] for key, row in block.items() if key.endswith(suffix)
    )


def ipc_stats_payload(
    *,
    methods: CallCostCounters,
    session_actor: CallCostCounters,
    agent_actor: CallCostCounters,
    effect_admission: CallCostCounters,
) -> dict[str, Any]:
    """The additive ``daemon.ipcStats`` block of ``ps``.

    ``processCpuSeconds`` is read at snapshot time so that one snapshot pair
    yields Δ for the calibration contract without a second instrument.  The
    two totals are derived from the same snapshot's rows, so a reader can
    difference them across two snapshots instead of re-summing by hand:
    ``instrumentedCpuMs`` (all four sides; numerator of coverage) and
    ``ipcPathCpuMs`` (IPC + session actor + session-originated Agent actor
    work + effect admission; numerator of the IPC-path fraction).
    """

    process_cpu = time.process_time()
    method_rows = methods.snapshot()
    session_rows = session_actor.snapshot()
    agent_rows = agent_actor.snapshot()
    admission_rows = effect_admission.snapshot()
    common = _cpu_ms(method_rows) + _cpu_ms(session_rows) + _cpu_ms(admission_rows)
    return {
        "sinceMs": methods.since_ms,
        "processCpuSeconds": process_cpu,
        "methods": method_rows,
        "sessionActor": session_rows,
        "agentActor": agent_rows,
        "effectAdmission": admission_rows,
        "instrumentedCpuMs": round(common + _cpu_ms(agent_rows), 3),
        "ipcPathCpuMs": round(
            common + _cpu_ms(agent_rows, suffix=_SESSION_ORIGIN_SUFFIX), 3
        ),
    }
