"""Static disposition rules for kernel isolation retry evidence."""

RELATED_CLASSIFICATIONS = {
    ("src/hyprial/biz/impl/usage.py", "_run"): "usage collection backoff",
    ("src/hyprial/daemon/impl/application/messaging/identity_binding.py", "_retry_identity_binding_publication"): "identity binding publication retry",
    ("src/hyprial/daemon/impl/harnesses/streaming/process.py", "_backoff_delay"): "streaming harness reconnect",
    ("src/hyprial/daemon/impl/inbox/actor/custody/retry.py", "_claim_retry"): "durable inbox delivery retry",
    ("src/hyprial/daemon/impl/inbox/actor/custody/retry.py", "_claim_custody_retry"): "durable inbox delivery retry",
    ("src/hyprial/daemon/impl/inbox/actor/custody/retry.py", "_apply_inflight_wakes"): "durable inbox delivery retry",
    ("src/hyprial/daemon/impl/inbox/authority/facade.py", "retry_due"): "durable inbox delivery retry",
    ("src/hyprial/daemon/impl/inbox/authority/facade.py", "retry_custody_due"): "durable inbox delivery retry",
    ("src/hyprial/daemon/impl/inbox/links/wake.py", "_recover_overflow"): "recipient wake custody",
    ("src/hyprial/daemon/impl/inbox/links/wake.py", "_settle"): "recipient wake custody",
    ("src/hyprial/daemon/impl/mcp/wake.py", "dispatch_due"): "MCP wake retry",
    ("src/hyprial/daemon/impl/inbox/service/core.py", "retry_due"): "durable inbox delivery retry",
    ("src/hyprial/daemon/impl/inbox/service/delivery/retry.py", "retry_custody_due"): "durable inbox delivery retry",
    ("src/hyprial/daemon/impl/lifecycle_manager/process.py", "_note_recover_fault"): "lifecycle journal recovery",
    ("src/hyprial/daemon/impl/orgfs/network/recovery.py", "_projection_retry_due"): "orgfs projection repair",
    ("src/hyprial/daemon/impl/orgfs/network/recovery.py", "_retry_failed_projection"): "orgfs projection repair",
    ("src/hyprial/daemon/impl/orgfs/network/recovery.py", "_defer_blob_recovery"): "orgfs inline blob import retry",
    ("src/hyprial/daemon/impl/route_registration/io.py", "_service_resettlements"): "route completion publication",
    ("src/hyprial/daemon/impl/runtime/reconcile.py", "_kick_retry_pump"): "daemon outbox retry pump",
    ("src/hyprial/daemon/impl/runtime/settlement.py", "_retry_pump_wait_seconds"): "daemon outbox retry pump",
    ("src/hyprial/daemon/impl/transport/session_recovery.py", "_schedule_degraded_recovery"): "Zenoh degraded-session recovery",
    ("src/hyprial/identity/impl/agents/state/worker_state.py", "_retry"): "agent state publication retry",
    ("src/hyprial/identity/impl/provider_auth/actor/_authority.py", "_schedule_retry"): "provider-auth helper retry",
    ("src/hyprial/kernel/impl/actor_runtime/guardian.py", "_restart_attempt_failed"): "actor supervision",
    ("src/hyprial/kernel/impl/actor_runtime/guardian.py", "_restart"): "actor supervision",
    ("src/hyprial/kernel/impl/platform/windows_pipe.py", "_complete_response_write"): "Windows IPC response-write settlement",
    ("src/hyprial/kernel/impl/platform/windows_pipe.py", "_sendall"): "Windows IPC response-write settlement",
    ("src/hyprial/daemon/impl/transport/zenoh/transport.py", "_register"): "Zenoh native call reopen",
    ("src/hyprial/daemon/impl/pac/graphs/reactor.py", "_deliver"): "PAC notification durable send replay",
    ("src/hyprial/daemon/impl/pac/graphs/reactor.py", "resend_undelivered"): "PAC notification durable send replay",
}

# These rows repeat work through a later pass, but the current shape scanner
# cannot prove the cross-call control flow.  They are evidence limits, not
# dismissals.  B1's runtime RetryPolicy gate is the completeness control.
LOCATION_LIMIT_CLASSES = {
    ("src/hyprial/daemon/impl/links/service/core.py", "_loop"): (
        "periodic-reentry",
        "tick retains failed link/apply or mirror work for a later service pass",
    ),
    ("src/hyprial/daemon/impl/org/network/service.py", "process_leave_requests"): (
        "periodic-reentry",
        "a failed member removal remains a pending leave request for the next pass",
    ),
    ("src/hyprial/daemon/impl/orgfs/composition/directory.py", "_anti_entropy_tick"): (
        "periodic-reentry",
        "a later anti-entropy tick can revisit work not completed by a failed pass",
    ),
    ("src/hyprial/daemon/impl/pac/actors/gc.py", "_run"): (
        "periodic-reentry",
        "the timer survives run_pass failure and revisits still-eligible cleanup work",
    ),
    ("src/hyprial/daemon/impl/service_connect/manager.py", "_scheduled_reconcile"): (
        "periodic-reentry",
        "the scheduled pass retains failed teardowns and desired mappings for later _unmap/_map attempts",
    ),
}

