#!/usr/bin/env python3
"""Reproduce the static evidence for the 2026-10-23 kernel isolation survey.

The scanner deliberately imports no product module.  It parses source so the
survey remains runnable without contacting a daemon or reading user state.
Output is deterministic Markdown and is intended to be pasted into the design
record.  This is evidence tooling, not a product gate.
"""

from __future__ import annotations

import argparse
import ast
import collections
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from kernel_isolation_survey_retry_rules import (
    LIMIT_CLASS_DETAILS,
    LIMIT_CLASS_INSTANCES,
    LOCATION_DISMISSALS,
    LOCATION_LIMIT_CLASSES,
    RELATED_CLASSIFICATIONS,
    SPECIAL_DISMISSALS,
)
from kernel_isolation_survey_retry_shapes import (
    retry_shape as _retry_shape,
    walk_local as _walk_local,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src" / "hyprial"
SURVEY_BASELINE = "d5a4dd257d15320ce59a9bb56d116e786ab4e195"
DOMAINS = ("kernel", "identity", "daemon", "biz")
UPPER_DOMAINS = ("identity", "daemon", "biz", "shell")

# Runtime primitives whose construction can create execution or scheduling
# capacity. ActorRuntime/EffectLane/GenerationScheduler are included because
# their threads are hidden behind the kernel abstraction.
EXECUTION_PRIMITIVES = {
    "threading.Thread": "thread",
    "threading.Timer": "timer-thread",
    "_thread.start_new_thread": "per-request-thread-spawner",
    "thread.start_new_thread": "per-request-thread-spawner",
    "http.server.ThreadingHTTPServer": "per-request-thread-server",
    "socketserver.ThreadingTCPServer": "per-request-thread-server",
    "socketserver.ThreadingUDPServer": "per-request-thread-server",
    "concurrent.futures.ThreadPoolExecutor": "thread-pool",
    "concurrent.futures.ProcessPoolExecutor": "process-pool",
    "asyncio.run": "event-loop",
    "asyncio.new_event_loop": "event-loop",
    "asyncio.create_task": "async-task",
    "asyncio.to_thread": "default-executor-work",
    "ActorRuntime": "actor-runtime",
    "EffectLane": "effect-worker-pool",
    "GenerationScheduler": "timer-scheduler",
}
SYNC_PRIMITIVES = {
    "queue.Queue": "bounded-or-unbounded-queue",
    "queue.SimpleQueue": "unbounded-queue",
    "threading.Lock": "lock",
    "threading.RLock": "lock",
    "threading.Condition": "condition",
    "threading.Semaphore": "semaphore",
    "threading.BoundedSemaphore": "semaphore",
}
RETRY_WORDS = ("retry", "backoff", "reconnect", "restart", "recover")
THREADING_BASES = {
    "ThreadingMixIn",
    "ThreadingHTTPServer",
    "ThreadingTCPServer",
    "ThreadingUDPServer",
    "ThreadingActor",
}


@dataclass(frozen=True)
class RetryFamily:
    family: str
    path: str
    symbol: str
    schedule: str
    cap: str
    budget: str
    logging: str


@dataclass(frozen=True)
class RetryMetrics:
    families: int
    candidates: int
    operation_repeat: int
    classified: int
    limit_class: int
    dismissed: int
    undisposed: int


# Curated classifications over the mechanical candidates.  The script locates
# each symbol by AST on every run, so stale/missing anchors fail loudly.  One
# row may cover several call sites of one shared engine; its schedule and
# budget describe that engine, not every caller.
RETRY_FAMILIES: tuple[RetryFamily, ...] = (
    RetryFamily('actor supervision', 'kernel/impl/actor_runtime/guardian.py', '_process_failure', 'configured base_backoff × 2^(attempt−1), capped at max_backoff, with configured jitter', 'configured max_restarts inside restart_window; defaults are 3/60s, 5/60s, or 2/120s', 'per actor guardian record; no daemon-global restart budget', 'ActorEvent records failure, scheduled delay, restart, or quarantine'),
    RetryFamily('effect completion custody', 'kernel/impl/actor_runtime/effects.py', '_run', 'fixed retry_seconds, default 0.01s', 'uncapped until owner ACK or lane close', 'per EffectLane capacity; no daemon-global budget', 'counter only; no per-attempt log in kernel'),
    RetryFamily('agent-home effect completion custody', 'identity/impl/agents/home/effects.py', '_run', 'fixed retry_seconds, default 0.01s', 'uncapped until owner ACK or close', 'per home effect lane; no daemon-global budget', 'no per-attempt log'),
    RetryFamily('provider-auth helper retry', 'identity/impl/provider_auth/actor/_authority.py', '_retry_loop', 'fixed notification_retry_seconds, 1s by default, through one due-time heap', 'no attempt cap; retained notice/decision/helper/restore state or authority close ends replay', 'one coalesced heap entry per kind/key plus configured provider-auth custody capacities', 'retry counters retained; notice/helper outcomes logged; scheduler attempts are log-silent'),
    RetryFamily('agent state publication retry', 'identity/impl/agents/state/worker_state.py', '_schedule_retry', 'fixed 0.1s threading.Timer after effect error, negative notice result, or overload', 'no attempt cap while the key remains pending; close ends replay', 'one pending entry per failure key; timers have no shared daemon budget', 'timer retries are log-silent'),
    RetryFamily('durable inbox delivery retry', 'daemon/impl/inbox/service/delivery/retry.py', '_log_retry', 'durable backoff_ms tuple, last delay repeats', 'until expiry/terminal outcome; attempts durable', 'per message row; no daemon-global attempt budget', 'outbox retry event for every backoff step'),
    RetryFamily('inbox effect completion settlement', 'daemon/impl/inbox/actor/worker.py', '_settle_completion', '0.005/0.01/0.02/0.05s then 0.05s', 'bounded by 1s settlement deadline per completion', 'per inbox worker completion', 'failure surfaced after deadline; per-sleep log absent'),
    RetryFamily('daemon outbox retry pump', 'daemon/impl/runtime/reconcile.py', '_retry_pump_run', 'next durable due time; failed/empty round has bounded wait', 'thread lifetime; row policy supplies message cap/expiry', 'single daemon pump, per-message durable budgets', 'delivery service logs attempts; pump overrun/fault logging is caller-owned'),
    RetryFamily('recipient wake custody', 'daemon/impl/inbox/links/wake.py', '_wait_retry', 'fixed 0.01s plus 0.1s completion waits by default', 'until settled, superseded, stale, or close', 'one coordinator; 128 pending recipients by default', 'unexpected settlement faults per attempt; admission waits are log-silent'),
    RetryFamily('IPC accept resource-exhaustion retry', 'daemon/impl/ipc/server.py', '_serve', 'exponential from 0.05s, capped at 1s', 'until accept succeeds, server closes, or daemon stops', 'main IPC accept loop and process file-descriptor budget', 'each exhaustion logged best-effort'),
    RetryFamily('lifecycle journal recovery', 'daemon/impl/lifecycle_manager/process.py', '_guarded_recover', '0.05s queue poll plus configured capped backoff on store faults', 'manager lifetime; operation deadline fences new forward work but does not revoke admitted effect custody', 'one process-manager queue; no daemon-global retry budget', 'first and rate-limited aggregate faults, plus recovery total'),
    RetryFamily('harness process reconcile/restart', 'daemon/impl/harnesses/actor/behaviors/reconcile.py', '_on_reconcile', 'restart_after with configured base/exponential backoff', 'per-generation start-attempt budget; 0 explicitly means infinite', 'per harness record, not daemon-global', 'harness state/failure events retain each decision; no separate per-sleep log'),
    RetryFamily('streaming harness reconnect', 'daemon/impl/harnesses/streaming/process.py', '_run', 'exponential from 0.25s, capped at 30s by default', 'process lifetime; no attempt cap in pump', 'per streaming worker process', 'each failed connection is reported through the turn-failure/error path; sleeps are log-silent'),
    RetryFamily('usage collection backoff', 'biz/impl/usage.py', 'refresh_once', '30m refresh cadence; exponential retry-after capped at 30m or 60m', 'usage-cache thread lifetime', 'one cache thread; budget tracked per source', 'failure retained in source snapshot; retry attempts log-silent'),
    RetryFamily('turn delivery retry', 'daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py', '_io_failed', 'no timer or backoff: the failure-decision callback receives retry=True immediately', 'max_delivery_attempts, 5 by default; terminal provider failures stop on attempt 1', 'per turn runtime/pending command', 'turn-retry or turn-abandoned event per failed attempt'),
    RetryFamily('Codex carrier fact retry', 'daemon/impl/harnesses/carrier/codex_carrier_support.py', 'flush', 'retry_seconds * 2^failures, capped at 5s', 'process lifetime; no attempt cap', 'per carrier support instance', 'worker.carrier.error per failed report'),
    RetryFamily('Codex carrier admission replay', 'daemon/impl/harnesses/codex/settlement.py', '_retry_carrier_facts', 'carrier poll cadence; immediate drain until overload', 'until accepted or carrier closes; bounded fact capacity', 'per Codex worker carrier-fact map', 'initial deferral/rejection logged; replay attempts log-silent'),
    RetryFamily('Codex turn-state RPC read retry', 'daemon/impl/harnesses/codex/carrier.py', 'receive_response', '0.5s after CodexAppServerRpcError; successful nonterminal reads are observation polls at the same interval', 'no attempt or elapsed cap; final turn state or interruption ends observation', 'one turn read at a time in the connector process default asyncio executor', 'transient RPC errors are log-silent; final error becomes the turn outcome'),
    RetryFamily('interactive carrier command admission', 'daemon/impl/harnesses/carrier/interactive_carrier_runtime.py', 'submit', 'fixed 0.002s after OVERLOADED', 'caller timeout, 0.25s by default', 'caller thread and one carrier actor mailbox', 'attempts are log-silent; terminal result is OVERLOADED'),
    RetryFamily('Codex app-server startup connection', 'daemon/impl/harnesses/codex/app_server.py', 'start', 'fixed 0.05s after socket absence or connect/initialize failure', 'startup_timeout_seconds wall-clock deadline', 'startup caller and one child process/socket', 'attempt exceptions are swallowed; exhaustion raises TimeoutError'),
    RetryFamily('Codex loaded-thread failover discovery', 'daemon/impl/harnesses/codex/app_server.py', 'discover_thread', 'immediate thread/read against each successive loaded thread ID after CodexAppServerRpcError or a non-candidate result', 'each ID from thread/loaded/list once; no aggregate deadline; each RPC uses request_timeout_seconds (default 15s)', 'one discovery caller serialized by the app-server RPC lock', 'per-ID failures and non-candidates are log-silent; exhaustion raises ConnectionError'),
    RetryFamily('Codex carrier poll-pass retry', 'daemon/impl/harnesses/codex/carrier.py', '_poll_main', 'poll_seconds after a caught poll-pass exception; successful passes use state-machine delay or immediate 0s', 'carrier thread lifetime; no attempt cap', 'one carrier polling thread serializes heartbeat and poll lanes', 'every caught poll-pass exception is logged; waits have no separate log'),
    RetryFamily('DSH resolved-address connection failover', 'daemon/impl/harnesses/dsh/api.py', '_connect_addresses', 'immediate connect against each successive resolved address after OSError', 'each resolved address once; no aggregate deadline; each connect gets the full effective request timeout', 'one HTTP request and its owned candidate sockets', 'per-address failures are log-silent; the final OSError is wrapped in DshApiError by the caller'),
    RetryFamily('Lark adapter process restart', 'daemon/impl/adapters/lark/runtime/handler.py', '_begin_reconcile_spawns', 'named lifecycle policy delay; stale worker pauses 1s before exit', 'max_restarts within restart_window, then per-gateway quarantine', 'per gateway; no daemon-global budget', 'worker observations and restart/quarantine outcomes are logged; the scheduled sleep has no separate log'),
    RetryFamily('Lark reaction completion admission', 'daemon/impl/adapters/lark/outbound/reactions.py', 'completion', 'fixed 0.01s actor-admission retry', '5s deadline', 'pins the ACK or reply native lane completion callback', 'per-attempt and deadline-expiry logs absent'),
    RetryFamily('Lark adapter completion admission', 'daemon/impl/adapters/lark/runtime/runtime.py', 'completion', 'fixed 0.01s actor-admission retry', '5s deadline', 'pins the process-effect completion callback', 'per-attempt logs absent; deadline becomes correlation failure'),
    RetryFamily('Lark inbound transient retry', 'daemon/impl/adapters/lark/inbound/pipeline.py', '_with_transient_retry', 'doubling backoff from configured initial to configured max', 'attempt count and wall-clock deadline', 'per inbound operation', 'helper emits no per-sleep log; terminal exhaustion reports attempts and elapsed time'),
    RetryFamily('identity binding cache rebuild', 'daemon/impl/identity/bindings.py', '_run', 'on change plus 300s JWKS TTL or earlier key recheck', 'projector thread lifetime', 'one identity-binding projector thread', 'each failed rebuild logged; no attempt counter'),
    RetryFamily('identity binding publication retry', 'daemon/impl/identity/bindings.py', 'retry_publish', 'capped exponential from lifecycle constants', 'uncapped attempts while publication remains required', 'single projector pending slot; no daemon-global budget', 'warnings aggregated by warning interval; attempt counter retained'),
    RetryFamily('Zenoh degraded-session recovery', 'daemon/impl/transport/session_recovery.py', '_recover_degraded', '1/2/4/8/16/30s then 30s', 'uncapped until success, close, or another reopen wins', 'one transport session recovery thread', 'last error retained per failure; terminal reopen event logged'),
    RetryFamily('Zenoh native call reopen', 'daemon/impl/transport/zenoh/transport.py', '_with_session', 'immediate single reopen', 'one retry', 'per native call', 'terminal error; retry itself not logged'),
    RetryFamily('transport callback refresh', 'daemon/impl/transport/session_actor.py', '_schedule_recovery', 'fixed 1s, coalesced by generation and recover_after', 'until callbacks complete, close, or session degrades', 'one scheduler entry per transport session', 'callback rejection counted; scheduled attempts log-silent'),
    RetryFamily('session route mutation retry', 'daemon/impl/application/actors/sessions/routes.py', '_settled_route_mutation', 'exponential from 0.005s, capped at 0.5s', 'uncapped except PORT_CLOSING', 'caller thread and shared route-registration authority', 'every failed attempt logged'),
    RetryFamily('org join retry', 'daemon/impl/org/network/service.py', '_join_with_retry', 'fixed 0.5s', 'caller deadline; 30s by default', 'caller thread; no shared budget', 'per-attempt log absent'),
    RetryFamily('org access incomplete rebuild', 'daemon/impl/org/network/access/actor.py', '_retry_later', 'fixed 10s generation-fenced schedule', 'until complete rebuild or close', 'one named scheduler entry per access actor', 'failed allow logged; incomplete retry scheduling log-silent'),
    RetryFamily('orgfs scheduled supplier sync', 'daemon/impl/orgfs/network/supplier_health.py', '_run_scheduled_sync', 'initial attempt plus one retry after 0.25s', '4.5s per run_once; at most one coalesced rerun', 'per-space queue 32 and single orgfs worker; supplier health shared across spaces', 'every failed attempt plus held/deferred/recovered state logged'),
    RetryFamily('orgfs projection repair', 'daemon/impl/orgfs/network/recovery.py', '_repair_projections', '0.25s between attempt 1 and attempt 2 for one failure epoch', '2 attempts per epoch and one reset epoch after a later success; at most 4 failed applications per row per daemon generation', 'per document/writer/sequence failure record; repair runs on the shared orgfs worker', 'abandonment logs at each exhausted epoch; individual retry sleep is log-silent'),
    RetryFamily('orgfs blob recovery', 'daemon/impl/orgfs/network/recovery.py', '_run_blob_recovery', 'immediate next supplier after failure; each discovered supplier is attempted once per recovery job', 'ends on first success or when no unattempted allowed supplier remains', 'one worker future per digest; pending digests and waiters share ORGFS_BLOB_FETCH_QUEUE_LIMIT', 'every supplier failure and terminal drop/recovery outcome is logged'),
    RetryFamily('orgfs content provisioning wait', 'daemon/impl/orgfs/composition/content.py', 'await_content', 'request_blob, wait to caller deadline, then sleep up to 0.1s', 'caller-supplied deadline', 'IPC caller plus per-space blob queue and single orgfs worker', 'content loop log-silent; blob worker logs fetch failure/drop'),
    RetryFamily('orgfs holder failover refresh', 'daemon/impl/orgfs/composition/content.py', 'refresh_space', 'immediate sync_from against each successive online holder after StoreError or OrgFsError', 'each holder once within the caller timeout; deadline is checked before and after each attempt', 'IPC content caller, one mesh, and the snapshotted online-holder set', 'attempt failures are log-silent; aggregate success is returned as a boolean'),
    RetryFamily('orgfs join holder failover', 'daemon/impl/orgfs/composition/lifecycle.py', '_join_sync', 'immediate sync_from against each successive sorted holder after StoreError or OrgFsError', 'each holder once; no aggregate elapsed deadline beyond timeouts inside mesh operations', 'one lifecycle join request, one mesh, and the merged holder snapshot/candidate set', 'attempt failures are log-silent; exhaustion raises the final OrgFsError'),
    RetryFamily('transfer restore observation', 'daemon/impl/application/actors/transfer_p2.py', '_retry_transfer_restore', 'restore-wake cadence, submitted by 1s daemon maintenance tick', 'bounded batch per pass; retained until outcome, stale, or exhaustion', 'one bounded restore-wake lane and deferred map', 'terminal/exhaustion warnings; unsuccessful observations mostly log-silent'),
    RetryFamily('harness restore eligibility replay', 'daemon/impl/application/restore.py', '_retry_restore_eligibility', '1s daemon maintenance tick', 'configured batch; until accepted, stale, or close', 'one bounded pending eligibility map', 'overload logged on initial admission; replay attempts log-silent'),
    RetryFamily('PAC cleanup retry', 'daemon/impl/pac/actors/coordinator/types.py', 'cleanup_retry_delay_ms', '5 minutes × 2^(attempt−1), capped at 6 hours; attempt 0 is immediate', 'no attempt cap; retained until graph cleanup settles', 'per cleanup intent; at most 2 cleanup retries admitted per sweep', 'journal state records each reconcile failure'),
    RetryFamily('routine recovery loop', 'biz/impl/routine/execution/coordinator/__init__.py', '_recover_loop', 'fixed 0.05s after failed recovery', 'coordinator lifetime', 'one routine coordinator thread', 'recovery loop emits no per-attempt log'),
    RetryFamily('routine coordinator completion admission', 'biz/impl/routine/execution/coordinator/__init__.py', '_effect_loop', 'fixed 0.01s after OVERLOADED actor admission', 'no attempt or elapsed cap; accepted/CLOSED admission or coordinator close ends replay', 'pins one routine coordinator effect thread and its operation custody', 'admission retries are log-silent'),
    RetryFamily('turn-pulse filename collision', 'biz/impl/turn_pulse.py', 'signal_turn_ended', 'immediate retry with a new random filename after FileExistsError', 'three attempts', 'one short-lived hook caller and target directory', 'attempts are log-silent; exhaustion raises OSError'),
    RetryFamily('state database connection initialization', 'daemon/impl/state_db.py', '_open', 'SQLite attempt then up to 0.05s sleep', '2s aggregate deadline', 'per-file connection lock; retry runs on caller thread', 'per-attempt and exhaustion logs absent'),
    RetryFamily('state database write-lock acquisition', 'daemon/impl/state_db.py', '_begin_write', 'SQLite 2s busy handler per attempt plus fixed 0.05s sleep', '8s retry deadline by default; final attempt can reach about 10s total', 'per-file write lock; retry runs on caller thread', 'per-attempt and exhaustion logs absent'),
    RetryFamily('lifecycle domain admission', 'daemon/impl/lifecycle_manager/process.py', '_admit', '0.005/0.01/0.02s then 0.02s by default', '1s deadline by default', 'pins the single lifecycle process-manager worker', 'per-attempt log absent; exhaustion becomes PORT_OVERLOADED'),
    RetryFamily('lifecycle completion admission', 'daemon/impl/lifecycle/lifecycle_coordinator.py', '_deliver', 'fixed 0.005s actor-admission retry', 'uncapped until accepted, actor closes, or coordinator closes', 'pins the current lifecycle effect or recovery worker', 'per-attempt log absent'),
    RetryFamily('lifecycle recovery loop', 'daemon/impl/lifecycle/lifecycle_coordinator.py', '_recover_loop', '0.01s between recovered batches; 0.05s idle/fault waits', 'coordinator lifetime', 'single lifecycle recovery thread', 'fault callback receives each caught recovery fault; successful batch retries are log-silent'),
    RetryFamily('session agent-command admission', 'daemon/impl/session_actor/worker.py', '_admit', 'configured backoff tuple; default 0.005/0.01/0.02/0.05s then 0.05s', 'configured wall-clock deadline; 1s by default', 'one bounded Agent-effect queue, capacity 64 by default, and one worker thread per SessionActor', 'attempts are log-silent; terminal failure is returned as _AgentEffectUnavailable'),
    RetryFamily('pending availability notice replay', 'daemon/impl/runtime/forwards.py', '_retry_pending_notices', 'runtime reconciliation cadence and settlement flush', 'no attempt cap; accepted delivery or runtime close removes custody', 'one runtime pending-notice map', 'first failure logged; repeated attempts log-silent'),
    RetryFamily('blocking-failure observer replay', 'daemon/impl/runtime/results.py', '_retry_blocking_failures', 'runtime reconciliation cadence; tight close drain until deadline', 'bounded map; until accepted, observer removal, or close deadline', 'one runtime map with configured capacity', 'capacity exhaustion logged; replay attempts log-silent'),
    RetryFamily('MCP inbox-watch poll recovery', 'daemon/impl/mcp/channel/loops.py', '_run_channel_poll_loop', 'Sleep.ms from the shared inbox-watch state machine; base poll interval is caller-configured', 'no attempt cap; Quiet verdict or task cancellation ends the loop', 'one async poll task per MCP channel', 'each failed non-wait action increments and reports the failure count; wait failures are debug logged'),
    RetryFamily('MCP inbox-watch heartbeat recovery', 'daemon/impl/mcp/channel/loops.py', '_run_channel_heartbeat_loop', 'Sleep.ms from the shared inbox-watch state machine; heartbeat default comes from CHANNEL_HEARTBEAT_INTERVAL_SECONDS', 'no attempt cap; Quiet verdict or task cancellation ends the loop', 'one async heartbeat task per MCP channel', 'each failure increments and reports the failure count'),
    RetryFamily('MCP wake retry', 'daemon/impl/mcp/wake.py', '_default_retry_delay', '1/2/4/8/16/32/60s then 60s for failures; accepted-but-unread resignal is 30/60/120/240/300s then 300s', 'no attempt cap; durable item state, acknowledgement, session removal, or coordinator close ends replay', 'shared wake queue, tail requeue prevents head blocking', 'each attempted wake outcome is sent to the diagnostic logger; log I/O failure is ignored'),
    RetryFamily('MCP stateless reconnect', 'daemon/impl/mcp/proxy.py', 'call', 'immediate reconnect with the same mutation request ID after DaemonDisconnected', 'reconnect_attempts plus the initial attempt; two total by default', 'one MCP tool call and fresh connection per attempt', 'attempts are log-silent; final DaemonDisconnected propagates'),
    RetryFamily('route native partial cleanup', 'daemon/impl/route_registration/io.py', '_execute_native', 'exponential from 0.005s, capped at 0.5s', 'uncapped while preparation cleanup keeps failing', 'shared bounded native queue and 8 workers by default', 'per-attempt log absent; final preparation failure returned'),
    RetryFamily('route completion publication', 'daemon/impl/route_registration/io.py', '_settle', '0.005/0.01/0.02s then 0.02s by default', '1s deadline by default', 'one route owner and per-token settlement state', 'per-attempt and deadline-expiry logs absent'),
    RetryFamily('inbox watch registration/list refresh', 'daemon/impl/inbox_watch.py', 'backoff_ms', 'exponential by failure count', 'watch lifetime', 'one watch actor schedule, separate failure counters', 'pure state machine retains failure counters but emits no log itself; channel loops log/report outcomes'),
    RetryFamily('agent migration owner generation retry', 'identity/impl/agents/actor/_core.py', 'migration_owner_call', '1s event waits; resubmit only after actor generation changes', 'uncapped until a result arrives', 'caller thread and one stable call id per migration owner request', 'per-wait and resubmit logs absent'),
    RetryFamily('forwarding sidecar supervision', 'daemon/impl/forwarding/supervisor.py', '_account_for_failure', 'process-lifecycle SupervisionPolicy exponential delay with jitter via GenerationScheduler', 'max_restarts inside restart_window; exhaustion leaves forwarding down until daemon restart', 'per sidecar supervisor; no daemon-global restart budget', 'every restart decision or exhausted budget emits a warning/error event'),
    RetryFamily('routine effect completion admission', 'biz/impl/routine/execution/service.py', '_submit_completion', 'fixed 0.01s actor-admission retry', 'no attempt cap; accepted admission, actor generation change, or service stop ends replay', 'pins one routine effect worker; configured effect queues bound retained items', 'admission retries are log-silent'),
    RetryFamily('routine alarm effect replay', 'biz/impl/routine/execution/service.py', '_effect_worker_loop', '500ms not-before timestamp; refill polls at up to 50ms while idle', 'no attempt cap while the durable projection keeps the alarm effect pending', 'one effect id and retry timestamp per pending alarm; workers/queues are configured', 'replay scheduling is log-silent; completion carries the alarm error'),
    RetryFamily('Lark inbound completion admission', 'daemon/impl/adapters/lark/inbound/inbound_runtime.py', 'completion', 'fixed 0.01s actor-admission retry', '5s deadline', 'pins the single inbound effect worker completion callback', 'per-attempt logs absent; deadline fails the correlation'),
    RetryFamily('Lark gateway dependency close', 'daemon/impl/adapters/lark/outbound/gateway.py', '_drain_close', 'fixed 0.01s after a false or exceptional 0.05s close attempt', 'no attempt or elapsed cap; successful close ends each dependency', 'one retained closer thread serializes runtime and dependent owners', 'last exception type is retained; attempts have no log'),
    RetryFamily('Lark state WAL transition', 'daemon/impl/adapters/lark/state/schema.py', '_switch_to_wal', 'fixed 0.05s after SQLite OperationalError', 'SQLite busy timeout in _BUSY_TIMEOUT_MS; 5s in this baseline', 'one adapter-state connection; retry runs on its opening thread', 'per-attempt and exhaustion logs absent'),
    RetryFamily('user store WAL transition', 'identity/impl/users/store/_store.py', '_switch_to_wal', 'fixed 0.05s after SQLite OperationalError', 'SQLite busy timeout in _SQLITE_BUSY_MS; 5s in this baseline', 'one user-store connection; retry runs on its opening thread', 'per-attempt and exhaustion logs absent'),
    RetryFamily('transfer quiesce reconciliation', 'daemon/impl/application/actors/transfer_p2.py', '_reconcile_transfer_quiesce', 'invoked by 1s maintenance cadence; scans at most 32 rows per pass', '10s aggregate query/receipt/hold/finish deadline per pass; durable row remains for later passes', 'one maintenance caller plus lifecycle, harness, and transfer authorities', 'unavailable/finish failure and successful completion are logged; skipped rows are log-silent'),
    RetryFamily('harness lifecycle failure admission', 'daemon/impl/composition/harness_ports.py', 'fail_lifecycle', 'fixed up to 0.002s after OVERLOADED admission', 'caller-supplied wall-clock timeout', 'caller thread and harness actor mailbox; a new edge correlation is minted per attempt', 'attempts are log-silent; terminal result is a timeout error'),
    RetryFamily('harness lifecycle close admission', 'daemon/impl/harnesses/actor/behaviors/lifecycle.py', '_close_lifecycle_in_actor', 'fixed up to 0.002s after OVERLOADED admission', 'caller-supplied close deadline', 'close caller and harness lifecycle actor mailbox', 'attempts are log-silent; boolean result reports exhaustion'),
    RetryFamily('home claim contention wait', 'daemon/impl/configuration/home_guard.py', '_claim_record', 'fixed min(0.05s, keepalive_duration) while a final non-advancing heartbeat may clear', 'claim_wait_timeout; advancing heartbeat or expiry fails immediately', 'daemon startup thread and one home marker lock', 'individual waits are log-silent; terminal conflict is HYPRIALHomeInUse'),
    RetryFamily('daemon state ownership lock', 'daemon/impl/configuration/ownership.py', 'acquire', 'fixed max(0.001s, poll_interval), 0.05s poll by default', 'caller timeout, 0s by default', 'caller thread and one daemon.lock file', 'attempts are log-silent; exhaustion raises DaemonOwnershipBusy'),
    RetryFamily('identity transaction lock', 'identity/impl/identity_transaction.py', 'acquire', 'fixed max(0.001s, poll_interval), 0.05s by default', 'caller timeout, 15s by default', 'caller thread and one home-scoped lock file', 'attempts are log-silent; exhaustion raises IdentityTransactionBusy'),
    RetryFamily('squire JSON mutation lock', 'daemon/impl/squire/store_lock.py', 'json_store_mutation', 'up to 0.01s between nonblocking lock attempts', 'caller timeout, 5s by default', 'caller thread and one per-store lock file', 'attempts are log-silent; exhaustion raises TimeoutError'),
    RetryFamily('user JSON mutation lock', 'identity/impl/users/_store_lock.py', 'json_store_mutation', 'up to 0.01s between nonblocking lock attempts', 'caller timeout, 5s by default', 'caller thread and one per-store lock file', 'attempts are log-silent; exhaustion raises TimeoutError'),
    RetryFamily('desired-state late-result join', 'daemon/impl/desired_state_io.py', '_join_late', 'fixed 0.01s correlation lookup poll', 'no local deadline; ends when a result/error appears', 'pins the caller thread and retained desired-state correlation', 'polls are log-silent'),
    RetryFamily('work-item stale-write mutation retry', 'daemon/impl/application/messaging/work_items.py', '_work_mutate', 'immediate reread and rewrite after stale-write conflict', '3 attempts per caller operation', 'one IPC caller and orgfs facade write', 'attempts are log-silent; terminal conflict is returned with attempt count'),
    RetryFamily('work-item stale-write repair retry', 'daemon/impl/application/messaging/work_items.py', '_work_repair', 'immediate reinspection and rewrite after stale-write conflict', '3 attempts per caller operation', 'one IPC caller and orgfs facade write', 'attempts are log-silent; terminal conflict is returned with attempt count'),
    RetryFamily('remote workflow outbox replay', 'daemon/impl/dispatch/remote/__init__.py', '_pump', 'fixed 1s pump cadence; up to 32 durable rows selected each pass', 'no attempt cap; result publication or workflow shutdown ends replay', 'one remote-workflow pump thread and durable SQLite outbox', 'database-unavailable pass logs one warning; row failures remain for later passes without per-row retry log'),
    RetryFamily('remote workflow completion admission', 'daemon/impl/dispatch/remote/__init__.py', '_remote_effect_loop', 'fixed 0.01s after OVERLOADED actor admission', 'no attempt cap; accepted admission, CLOSED result, or workflow stop ends replay', 'pins the single remote effect worker; queue capacity is configured', 'admission retries are log-silent'),
    RetryFamily('streaming failure-observer admission', 'daemon/impl/harnesses/streaming/failure.py', '_observer_drain', 'fixed 0.05s after OVERLOADED observer admission', 'no attempt cap; accepted/closed observer, observer stop, or callback error ends the item', 'one bounded observer queue and one dedicated observer thread', 'callback exceptions and CLOSED rejection are logged; overload retries are log-silent'),
    RetryFamily('streaming close-command admission', 'daemon/impl/harnesses/streaming/process.py', 'stop', 'fixed 0.005s after OVERLOADED admission', 'stop_timeout_seconds wall-clock deadline', 'stop caller and one turn-runtime mailbox', 'attempts are log-silent; exhaustion raises RuntimeError'),
    RetryFamily('harness process I/O completion handoff', 'daemon/impl/harnesses/actor/io_port.py', '_handoff', '0.01s receipt waits, 0.002s admission retries, and 0.05s waits for an untranslatable success after permanent close', 'no elapsed cap; processed receipt or translatable permanent close ends custody', 'one completion thread per submitted future plus ProcessIoPort in-flight/slot limits', 'handoff retries are log-silent; drain exposes retained custody'),
    RetryFamily('turn completion relay admission', 'daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py', '_run', 'fixed 0.002s after OVERLOADED delivery', 'no attempt cap; accepted/closed delivery or relay close ends the item', 'one queue of 8 and one relay thread', 'admission retries are log-silent'),
    RetryFamily('turn completion relay queue admission', 'daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py', '_CompletionRelay', 'queue.put blocks for up to 0.05s per full-queue attempt', 'caller timeout, 0.25s by default', 'caller thread and completion relay queue of 8', 'attempts are log-silent; exhaustion returns OVERLOADED'),
    RetryFamily('lifecycle unresolved forward redrive', 'daemon/impl/lifecycle_manager/process.py', '_execute', 'fixed min(0.02s, completion_timeout) after an unresolved step receipt', 'operation deadline is 70s by default and fences new forward work; an already admitted effect remains in custody until a receipt settles', 'pins the single lifecycle process-manager worker and one journaled operation', 'deadline/failure is journaled; individual redrive sleeps are log-silent'),
    RetryFamily('lifecycle unresolved compensation redrive', 'daemon/impl/lifecycle_manager/process.py', '_compensate', 'fixed min(0.02s, completion_timeout) after an unresolved compensation receipt', 'no separate elapsed cap after compensation starts; receipt settlement or terminal compensation result ends it', 'pins the single lifecycle process-manager worker and one journaled operation', 'terminal result is journaled; individual redrive sleeps are log-silent'),
    RetryFamily('org-context generation reassociation', 'daemon/impl/org/mesh.py', '_submit', 'immediate resubmit only after CLOSED admission and observation of a newer actor generation', 'no admission-attempt cap; accepted or non-reassociable rejection ends it; accepted synchronous calls wait up to 60s', 'one org-context authority with total custody capacity 128', 'resubmits are log-silent'),
    RetryFamily('orgfs lifecycle generation reassociation', 'daemon/impl/orgfs/composition/lifecycle.py', '_ask_lifecycle', 'immediate resubmit only after CLOSED admission and a newer generation', 'no admission-attempt cap; accepted or non-reassociable rejection ends it; accepted call then waits without a deadline', 'one directory lifecycle actor and per-operation waiter maps', 'resubmits are log-silent'),
    RetryFamily('orgfs space-state generation reassociation', 'daemon/impl/orgfs/document/space_state.py', '_submit_space_state', 'immediate resubmit only after CLOSED admission and a newer generation', 'no admission-attempt cap; accepted or non-reassociable rejection ends it; accepted call then waits without a deadline', 'per-space actor custody is capped at _SPACE_STATE_CAPACITY; post-commit EffectLane has its own reservation cap', 'resubmits are log-silent'),
    RetryFamily('orgfs checkout generation reassociation', 'daemon/impl/orgfs/projection/checkout_authority.py', '_ask', 'immediate resubmit only after CLOSED admission and a newer generation', 'no admission-attempt cap; accepted or non-reassociable rejection ends it; accepted wait uses the caller timeout', 'one checkout actor with capacity 32', 'resubmits are log-silent'),
    RetryFamily('orgfs blob generation reassociation', 'daemon/impl/orgfs/storage/blob_authority.py', '_call', 'immediate resubmit only after CLOSED admission and a newer generation', 'no admission-attempt cap; accepted or non-reassociable rejection ends it; accepted wait is 60s', 'one blob actor; mailbox/effect capacity and waiter map bound concurrent custody', 'resubmits are log-silent'),
    RetryFamily('orgfs replica generation reassociation', 'daemon/impl/orgfs/storage/replica_authority.py', '_ask', 'immediate resubmit only after CLOSED admission and a newer generation', 'no admission-attempt cap; accepted or rejection ends it; accepted call then waits without a deadline', 'one replica actor with total custody capacity 128', 'resubmits are log-silent'),
    RetryFamily('orgfs space generation reassociation', 'daemon/impl/orgfs/storage/space_authority.py', '_ask', 'immediate resubmit only after CLOSED admission and a newer generation', 'no admission-attempt cap; accepted or rejection ends it; accepted call then waits without a deadline', 'one space authority with _SPACE_AUTHORITY_CAPACITY', 'resubmits are log-silent'),
    RetryFamily('PAC notification durable send replay', 'daemon/impl/pac/graphs/authority/__init__.py', '_delivery_loop', 'wake-driven scan with at most a 1s wait between scans; selects at most 128 rows', 'no attempt cap for transient send/record failures; durable delivery row or permanent failure record ends replay', 'one graph delivery thread over durable pending rows', 'permanent refusal is recorded; transient send and record failures are log-silent here'),
    RetryFamily('PAC notification completion admission', 'daemon/impl/pac/graphs/authority/__init__.py', '_delivery_loop', 'fixed 0.01s after OVERLOADED actor admission', 'no attempt cap; accepted/CLOSED admission or authority close ends the item', 'pins the one graph delivery thread after a successful external send', 'admission retries are log-silent'),
    RetryFamily('provider-auth code-observation admission', 'identity/impl/provider_auth/actor/_authority.py', 'announce', 'fixed 0.01s after rejected internal admission', 'no attempt cap; accepted admission or provider-auth stop ends replay', 'pins one provider-auth helper worker callback', 'admission retries are log-silent'),
    RetryFamily('route settled-call replay', 'daemon/impl/route_registration/client.py', '_call', '5ms after OVERLOADED admission or unsettled receipt; command wait uses the configured client timeout before resubmission', 'ordinary calls are bounded by client timeout; wait_for_settlement calls have no attempt or elapsed cap', 'caller thread plus shared route-registration authority and correlation router', 'retries are log-silent; bounded exhaustion raises typed errors'),
    RetryFamily('session undelivered admission replay', 'daemon/impl/session_actor/actor.py', '_replay_undelivered', 'fixed 0.005s after rejected admission', 'no attempt cap; accepted admission or session drain ends each item', 'one replay thread per active replay run and the actor-owned undelivered deque', 'admission retries are log-silent'),
    RetryFamily('session generation reconciliation', 'daemon/impl/session_actor/actor.py', '_replay_when_running', '0.005s while generation is not running or custody blocks; 0.01s after desired-state reconciliation fault', 'no attempt cap; successful reconciliation, newer/stopped generation, or drain ends the thread', 'one replay thread per actor generation plus durable effect custody', 'retry sleeps are log-silent; reconciliation fault is only exposed through attempted state'),
    RetryFamily('session internal completion admission', 'daemon/impl/session_actor/actor.py', '_deliver_internal', 'exponential 0.001s doubling to 0.05s', '1s wall-clock deadline; CLOSED terminates immediately', 'caller/effect worker and session actor mailbox', 'attempts and exhaustion are log-silent; boolean result carries failure'),
    RetryFamily('liveliness presence reconciliation', 'daemon/impl/transport/zenoh/presence.py', '_reconcile_confirms', 'up to five reads, each with 1s timeout, with 0.4s between attempts', 'five attempts', 'one liveliness rebuild callback per identity', 'attempt exceptions and negative reads are swallowed; aggregate rebuild result is logged'),
    RetryFamily('machine identifier source failover', 'identity/impl/device/machine_binding.py', '_machine_identifier', 'immediate read of the second Linux machine-ID path after OSError or an empty first value', 'two Linux paths, each once; then the platform-independent host/node fallback; no elapsed deadline', 'one synchronous machine-fingerprint caller', 'source failures are log-silent; the final fallback always returns a value'),
    RetryFamily('Windows IPC response-write settlement', 'kernel/impl/platform/windows_pipe.py', '_settle_response_write', 'poll_interval after ERROR_IO_INCOMPLETE', 'no elapsed cap; Windows terminal OVERLAPPED status ends custody', 'one timed-out response holder per pending Windows pipe write', 'polls are log-silent; terminal OS error is propagated'),
    RetryFamily('Windows IPC busy-pipe connect', 'kernel/impl/platform/windows_pipe.py', 'connect', 'fixed 0.01s after ERROR_PIPE_BUSY', 'caller-supplied wall-clock timeout', 'IPC client caller thread and one named pipe endpoint', 'attempts are log-silent; timeout is typed'),
    RetryFamily('kernel blocking file lock', 'kernel/impl/platform/file_lock.py', 'lock_exclusive', 'fixed 0.05s on Windows lock contention when blocking=True', 'no elapsed cap in the primitive; nonblocking mode fails immediately', 'caller thread and one file descriptor; POSIX blocking is delegated to flock', 'wait attempts are log-silent'),
    RetryFamily('daemon service launchd load', 'daemon/impl/operations/service.py', '_load', 'five launchctl attempts with 0.25/0.5/0.75/1.0s waits after the first four transient failures', 'five attempts; non-transient error terminates immediately', 'service-management caller thread and launchd', 'attempts are log-silent; terminal command error includes stderr'),
    RetryFamily('autoupdate service load', 'daemon/impl/autoupdate/__init__.py', '_load', 'launchd: five attempts with 0.25/0.5/0.75/1.0s waits; systemd: fixed 0.5s', 'launchd: five attempts; systemd: 5s wall-clock deadline; non-transient errors terminate immediately', 'autoupdate-management caller thread and OS service manager', 'attempts are log-silent; terminal command error includes stderr'),
    RetryFamily('harness lifecycle rejection replay', 'daemon/impl/harnesses/actor/behaviors/desired.py', '_continue_lifecycle_rejection', 'configured lifecycle retry base with exponential growth capped at 1s, scheduled by threading.Timer', 'failure_budget attempts, default 3; closing or exhausted budget terminalizes', 'per harness lifecycle correlation and one timer entry', 'timer retries are log-silent; terminal lifecycle failure is published'),
    RetryFamily('orgfs IPC content-operation replay', 'daemon/impl/application/messaging/orgfs_bridge.py', 'with_content_wait', 'after await_content returns, sleep up to 0.25s before rerunning the requested read operation', 'caller waitSeconds deadline: 10s default, constrained to 0..120s', 'IPC request thread plus per-space blob queue and shared orgfs worker', 'retries are log-silent; terminal pending error reports waitedSeconds'),
    RetryFamily('Codex remote settlement retry', 'daemon/impl/harnesses/codex/settlement.py', '_defer_settlement', 'settlement_retry_seconds × 2^(attempt−1), capped at 5s', 'no attempt cap while the delivery snapshot remains pending', 'per delivery snapshot and bounded carrier fact handoff', 'every deferral logs attempt, delay, stage, and error'),
    RetryFamily('PAC actor-effect completion admission', 'daemon/impl/pac/graphs/authority/__init__.py', '_actor_effect_loop', 'fixed 0.01s after OVERLOADED actor admission', 'no attempt cap; accepted/CLOSED admission or authority close ends the item', 'pins one PAC actor-effect worker; actor-effect queue/active-key sets bound work', 'admission retries are log-silent'),
    RetryFamily('stale session-route lock acquisition', 'daemon/impl/application/actors/sessions/routes.py', '_expire_stale_channel_routes_owned', 'fixed 0.005s when a full pass acquires no per-agent route-effect lock', 'no attempt or elapsed cap while any observed stale session remains pending', 'caller or session-route effect worker plus per-agent route locks', 'contention retries are log-silent'),
    RetryFamily('Zenoh startup endpoint rebind', 'daemon/impl/application/netendpoints/endpoints.py', '_open_transport', 'immediate reopen after selecting a new loopback port or removing one best-effort derived listener', 'loopback collisions use the external-I/O restart budget, 5 by default; derived listener removal adds at most one fallback open', 'daemon startup caller; every open may create SDK/native resources before failure', 'attempts are log-silent here; the final derived-listener error is returned to the caller'),
    RetryFamily('inbox unsettled-completion replay', 'daemon/impl/inbox/actor/worker.py', '_retry_unsettled_completions', 'worker idle cadence, at most once after each 0.05s queue wait', 'no attempt or elapsed cap; accepted admission or drain handoff ends custody', 'retains pending count and active recipient keys on one DeliveryIoWorker', 'replay attempts are log-silent'),
    RetryFamily('orgfs inline blob import retry', 'daemon/impl/orgfs/network/recovery.py', '_recover_blob', 'immediate single import replay after fetching the missing digest from the same supplier', 'exactly one replay; repeated rejection or fetch/import exception is terminal for the receive', 'inbound receive caller and blob store; no deferred worker queue on this path', 'fetch/import exception logs blob-fetch-failed; repeated rejection uses normal receive outcome logging'),
    RetryFamily('orgfs commit-many client registration retry', 'daemon/impl/orgfs/storage/store/journal.py', 'commit_many', 'immediate replay after registering newly observed mutation client IDs', 'at most len(items)+2 attempts', 'caller thread, one store lock, and one SQLite transaction per attempt', 'attempts are log-silent; exhaustion raises StoreError'),
    RetryFamily('IPC partial response write retry', 'daemon/impl/ipc/server.py', '_write_ipc_response', 'socket timeout is poll_interval; retry resumes at the unsent byte offset', 'caller-supplied wall-clock timeout; daemon uses 15s', 'one IPC response thread and connection', 'timeouts are log-silent; terminal zero-progress or deadline raises'),
    RetryFamily('smolvm process-group termination retry', 'daemon/impl/transfer/execution/smolvm.py', '_run', 'SIGTERM then SIGKILL, with up to 3s wait after each signal', 'two signal attempts after timeout or interruption', 'one transfer execution caller and its owned process group', 'signal lookup failures are swallowed; timeout/interruption is retained in the result'),
)


def python_files(domains: Iterable[str]) -> list[Path]:
    return sorted(path for domain in domains for path in (SOURCE_ROOT / domain).rglob("*.py"))


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imports(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def resolved_call(node: ast.Call, aliases: dict[str, str]) -> str:
    raw = dotted(node.func) or "<dynamic>"
    first, dot, rest = raw.partition(".")
    resolved = aliases.get(first, first)
    return f"{resolved}.{rest}" if dot else resolved


def owner_for(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    owners: list[str] = []
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            owners.append(current.name)
    return ".".join(reversed(owners)) or "<module>"


def call_label(call: ast.Call) -> str:
    for keyword in call.keywords:
        if keyword.arg in {"name", "thread_name_prefix"}:
            try:
                return ast.unparse(keyword.value).replace("|", "\\|")
            except Exception:
                return "UNKNOWN"
    return "-"


def _resolved_name(node: ast.AST, aliases: dict[str, str]) -> str:
    raw = dotted(node) or ""
    first, dot, rest = raw.partition(".")
    resolved = aliases.get(first, first)
    return f"{resolved}.{rest}" if dot else resolved


def _threaded_base(
    path: Path,
    node: ast.ClassDef,
    *,
    seen: set[tuple[Path, str]] | None = None,
) -> str | None:
    """Return the threaded ancestor name, following source subclass chains."""

    visited = set() if seen is None else seen
    key = (path, node.name)
    if key in visited:
        return None
    visited.add(key)
    tree = parse(path)
    alias_map = imports(tree)
    same_file = {
        item.name: item for item in tree.body if isinstance(item, ast.ClassDef)
    }
    for base in node.bases:
        target = base.value if isinstance(base, ast.Subscript) else base
        resolved = _resolved_name(target, alias_map)
        short = resolved.rsplit(".", 1)[-1]
        if short in THREADING_BASES:
            return short
        if short in same_file:
            inherited = _threaded_base(path, same_file[short], seen=visited)
            if inherited:
                return inherited
        module, _, imported_name = resolved.rpartition(".")
        source = module_path(module) if module else None
        if source is not None:
            definition_path, definition = resolve_definition(source, imported_name, set())
            if isinstance(definition, ast.ClassDef):
                inherited = _threaded_base(
                    definition_path, definition, seen=visited
                )
                if inherited:
                    return inherited
    return None


def _dynamic_threaded_type(call: ast.Call, aliases: dict[str, str]) -> bool:
    if (dotted(call.func) or "") != "type" or len(call.args) < 2:
        return False
    bases = call.args[1]
    if not isinstance(bases, (ast.Tuple, ast.List)):
        return False
    return any(
        _resolved_name(base, aliases).rsplit(".", 1)[-1] in THREADING_BASES
        for base in bases.elts
    )


def execution_sites(
    files: Iterable[Path] | None = None,
) -> tuple[list[tuple[str, int, str, str, str]], list[tuple[str, int, str, str]]]:
    execution: list[tuple[str, int, str, str, str]] = []
    sync: list[tuple[str, int, str, str]] = []
    for path in list(files) if files is not None else python_files(DOMAINS):
        tree = parse(path)
        alias_map = imports(tree)
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                threaded_base = _threaded_base(path, node)
                if threaded_base:
                    kind = (
                        "pykka-actor-thread"
                        if threaded_base == "ThreadingActor"
                        else "inherited-thread-spawner"
                    )
                    execution.append(
                        (rel(path), node.lineno, kind, node.name, threaded_base)
                    )
                continue
            if not isinstance(node, ast.Call):
                continue
            if _dynamic_threaded_type(node, alias_map):
                execution.append(
                    (
                        rel(path), node.lineno, "inherited-thread-spawner",
                        owner_for(node, parents), "ThreadingMixIn",
                    )
                )
                continue
            resolved = resolved_call(node, alias_map)
            short = resolved.rsplit(".", 1)[-1]
            primitive = next(
                (value for key, value in EXECUTION_PRIMITIVES.items() if resolved == key or (key in {"ActorRuntime", "EffectLane", "GenerationScheduler"} and short == key)),
                None,
            )
            if primitive:
                execution.append((rel(path), node.lineno, primitive, owner_for(node, parents), call_label(node)))
            sync_kind = next((value for key, value in SYNC_PRIMITIVES.items() if resolved == key), None)
            if sync_kind:
                sync.append((rel(path), node.lineno, sync_kind, owner_for(node, parents)))
    return sorted(execution), sorted(sync)


def retry_candidates(
    files: Iterable[Path] | None = None,
) -> list[tuple[str, int, int, str, str, str]]:
    """Return lexical and syntax-shape candidates with an explicit criterion."""

    rows: list[tuple[str, int, int, str, str, str]] = []
    for path in list(files) if files is not None else python_files(DOMAINS):
        tree = parse(path)
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            lowered = node.name.lower()
            name_signal = any(word in lowered for word in RETRY_WORDS)
            identifiers = {
                item.id.lower()
                for item in _walk_local(node)
                if isinstance(item, ast.Name)
            } | {
                item.attr.lower()
                for item in _walk_local(node)
                if isinstance(item, ast.Attribute)
            }
            identifier_signal = any(
                word in identifier
                for identifier in identifiers
                for word in RETRY_WORDS
            )
            shape, operations = _retry_shape(node)
            legacy_identifiers = {
                item.id.lower() for item in ast.walk(node) if isinstance(item, ast.Name)
            } | {
                item.attr.lower()
                for item in ast.walk(node)
                if isinstance(item, ast.Attribute)
            }
            legacy_identifier_signal = any(
                word in identifier
                for identifier in legacy_identifiers
                for word in RETRY_WORDS
            )
            legacy_call_names = {
                (dotted(item.func) or "").rsplit(".", 1)[-1].lower()
                for item in ast.walk(node)
                if isinstance(item, ast.Call)
            }
            legacy_loop_delay = any(
                isinstance(item, (ast.For, ast.AsyncFor, ast.While))
                for item in ast.walk(node)
            ) and bool(
                legacy_call_names
                & {"sleep", "wait", "schedule", "timer", "capped_exponential"}
            )
            detected_signal = (
                name_signal
                or identifier_signal
                or legacy_identifier_signal
                or shape != "lexical"
                or legacy_loop_delay
            )
            limit_class_signal = (
                not detected_signal
                and (rel(path), node.name) in LOCATION_LIMIT_CLASSES
            )
            if not detected_signal and not limit_class_signal:
                continue
            signals = ",".join(
                name
                for name, present in (
                    ("name", name_signal),
                    ("identifier", identifier_signal),
                    ("legacy-identifier", legacy_identifier_signal),
                    ("syntax", shape != "lexical"),
                    ("legacy-loop-delay", legacy_loop_delay),
                    ("limit-class", limit_class_signal),
                )
                if present
            )
            loops = sum(
                isinstance(item, (ast.For, ast.AsyncFor, ast.While))
                for item in _walk_local(node)
            )
            evidence = (
                f"signals={signals}; loops={loops}; shape={shape}; "
                f"operations={operations}"
            )
            rows.append(
                (
                    rel(path),
                    node.lineno,
                    node.end_lineno or node.lineno,
                    node.name,
                    owner_for(node, parents),
                    evidence,
                )
            )
    return sorted(rows)


def locate_symbol(path: Path, symbol: str) -> ast.AST:
    tree = parse(path)
    matches = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol]
    if len(matches) != 1:
        raise SystemExit(f"retry anchor {rel(path)}::{symbol} matched {len(matches)} definitions")
    return matches[0]


def module_path(module: str) -> Path | None:
    prefix = "hyprial."
    if not module.startswith(prefix):
        return None
    candidate = ROOT / "src" / Path(*module.split("."))
    as_file = candidate.with_suffix(".py")
    if as_file.exists():
        return as_file
    init = candidate / "__init__.py"
    return init if init.exists() else None


def resolve_definition(
    path: Path, name: str, seen: set[tuple[Path, str]] | None = None
) -> tuple[Path, ast.AST | None]:
    """Follow package re-exports until the defining AST node is found."""

    visited = set() if seen is None else seen
    key = (path, name)
    if key in visited:
        return path, None
    visited.add(key)
    tree = parse(path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return path, node
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                return path, node
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for item in node.names:
            if (item.asname or item.name) != name:
                continue
            target = module_path(node.module)
            if target is not None:
                return resolve_definition(target, item.name, visited)
    return path, None


def _is_protocol(node: ast.ClassDef) -> bool:
    return any(
        (dotted(base.value if isinstance(base, ast.Subscript) else base) or "")
        .rsplit(".", 1)[-1] == "Protocol"
        for base in node.bases
    )


def _annotation_callback(
    path: Path, annotation: ast.AST | None, seen: set[tuple[Path, str]]
) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    alias_map = imports(parse(path))
    for item in ast.walk(annotation):
        raw = dotted(item)
        if not raw:
            continue
        resolved = _resolved_name(item, alias_map)
        if resolved.rsplit(".", 1)[-1] == "Callable":
            return True
        short = raw.split(".", 1)[0]
        target_name = short
        target_path = path
        imported = alias_map.get(short)
        if imported:
            module, _, target_name = imported.rpartition(".")
            target_path = module_path(module) or path
        key = (target_path, target_name)
        if key in seen:
            continue
        definition_path, definition = resolve_definition(
            target_path, target_name, set()
        )
        if definition is not None and _callback_bearing(
            definition_path, definition, seen | {key}
        ):
            return True
    return False


def _function_annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    annotations = [item.annotation for item in args if item.annotation is not None]
    if node.args.vararg and node.args.vararg.annotation:
        annotations.append(node.args.vararg.annotation)
    if node.args.kwarg and node.args.kwarg.annotation:
        annotations.append(node.args.kwarg.annotation)
    if node.returns is not None:
        annotations.append(node.returns)
    return annotations


def _callback_bearing(
    path: Path, node: ast.AST, seen: set[tuple[Path, str]] | None = None
) -> bool:
    visited = set() if seen is None else seen
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return any(
            _annotation_callback(path, annotation, visited)
            for annotation in _function_annotations(node)
        )
    if isinstance(node, ast.ClassDef):
        methods = [
            item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if _is_protocol(node) and (
            any(item.name == "__call__" for item in methods)
            or any(isinstance(base, ast.Subscript) for base in node.bases)
            or any(item.name in {"observe", "publish", "submit"} for item in methods)
        ):
            return True
        annotations = [
            item.annotation for item in node.body if isinstance(item, ast.AnnAssign)
        ]
        init = next((item for item in methods if item.name == "__init__"), None)
        if init is not None:
            annotations.extend(_function_annotations(init))
        return any(
            _annotation_callback(path, annotation, visited)
            for annotation in annotations
        )
    if isinstance(node, ast.AnnAssign):
        return _annotation_callback(path, node.annotation, visited)
    if isinstance(node, ast.Assign):
        return _annotation_callback(path, node.value, visited)
    return False


def signature(path: Path | None, node: ast.AST | None) -> tuple[str, str, str]:
    callback = (
        "yes" if path is not None and node is not None and _callback_bearing(path, node) else "no"
    )
    if isinstance(node, ast.AsyncFunctionDef):
        text = f"({ast.unparse(node.args)}) -> {ast.unparse(node.returns) if node.returns else 'UNKNOWN'}"
        return "async", text, callback
    if isinstance(node, ast.FunctionDef):
        text = f"({ast.unparse(node.args)}) -> {ast.unparse(node.returns) if node.returns else 'UNKNOWN'}"
        return "sync; blocking UNKNOWN", text, callback
    if isinstance(node, ast.ClassDef):
        methods = [item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
        init = next((item for item in methods if item.name == "__init__"), None)
        fields = [f"{item.target.id}: {ast.unparse(item.annotation)}" for item in node.body if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)]
        if _is_protocol(node):
            ports = [
                f"{item.name}({ast.unparse(item.args)}) -> {ast.unparse(item.returns) if item.returns else 'UNKNOWN'}"
                for item in methods
                if not item.name.startswith("_") or item.name == "__call__"
            ]
            text = f"protocol({'; '.join(ports)})"
        else:
            text = f"class({'; '.join(fields)})" if fields else (f"class({ast.unparse(init.args)})" if init else "class")
        return "sync object; method blocking UNKNOWN", text, callback
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        annotation = ast.unparse(node.annotation) if isinstance(node, ast.AnnAssign) else ast.unparse(node.value)
        return "data", annotation, callback
    return "module/data", "module", callback


def kernel_exports() -> list[tuple[str, str, int, str, str, str, int]]:
    interface = SOURCE_ROOT / "kernel" / "interface.py"
    tree = parse(interface)
    import_rows: dict[str, tuple[str, str]] = {}
    all_names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                import_rows[item.asname or item.name] = (node.module, item.name)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            all_names = [item.value for item in node.value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
    consumers: dict[str, set[str]] = collections.defaultdict(set)
    for path in python_files(UPPER_DOMAINS):
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.ImportFrom) and node.module in {"hyprial.kernel", "hyprial.kernel.interface"}:
                for item in node.names:
                    if item.name != "*":
                        consumers[item.name].add(rel(path))
    rows = []
    for name in all_names:
        module, original = import_rows.get(name, ("UNKNOWN", name))
        source = module_path(module)
        definition_path, node = (
            resolve_definition(source, original) if source else (None, None)
        )
        mode, types, callback = signature(definition_path, node)
        rows.append((name, rel(definition_path) if definition_path else module, getattr(node, "lineno", 0), mode, types.replace("|", "\\|").replace("\n", " "), callback, len(consumers[name])))
    return rows


def source_baseline() -> str:
    result = subprocess.run(
        ["git", "diff", "--quiet", SURVEY_BASELINE, "--", "src"], cwd=ROOT
    )
    if result.returncode:
        raise SystemExit(
            f"product source differs from survey baseline {SURVEY_BASELINE}"
        )
    return SURVEY_BASELINE


def print_execution() -> None:
    execution, sync = execution_sites()
    counts = collections.Counter(row[2] for row in execution)
    print(f"## Execution-site evidence\n\nSource baseline: `{source_baseline()}`. Execution construction sites: **{len(execution)}**; synchronization construction sites: **{len(sync)}**.\n")
    print("Execution counts: " + ", ".join(f"{key}={counts[key]}" for key in sorted(counts)) + ".\n")
    print("| file:line | primitive | lexical owner | configured name/prefix |")
    print("|---|---|---|---|")
    for path, line, primitive, owner, label in execution:
        print(f"| `{path}:{line}` | {primitive} | `{owner}` | `{label}` |")
    print("\nSynchronization counts: " + ", ".join(f"{key}={value}" for key, value in sorted(collections.Counter(row[2] for row in sync).items())) + ".")


def _dismissal_reason(path: str, symbol: str, evidence: str) -> str | None:
    """Apply the operation-repeat criterion; never infer semantics from names."""

    location_reason = LOCATION_DISMISSALS.get((path, symbol))
    if location_reason is not None:
        return location_reason
    if symbol in SPECIAL_DISMISSALS:
        return SPECIAL_DISMISSALS[symbol]
    if "shape=lexical" in evidence:
        return "lexical retry/recovery token only; no repeated operation shape"
    if "shape=observation" in evidence:
        return "observation wait: the loop only polls state and does not replay work"
    if "shape=cadence" in evidence:
        return "loop/cadence without an operation repeated after delay or failure"
    if "shape=retry-failover" in evidence:
        return (
            "failover-shape review: iterates independent items or performs an "
            "observation/search; it does not repeat one logical effect against alternatives"
        )
    return None


def retry_dispositions(
    candidates: list[tuple[str, int, int, str, str, str]],
) -> list[tuple[str, int, str, str, str, str]]:
    overlap = set(LOCATION_LIMIT_CLASSES) & set(LOCATION_DISMISSALS)
    if overlap:
        raise SystemExit(f"retry rows are both limit-class and dismissed: {sorted(overlap)}")
    anchors: list[tuple[RetryFamily, int, int]] = []
    known_families = {family.family for family in RETRY_FAMILIES}
    unknown_related = set(RELATED_CLASSIFICATIONS.values()) - known_families
    if unknown_related:
        raise SystemExit(f"unknown related retry families: {sorted(unknown_related)}")
    for family in RETRY_FAMILIES:
        node = locate_symbol(SOURCE_ROOT / family.path, family.symbol)
        anchors.append((family, node.lineno, node.end_lineno or node.lineno))
    rows: list[tuple[str, int, str, str, str, str]] = []
    covered_families: set[str] = set()
    seen_limit_classes: set[tuple[str, str]] = set()
    for path, line, end_line, symbol, owner, evidence in candidates:
        matches = sorted(
            family.family
            for family, anchor_line, anchor_end in anchors
            if path == f"src/hyprial/{family.path}"
            and line <= anchor_end
            and anchor_line <= end_line
        )
        related = RELATED_CLASSIFICATIONS.get((path, symbol))
        if related is not None:
            matches.append(related)
        if matches:
            disposition = "classified"
            matched = set(matches)
            covered_families.update(matched)
            reason = ", ".join(sorted(matched))
        elif (path, symbol) in LOCATION_LIMIT_CLASSES:
            shape, detail = LOCATION_LIMIT_CLASSES[(path, symbol)]
            disposition = f"limit-class:{shape}"
            reason = detail
            seen_limit_classes.add((path, symbol))
        else:
            reason = _dismissal_reason(path, symbol, evidence)
            disposition = "dismissed" if reason is not None else "UNDISPOSED"
        rows.append((path, line, f"{owner}.{symbol}", evidence, disposition, reason or "-"))
    missing_families = known_families - covered_families
    if missing_families:
        raise SystemExit(
            f"retry families without a detected candidate: {sorted(missing_families)}"
        )
    missing_limits = set(LOCATION_LIMIT_CLASSES) - seen_limit_classes
    if missing_limits:
        raise SystemExit(
            f"limit classes without a detected candidate: {sorted(missing_limits)}"
        )
    return rows


def retry_metrics() -> RetryMetrics:
    candidates = retry_candidates()
    dispositions = retry_dispositions(candidates)
    counts = collections.Counter(row[4] for row in dispositions)
    disposed = (
        counts["classified"]
        + sum(
            count
            for disposition, count in counts.items()
            if disposition.startswith("limit-class:")
        )
        + counts["dismissed"]
        + counts["UNDISPOSED"]
    )
    if disposed != len(candidates):
        raise SystemExit(
            f"retry disposition count mismatch: candidates={len(candidates)}, disposed={disposed}"
        )
    return RetryMetrics(
        families=len(RETRY_FAMILIES),
        candidates=len(candidates),
        operation_repeat=sum("shape=retry" in row[3] for row in dispositions),
        classified=counts["classified"],
        limit_class=sum(row[4].startswith("limit-class:") for row in dispositions),
        dismissed=counts["dismissed"],
        undisposed=counts["UNDISPOSED"],
    )


def limit_class_details() -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    detail_keys: set[tuple[str, str]] = set()
    for shape, path_text, symbol, schedule, cap, budget, logging, starvation in LIMIT_CLASS_DETAILS:
        key = (path_text, symbol)
        detail_keys.add(key)
        configured = LOCATION_LIMIT_CLASSES.get(key)
        if configured is None or configured[0] != shape:
            raise SystemExit(f"limit-class detail has no matching disposition: {key}")
        node = locate_symbol(ROOT / path_text, symbol)
        rows.append(
            (
                f"limit-class:{shape}",
                f"{path_text}:{node.lineno}",
                symbol,
                schedule,
                cap,
                budget,
                logging,
                starvation,
            )
        )
    if len(detail_keys) != len(LIMIT_CLASS_DETAILS):
        raise SystemExit("duplicate limit-class detail rows")
    return rows


def limit_class_instances() -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for shape, path_text, symbol, needle, detail in LIMIT_CLASS_INSTANCES:
        path = ROOT / path_text
        node = locate_symbol(path, symbol)
        lines = path.read_text(encoding="utf-8").splitlines()
        start = node.lineno
        end = node.end_lineno or start
        matches = [
            number
            for number in range(start, end + 1)
            if needle in lines[number - 1]
        ]
        if len(matches) != 1:
            raise SystemExit(
                f"limit-class anchor must match once in {path_text}::{symbol}: {needle!r}"
            )
        rows.append((shape, f"{path_text}:{matches[0]}", symbol, detail))
    return rows


def print_retries(*, all_candidates: bool = False) -> None:
    candidates = retry_candidates()
    dispositions = retry_dispositions(candidates)
    high_signal = [row for row in dispositions if "shape=retry" in row[3]]
    counts = collections.Counter(row[4] for row in dispositions)
    limit_count = sum(row[4].startswith("limit-class:") for row in dispositions)
    if counts["UNDISPOSED"]:
        unknown = ", ".join(
            f"{row[0]}:{row[1]} ({row[2]})"
            for row in dispositions
            if row[4] == "UNDISPOSED"
        )
        raise SystemExit(f"undisposed retry candidates: {unknown}")
    print(f"## Retry evidence\n\nSource baseline: `{source_baseline()}`. Curated retry/restart/reconnect families: **{len(RETRY_FAMILIES)}**. Mechanical retry-shape candidates: **{len(candidates)}**; operation-repeat shape candidates: **{len(high_signal)}**.\n")
    print(
        "Disposition counts: "
        f"classified={counts['classified']}, limit-class={limit_count}, "
        f"dismissed={counts['dismissed']}, undisposed={counts['UNDISPOSED']}. "
        "Evidence qualification: inventory complete with respect to the detected "
        "shapes listed in Method coverage; shapes listed as blind spots are flagged, not enumerated "
        "exhaustively.\n"
    )
    print("| family | anchor | delay schedule | cap | budget scope | attempt logging |")
    print("|---|---|---|---|---|---|")
    for row in RETRY_FAMILIES:
        node = locate_symbol(SOURCE_ROOT / row.path, row.symbol)
        values = [row.family, f"`src/hyprial/{row.path}:{node.lineno}` `{row.symbol}`", row.schedule, row.cap, row.budget, row.logging]
        print("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
    print("\n### Limit-class mechanism details\n")
    print("| limit class | file:line | lexical scope | schedule | cap | budget scope | logging | starvation evidence |")
    print("|---|---|---|---|---|---|---|---|")
    for detail in limit_class_details():
        shape, location, symbol, *rest = detail
        values = [shape, f"`{location}`", f"`{symbol}`", *rest]
        print("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
    print("\n### Cross-scope method-limit instances\n")
    print("| limit class | file:line | lexical scope | evidence |")
    print("|---|---|---|---|")
    for shape, location, symbol, detail in limit_class_instances():
        values = [f"limit-class:{shape}", f"`{location}`", f"`{symbol}`", detail]
        print("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")
    shown = dispositions if all_candidates else high_signal
    qualifier = "full review superset" if all_candidates else "high-signal subset"
    print(f"\n### Mechanical retry-shape candidate dispositions ({qualifier})\n")
    if not all_candidates:
        print("Pass `--all-candidates` to print all classified and dismissed rows.\n")
    print("| file:line | lexical scope | syntax | disposition | family or dismissal reason |")
    print("|---|---|---|---|---|")
    for path, line, owner, evidence, disposition, reason in shown:
        values = [f"`{path}:{line}`", f"`{owner}`", evidence, disposition, reason]
        print("| " + " | ".join(value.replace("|", "\\|") for value in values) + " |")


def print_kernel() -> None:
    rows = kernel_exports()
    counts = collections.Counter(row[3] for row in rows)
    callback_count = sum(row[5] == "yes" for row in rows)
    zero_consumers = sum(row[6] == 0 for row in rows)
    print(f"## Kernel boundary evidence\n\nSource baseline: `{source_baseline()}`. `kernel/interface.py` exports: **{len(rows)}**; callback-bearing surfaces: **{callback_count}**; exports with zero explicit upper-layer consumers: **{zero_consumers}**. Call shapes: " + ", ".join(f"{key}={counts[key]}" for key in sorted(counts)) + ".\n")
    print("Consumer count is the number of distinct Python files in identity, daemon, biz, and shell with an explicit `from hyprial.kernel[.interface] import NAME`. `sync` describes Python call shape only; whether it blocks is UNKNOWN unless a narrower contract proves it.\n")
    print("| export | definition | call shape | crossing signature/type | Python callback | consumers |")
    print("|---|---|---|---|---|---:|")
    for name, path, line, mode, types, callback, consumers in rows:
        location = f"`{path}:{line}`" if line else f"`{path}`"
        print(f"| `{name}` | {location} | {mode} | `{types}` | {callback} | {consumers} |")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("section", choices=("execution", "retries", "kernel", "all"), nargs="?", default="all")
    parser.add_argument(
        "--all-candidates",
        action="store_true",
        help="print all retry-shape candidates instead of the high-signal subset",
    )
    args = parser.parse_args()
    if args.section in {"execution", "all"}:
        print_execution()
    if args.section in {"retries", "all"}:
        print_retries(all_candidates=args.all_candidates)
    if args.section in {"kernel", "all"}:
        print_kernel()


if __name__ == "__main__":
    main()
