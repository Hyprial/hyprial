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
* **Session-persistence I/O side** (``sessionPersistenceIo``): the
  ``desired-state-io`` worker around one immutable storage request, keyed by
  ``<operation>/<origin>``.  ``/ipc`` means an external session command and
  its exact retained completion; lease/lifecycle/replay work is
  ``/background``.
* **State-persistence side** (``statePersistence``): the
  ``state-persistence-authority`` actor around one typed command, keyed by
  ``<CommandType>/<origin>`` propagated from the immutable I/O request.
* **Runtime side** (``runtime``): every other generic ``ActorRuntime``
  dispatch, every other ``EffectLane`` execution, and each maintenance phase,
  keyed by normalized declared owner and work type.  A trailing numeric worker
  suffix is removed from the declared owner; Pykka's private thread name is
  never an identity.  ``kind:<instance>`` names keep only the kind and orgfs
  lanes drop their embedded space id.  The map is capped with one reserved
  row: once full, every new key folds into ``_overflow/_other`` (never another
  owner's row) with ``runtimeOverflowFolds`` incremented.

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
* **Legacy instrumented CPU** (``instrumentedCpuMs`` delta) = IPC + session actor +
  Agent actor + effect admission + session-persistence I/O + state persistence.
  This published total is kept unchanged for existing readers.
* **Coverage CPU** (``coverageCpuMs`` delta) = legacy instrumented CPU + the
  generic actor/effect/maintenance ``runtime`` block.  Owners already present
  in a legacy side are excluded at the generic choke point, so this is a
  disjoint sum.
* **IPC-path CPU** (``ipcPathCpuMs`` delta) = IPC + session actor + the
  session-originated Agent-actor share (``*/session`` keys) + effect
  admission + the ``*/ipc`` shares of both persistence sides. Agent-actor
  work with ``/other`` and persistence ``/background`` rows are instrumented
  but not IPC path.
* **Coverage** = coverage CPU / Δ.  **IPC-path fraction** = IPC-path
  CPU / Δ.

Checks:

* **Upper bound:** coverage CPU ≤ Δ.  Exceeding it means double
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

Known uncovered categories, named up front: custom worker threads that do not
use ``EffectLane``, lifecycle process management, IPC framing outside the
handler (accept, per-connection thread start, ``recv``, JSON parse and
serialise, ``sendall``), and GC/interpreter overhead outside handler bodies.

Cost and bounds
---------------
One runtime unit costs two ``thread_time()`` reads and one short critical
section under one lock; an IPC call also costs one ``perf_counter()`` pair;
the Agent-actor side also takes the SessionActor's effect lock once per
command to decide the origin.  Legacy-side keys are fixed and anything outside
them folds into :data:`OTHER_KEY`; runtime keys use their separate hard cap, so
a client sending arbitrary method or command names cannot grow either map.
"""

from __future__ import annotations

import time
from typing import Any

from hyprial.kernel import (
    OTHER_KEY,
    CallCostCounters,
    RuntimeCpuCounters,
    runtime_cpu_counters,
)

__all__ = [
    "OTHER_KEY",
    "CallCostCounters",
    "ipc_stats_payload",
    "owner_budget_rows",
]

#: Owners of the legacy sides, as the generic choke point names them.  Those
#: owners are excluded from ``runtime`` to avoid double counting, so the CPU
#: budget alarm reads them from their own side instead; otherwise an actor
#: with an existing meter (the #1173 hot spot ran on agent-registry-authority)
#: could never alarm.
_BUDGET_SIDE_OWNERS = (
    ("sessionActor", "session-authority"),
    ("agentActor", "agent-registry-authority"),
    ("statePersistence", "state-persistence-authority"),
    ("sessionPersistenceIo", "desired-state-io"),
    ("effectAdmission", "session-agent-effects"),
    ("methods", "ipc"),
)


#: The Agent-actor key suffix for work a SessionActor effect asked for; the
#: session-originated share is part of the IPC path (calibration contract).
_SESSION_ORIGIN_SUFFIX = "/session"
_IPC_ORIGIN_SUFFIX = "/ipc"


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
    session_persistence_io: CallCostCounters,
    state_persistence: CallCostCounters,
    runtime: RuntimeCpuCounters = runtime_cpu_counters,
) -> dict[str, Any]:
    """The additive ``daemon.ipcStats`` block of ``ps``.

    ``processCpuSeconds`` is read at snapshot time so that one snapshot pair
    yields Δ for the calibration contract without a second instrument.  The
    totals are derived from the same snapshot's rows, so a reader can
    difference them across two snapshots instead of re-summing by hand:
    ``instrumentedCpuMs`` (the stable legacy six-side total) and
    ``ipcPathCpuMs`` (IPC + session actor + session-originated Agent actor
    work + effect admission + IPC-origin persistence work; numerator of the
    IPC-path fraction). ``coverageCpuMs`` adds the disjoint generic runtime
    side and is the numerator for whole-daemon attribution coverage.
    """

    process_cpu = time.process_time()
    method_rows = methods.snapshot()
    session_rows = session_actor.snapshot()
    agent_rows = agent_actor.snapshot()
    admission_rows = effect_admission.snapshot()
    session_io_rows = session_persistence_io.snapshot()
    state_rows = state_persistence.snapshot()
    runtime_rows = runtime.snapshot()
    common = _cpu_ms(method_rows) + _cpu_ms(session_rows) + _cpu_ms(admission_rows)
    instrumented_cpu_ms = (
        common
        + _cpu_ms(agent_rows)
        + _cpu_ms(session_io_rows)
        + _cpu_ms(state_rows)
    )
    runtime_cpu_ms = _cpu_ms(runtime_rows)
    return {
        "sinceMs": methods.since_ms,
        "processCpuSeconds": process_cpu,
        "coverageCpuMs": round(instrumented_cpu_ms + runtime_cpu_ms, 3),
        "methods": method_rows,
        "sessionActor": session_rows,
        "agentActor": agent_rows,
        "effectAdmission": admission_rows,
        "sessionPersistenceIo": session_io_rows,
        "statePersistence": state_rows,
        "runtime": runtime_rows,
        "runtimeOverflowFolds": runtime.overflow_folds,
        "instrumentedCpuMs": round(instrumented_cpu_ms, 3),
        "ipcPathCpuMs": round(
            common
            + _cpu_ms(agent_rows, suffix=_SESSION_ORIGIN_SUFFIX)
            + _cpu_ms(session_io_rows, suffix=_IPC_ORIGIN_SUFFIX)
            + _cpu_ms(state_rows, suffix=_IPC_ORIGIN_SUFFIX),
            3,
        ),
    }


def owner_budget_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every owner's ``<owner>/<work>`` rows for the CPU budget alarm.

    The generic ``runtime`` block plus each legacy side under its owner name.
    The sides are disjoint (see the partition rule above), so no CPU is
    judged twice.
    """

    rows: dict[str, dict[str, Any]] = dict(payload.get("runtime") or {})
    for side, owner in _BUDGET_SIDE_OWNERS:
        for key, row in (payload.get(side) or {}).items():
            rows[f"{owner}/{key}"] = {
                "calls": row.get("calls", 0),
                "threadCpuMs": row.get("threadCpuMs", 0.0),
            }
    return rows