LIMIT_CLASS_DETAILS = (
    (
        "periodic-reentry",
        "src/hyprial/daemon/impl/service_connect/manager.py",
        "_scheduled_reconcile",
        "fixed 5.0s after each completed pass; finally rearms one generation-fenced scheduler entry",
        "manager lifetime; no attempt or aggregate pass deadline; each _map/_unmap controller wait is up to 3.0s without cancelling accepted work",
        "one generation-fenced scheduler entry; service-control EffectLane capacity 16 with one worker; one manager RLock; no daemon-global retry budget",
        "mapping and aggregate reconciliation transitions are fingerprint-deduplicated; successful closes log; repeated identical failures are log-silent",
        "reconcile holds the manager lock across slow controller calls; service.list/connect/disconnect need the same lock and can block behind their cumulative wait",
    ),
)

# Known instances that demonstrate a static-analysis blind spot.  ``needle``
# is resolved to a baseline-bound file:line by the evidence generator.
LIMIT_CLASS_INSTANCES = (
    (
        "periodic-reentry",
        "src/hyprial/daemon/impl/links/service/core.py",
        "_loop",
        "self.tick()",
        "LinkService._loop periodically calls tick after earlier passes return or fail",
    ),
    (
        "periodic-reentry",
        "src/hyprial/daemon/impl/links/service/core.py",
        "tick",
        "except Exception as error:  # noqa: BLE001 - retry the whole pass (B15)",
        "LinkService.tick returns after apply failure; LinkService._loop later calls tick again",
    ),
    (
        "periodic-reentry",
        "src/hyprial/daemon/impl/links/store.py",
        "_write_everywhere",
        "except Exception:  # noqa: BLE001 - retry this mirror next pass",
        "LinkStore._write_everywhere retains a failed mirror for the next flush pass",
    ),
    (
        "periodic-reentry",
        "src/hyprial/daemon/impl/links/store.py",
        "_resolve_stale",
        "except Exception:  # noqa: BLE001 - retry this mirror next pass",
        "LinkStore._resolve_stale retains a failed rewrite for the next flush pass",
    ),
)

LOCATION_DISMISSALS = {
    ("src/hyprial/daemon/impl/adapters/lark/inbound/reconcile.py", "reconcile_recent"): "bounded scan classifies distinct dead-letter rows; it does not replay a failed row in the loop",
    ("src/hyprial/daemon/impl/adapters/lark/outbound/reactions.py", "wait_idle"): "each admitted barrier is a fresh state observation; no failed effect is replayed",
    ("src/hyprial/daemon/impl/adapters/lark/worker/process.py", "_read_health_stream"): "stream loop parses new frames; malformed frames are discarded, not replayed",
    ("src/hyprial/daemon/impl/adapters/lark/worker/sweep.py", "_run"): "a coalesced pending reconnect requests one fresh sweep after success; failures terminate",
    ("src/hyprial/daemon/impl/harnesses/actor/behaviors/process_events.py", "_drive_ensure_failure_fence"): "loops visit distinct start attempts and timers; cancellation is not retried after failure",
    ("src/hyprial/daemon/impl/harnesses/dsh_tui/carrier.py", "watch_tui_process"): "periodic process-state observation; run_sync does not repeat failed work",
    ("src/hyprial/daemon/impl/harnesses/turn_delivery/channel/worker_relay.py", "_accept"): "accept loop starts one handler for each distinct connection; failed accepts terminate or time out",
    ("src/hyprial/daemon/impl/inbox/service/core.py", "__init__"): "constructor validates distinct policy entries; no operation is replayed",
    ("src/hyprial/daemon/impl/lifecycle/maintenance_watchdog.py", "watch"): "periodic watchdog samples are distinct commands, not replay of a failed sample",
    ("src/hyprial/daemon/impl/orgfs/composition/directory.py", "_execute_directory_effect"): "loops schedule distinct directory rows; no failed submission is replayed here",
    ("src/hyprial/daemon/impl/orgfs/storage/replica.py", "prime_from"): "loops consume distinct exported envelopes; failures propagate instead of replaying an envelope",
    ("src/hyprial/daemon/impl/pac/storage/legacy.py", "_ensure_effect_archive_tables"): "migration loop executes distinct schema statements once each",
    ("src/hyprial/daemon/impl/pac/storage/legacy.py", "_copy_and_empty_effect_tables"): "migration loop executes distinct table statements once each",
    ("src/hyprial/identity/impl/agents/registry/_base.py", "_rebuild_agents_table"): "migration loops execute distinct rows/statements; transaction failure propagates",
    ("src/hyprial/kernel/impl/actor_runtime/guardian.py", "drain"): "drain loop visits distinct actor records and only observes their settlement",
}

SPECIAL_DISMISSALS = {
    "_write_recaps": "queue idle wait; each recap is attempted once",
    "_remove_agent_routines": "waits for accepted routine removals; it does not resubmit them",
    "_remove_registered_routine": "joins one accepted routine operation; it does not resubmit it",
    "_ipc_routine_remove": "joins one accepted routine operation; it does not resubmit it",
    "offer": "waits for one correlated dispatch decision; it does not replay the offer",
    "_record_failure": "records restart state; the classified harness policy owns replay",
    "invoke": "polls for one hook reply until its deadline; it does not resubmit the hook",
    "_on_io_completion": "records completion state and due time; the inbox worker owns replay",
    "online_retry_next_attempt_ms": "pure due-time calculation; durable inbox delivery owns execution",
    "recover": "reads or joins recovery state once; it does not replay a failed command",
    "retry_cleanup": "one explicit native-cleanup pass; this function has no internal replay schedule",
    "_report_turn_failure": "bounded wait for one actor decision; it does not resubmit failed work",
    "_work": "executes one queued lifecycle effect; completion replay is classified separately",
}
