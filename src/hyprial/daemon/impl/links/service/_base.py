"""Shared primitives of the WP2 link service (constants, per-actor state).

Internal module of ``hyprial.daemon.impl.links.service`` (spec §9.5).  It has no
imports from its sibling modules, so every other module may depend on it
without a cycle.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import hyprial.daemon.impl.links.policy as policy
from hyprial.daemon.impl.links.controller import (
    ControllerOutput,
    ControllerState,
    DwellSample,
    ReplySample,
    RttSample,
)
from hyprial.daemon.impl.links.model import LinkFile
from hyprial.daemon.impl.links.store import CensoredRecord, HeldRow, LedgerRow


TICK_INTERVAL_MS = 1000
#: A send reservation nobody confirms is dropped after this long.
RESERVATION_TTL_MS = 30_000
#: Per actor, per kind, per peer bound on the sample deques (B17).
SAMPLE_CAP = 1024
#: How long an online agent counts towards ``N_active``.
ACTIVE_WINDOW_MS = 24 * 60 * 60 * 1000
#: Terminal code the inbox retry pump uses for an expired hold (§3.6).  The
#: inbox spells it as a literal; links cannot import the inbox.
TTL_EXPIRED = "TTL_EXPIRED"
#: ``link.reclaimed`` never carries more than this many drained ids (§2.7).
_DRAINED_ID_LIMIT = 32
#: Bound on one peer's task-binding set.
_MAX_TASK_IDS = 4096
#: ``link.bypass_admitted`` rate limit per (actor, peer, reason).
BYPASS_LOG_INTERVAL_MS = 60_000



@dataclass
class _ActorState:
    """One local actor's in-memory state (§1.2 table)."""

    controller: ControllerState | None = None
    limits: ControllerOutput | None = None
    admitted: set[str] = field(default_factory=set)
    pending_inbound: dict[str, policy.PendingRequest] = field(default_factory=dict)
    censored_inbound: dict[str, CensoredRecord] = field(default_factory=dict)
    outbound: list[policy.OutboundRequest] = field(default_factory=list)
    reservations: list[tuple[str, int]] = field(default_factory=list)
    held: dict[str, HeldRow] = field(default_factory=dict)
    held_detail: dict[str, str] = field(default_factory=dict)
    held_pending: dict[str, tuple[str, str, str | None]] = field(default_factory=dict)
    reply_credits: dict[str, tuple[str, str]] = field(default_factory=dict)
    reclaimed: dict[str, int] = field(default_factory=dict)
    barrier: dict[str, tuple[int, int]] = field(default_factory=dict)
    recent_reclaims_ms: list[int] = field(default_factory=list)
    drained: set[str] = field(default_factory=set)
    #: Ruling 70: settled inbound ids (closed/acked/expired/drained ledger
    #: rows) as ``id -> row expiry``.  ``admit_dispatch`` answers True for
    #: them without re-entering ``admitted``/``pending_inbound`` or taking a
    #: W seat; entries are pruned at the ledger purge horizon, so the set
    #: stays as bounded as the ledger itself.
    settled: dict[str, int] = field(default_factory=dict)
    released_outbound: set[str] = field(default_factory=set)
    task_bindings: dict[str, set[str]] = field(default_factory=dict)
    established: dict[str, int] = field(default_factory=dict)
    minted_side: dict[str, str] = field(default_factory=dict)
    last_used: dict[str, int] = field(default_factory=dict)
    pending_open: list[LedgerRow] = field(default_factory=list)
    pending_bind: list[tuple[str, str, str, int]] = field(default_factory=list)
    pending_unbind: list[tuple[str, str, str]] = field(default_factory=list)
    sampled_writeback: set[str] = field(default_factory=set)
    pending_drain: set[str] = field(default_factory=set)
    explicit: set[str] = field(default_factory=set)
    #: Local write intent not yet handed to the store (P-3).  The IPC/admin/
    #: PAC threads only touch this; the service thread drains it into
    #: ``LinkStore.write`` during a pass.
    pending_file: LinkFile | None = None
    #: Reclaims an operator or ``evict`` requested, applied by the service
    #: thread (``_reclaim`` does inbox I/O and publishes events).
    pending_reclaims: list[tuple[str, str, int]] = field(default_factory=list)
    dwell: deque[DwellSample] = field(default_factory=deque)
    rtt: deque[RttSample] = field(default_factory=deque)
    replies: deque[ReplySample] = field(default_factory=deque)
    held_cwnd_peers: frozenset[str] = frozenset()
    #: H1: how often observe mode skipped a full W/cwnd on this actor's behalf
    #: (send or dispatch).  Counted, never authoritative.
    window_observed: int = 0
