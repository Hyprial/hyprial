"""Metadata shared by the native resident inventory tests.

Pre-package paths are retained so registry-union merges do not erase historical attribution:
archive/bundle.py since 4008bfba0, inbox_surface/history.py since the module became the history
package, the five named org/network admission paths retired by sidecar-owned v6 membership, and
the eleven strict GUI IPC v2 / onboarding control modules retired with the strict control surface
(#2540). The ten work-specific handler and catalog modules were retired by #2214
when work became ordinary OrgFS documents. The inventory still asserts that these
metadata-only paths are absent; their original ownership attribution is retained.
The 28 transfer P2 custody, SSH orchestration and harness start-hold modules were retired
when ``teleport.run`` became the only move path.
The routine_defaults package was retired when the ``routine`` cell kind and the agent-default
override were removed. The three composition/pac_mirror modules were retired when the ``graph``
cell kind and the PAC graph mirror were removed.
application/onboarding/http.py was retired when the ownerless HTTP login host was deleted.
inbox_surface/link_send.py was retired with the send-link gate (T1b); its surviving
halves live in send_receipts.py and the threads surface pending T2b.
inbox_surface/agent_bot.py and agent_bot_attempts.py were retired when the agent bot's DM to
the owner by open id was deleted (R2, IM binding removal).
The five inbox/relay modules were retired with owner relay evidence (tier-2 IM binding removal).
onboarding/operations/custody.py was retired when the launcher's logout coordinator was deleted.
The identity resolver, its override store and audit, the binding publication and suppression
modules, the application binding mixin and the identity binding audit request (13 modules) were
retired when the user-to-IM binding chain was deleted (R3, IM binding removal).
harnesses/protocol/windows_pty.py was retired when the daemon PTY connector launch path was deleted.
kernel/impl/facts/launch_spec.py was retired when the 999-line module was split into the
same-name launch_spec package (args/spec facade, agy Slice 0); the package rows stay live.
owner_migration/user_agent.py was retired when each machine's user agent became an
independent agent with a registry-minted id, so no owner change re-keys it.
The six Squire setup-surface modules (agent_templates setup/home_layout/store_lock
and request_catalog/setup squire_operations/squire_specs/squire_examples) were
retired with the dedicated Squire setup surface (S2b, 2026-11-20).
The nine smolvm runtime/rollback and dead transfer provenance/landing modules
(harnesses/smolvm_runtime, transfer/execution/smolvm, transfer/landing/runtime,
transfer/__init__, transfer/archive attribution/signature/trust and
transfer/landing owner/signature_gate) were retired by W1D-1 (2026-11-21).
The six daemon/impl/context_snapshot modules were retired with W1B context_snapshot removal.
daemon/impl/org/membership/personal.py was retired when OC4 removed personal organizations.
daemon/impl/harnesses/python_worker/_user_proxy_worker.py was retired when I7b-2 deleted the user-proxy harness kind.
The ten daemon/impl/authority_set and daemon/impl/authority_service modules were
retired by I6-5b (member allow/deny moved into cell member reconcile).
request_catalog/adapter/routes/compat.py and compat_examples.py were retired with the
adapter.route-config.* twin (F1a, 2026-11-23).
inbox_surface/send_targets/route.py was retired with the route: send front door (I4b, 2026-11-24).
"""
HISTORICAL_ROWS = frozenset(
    {
        "daemon/impl/application/actors/transfer.py",
        "daemon/impl/application/actors/transfer_p2.py",
        "daemon/impl/application/messaging/inbox_surface/link_send.py",
        "daemon/impl/application/onboarding/http.py",
        "daemon/impl/harnesses/smolvm_runtime/__init__.py",
        "daemon/impl/inbox/relay/__init__.py",
        "daemon/impl/inbox/relay/attestation.py",
        "daemon/impl/inbox/relay/schema.py",
        "daemon/impl/inbox/relay/surface.py",
        "daemon/impl/inbox/relay/verdict.py",
        "daemon/impl/onboarding/operations/custody.py",
        "daemon/impl/owner_migration/__init__.py",
        "daemon/impl/owner_migration/apply.py",
        "daemon/impl/owner_migration/planning.py",
        "daemon/impl/owner_migration/user_agent.py",
        "daemon/impl/owner_migration/vocabulary.py",
        "daemon/impl/harnesses/python_worker/_user_proxy_worker.py",
        "daemon/impl/application/messaging/inbox_surface/send_targets/route.py",
        "daemon/impl/harnesses/actor/behaviors/transfer.py",
        "daemon/impl/harnesses/actor/behaviors/delivery.py",
        "daemon/impl/harnesses/runtime/transfer_ports.py",
        "daemon/impl/transfer/__init__.py",
        "daemon/impl/transfer/admission.py",
        "daemon/impl/transfer/agent_home_chain.py",
        "daemon/impl/transfer/archive/attribution.py",
        "daemon/impl/transfer/archive/signature.py",
        "daemon/impl/transfer/archive/trust.py",
        "daemon/impl/transfer/cleanup.py",
        "daemon/impl/transfer/execution/sandbox_smoke.py",
        "daemon/impl/transfer/execution/smolvm.py",
        "daemon/impl/transfer/execution/ssh.py",
        "daemon/impl/transfer/landing/owner.py",
        "daemon/impl/transfer/landing/runtime.py",
        "daemon/impl/transfer/landing/signature_gate.py",
        "daemon/impl/transfer/orchestrator.py",
        "daemon/impl/transfer/p2/__init__.py",
        "daemon/impl/transfer/p2/actor.py",
        "daemon/impl/transfer/p2/journal.py",
        "daemon/impl/transfer/p2/model.py",
        "daemon/impl/transfer/p2/orchestration/__init__.py",
        "daemon/impl/transfer/p2/orchestration/chain.py",
        "daemon/impl/transfer/p2/orchestration/lift.py",
        "daemon/impl/transfer/p2/orchestration/orchestrator.py",
        "daemon/impl/transfer/p2/orchestration/receive.py",
        "daemon/impl/transfer/p2/orchestration/target.py",
        "daemon/impl/transfer/p2/orchestration/target_tui.py",
        "daemon/impl/transfer/p2/projection.py",
        "daemon/impl/transfer/p2/source/__init__.py",
        "daemon/impl/transfer/p2/source/effect.py",
        "daemon/impl/transfer/p2/source/runner.py",
        "daemon/impl/transfer/p2/source/session.py",
        "daemon/impl/transfer/p2/source_ports.py",
        "daemon/impl/transfer/p2/store.py",
        "daemon/impl/application/messaging/work_items.py",
        "daemon/impl/application/messaging/work_ledger.py",
        "daemon/impl/application/messaging/work/__init__.py",
        "daemon/impl/application/messaging/work/acceptance.py",
        "daemon/impl/application/messaging/work/conflicts.py",
        "daemon/impl/application/messaging/work/projections.py",
        "daemon/impl/application/messaging/work/validation.py",
        "daemon/impl/request_catalog/work/__init__.py",
        "daemon/impl/request_catalog/work/methods.py",
        "daemon/impl/request_catalog/work/schemas.py",
        "daemon/impl/request_catalog/agent/legacy_boundary.py",
        "daemon/impl/request_catalog/agent/examples.py",
        "daemon/impl/application/actors/routine_defaults.py",
        "daemon/impl/application/actors/routine_defaults/__init__.py",
        "daemon/impl/composition/pac_mirror/__init__.py",
        "daemon/impl/composition/pac_mirror/publisher.py",
        "daemon/impl/composition/pac_mirror/wiring.py",
        "daemon/impl/request_catalog/catalog.py",
        "daemon/impl/request_catalog/readiness.py",
        "daemon/impl/request_catalog/message_lookup.py",
        "daemon/impl/transfer/archive/bundle.py",
        "daemon/impl/application/messaging/inbox_surface/history.py",
        "daemon/impl/application/messaging/inbox_surface/agent_bot.py",
        "daemon/impl/application/messaging/inbox_surface/agent_bot_attempts.py",
        "daemon/impl/operations/service.py",
        "daemon/impl/org/network/access/__init__.py",
        "daemon/impl/org/network/access/actor.py",
        "daemon/impl/org/network/access/snapshot.py",
        "daemon/impl/org/network/admission/__init__.py",
        "daemon/impl/org/network/dialing/__init__.py",
        "daemon/impl/application/control/__init__.py",
        "daemon/impl/ipc/protocol/caller.py",
        "daemon/impl/ipc/protocol/framing.py",
        "daemon/impl/ipc/protocol/peer.py",
        "daemon/impl/onboarding/admission.py",
        "daemon/impl/onboarding/control/__init__.py",
        "daemon/impl/onboarding/control/instance.py",
        "daemon/impl/onboarding/control/peer.py",
        "daemon/impl/onboarding/control/protocol.py",
        "daemon/impl/onboarding/control/service.py",
        "daemon/impl/onboarding/control/sessions.py",
        "daemon/impl/application/messaging/identity_binding.py",
        "daemon/impl/identity/audit/__init__.py",
        "daemon/impl/identity/bindings.py",
        "daemon/impl/identity/ipc.py",
        "daemon/impl/identity/models.py",
        "daemon/impl/identity/mutation/__init__.py",
        "daemon/impl/identity/mutation/errors.py",
        "daemon/impl/identity/mutation/overrides.py",
        "daemon/impl/identity/mutation/suppression.py",
        "daemon/impl/identity/resolver.py",
        "daemon/impl/identity/store.py",
        "daemon/impl/request_catalog/identity/audit/__init__.py",
        "daemon/impl/request_catalog/identity/audit/examples.py",
        "daemon/impl/harnesses/protocol/windows_pty.py",
        "daemon/impl/agent_templates/setup.py",
        "daemon/impl/agent_templates/home_layout.py",
        "daemon/impl/agent_templates/store_lock.py",
        "daemon/impl/request_catalog/setup/squire_operations.py",
        "daemon/impl/request_catalog/setup/squire_specs.py",
        "daemon/impl/request_catalog/setup/squire_examples.py",
        "kernel/impl/processes/owned_process.py",
        "kernel/impl/processes/process_facts.py",
        "daemon/impl/harnesses/dsh/__init__.py",
        "daemon/impl/harnesses/dsh/api.py",
        "daemon/impl/harnesses/dsh/client.py",
        "daemon/impl/harnesses/dsh/process.py",
        "daemon/impl/harnesses/dsh/worker_home.py",
        "daemon/impl/harnesses/dsh_tui/__init__.py",
        "daemon/impl/harnesses/dsh_tui/carrier.py",
        "daemon/impl/harnesses/dsh_tui/mcp_row.py",
        "daemon/impl/harnesses/dsh_tui/plugin.py",
        "daemon/impl/harnesses/python_worker/_exec_env_socket.py",
        "daemon/impl/harnesses/runtime/tmux.py",
        "daemon/impl/request_catalog/link.py",
        "daemon/impl/request_catalog/link_examples/__init__.py",
        "daemon/impl/request_catalog/threads/__init__.py",
        "daemon/impl/request_catalog/threads/examples.py",
        "kernel/impl/facts/launch_spec.py",
        "daemon/impl/request_catalog/adapter/pins.py",
        "daemon/impl/request_catalog/agent/authority/__init__.py",
        "daemon/impl/composition/agent_pins/__init__.py",
        "daemon/impl/composition/agent_pins/facade.py",
        "daemon/impl/org/membership/projection.py",
        "daemon/impl/context_snapshot/__init__.py",
        "daemon/impl/context_snapshot/contract.py",
        "daemon/impl/context_snapshot/io.py",
        "daemon/impl/context_snapshot/native.py",
        "daemon/impl/context_snapshot/recap.py",
        "daemon/impl/context_snapshot/service.py",
        "daemon/impl/composition/links.py",
        "daemon/impl/composition/link_spaces/__init__.py",
        "daemon/impl/composition/link_spaces/spaces.py",
        "daemon/impl/composition/threads/__init__.py",
        "daemon/impl/composition/threads/params.py",
        "daemon/impl/composition/threads/wiring.py",
        "daemon/impl/links/__init__.py",
        "daemon/impl/links/controller.py",
        "daemon/impl/links/model.py",
        "daemon/impl/links/policy.py",
        "daemon/impl/links/ports.py",
        "daemon/impl/links/store.py",
        "daemon/impl/links/service/__init__.py",
        "daemon/impl/links/service/_base.py",
        "daemon/impl/links/service/core.py",
        "daemon/impl/links/service/gate.py",
        "daemon/impl/links/service/holds.py",
        "daemon/impl/links/service/reclaim.py",
        "daemon/impl/links/service/views.py",
        "daemon/impl/threads/__init__.py",
        "daemon/impl/threads/aliases.py",
        "daemon/impl/threads/ids.py",
        "daemon/impl/threads/policy.py",
        "daemon/impl/threads/registry.py",
        "daemon/impl/threads/service.py",
        "daemon/impl/threads/profiles/__init__.py",
        "daemon/impl/inbox/service/link_ledger.py",
        "daemon/impl/inbox/authority/rows.py",
        "daemon/impl/org/membership/personal.py",
        "daemon/impl/authority_service/__init__.py",
        "daemon/impl/authority_service/paths.py",
        "daemon/impl/authority_service/service.py",
        "daemon/impl/authority_set/__init__.py",
        "daemon/impl/authority_set/adapters.py",
        "daemon/impl/authority_set/audit.py",
        "daemon/impl/authority_set/model.py",
        "daemon/impl/authority_set/ports.py",
        "daemon/impl/authority_set/store.py",
        "daemon/impl/authority_set/supervisor.py",
        "daemon/impl/request_catalog/adapter/routes/compat.py",
        "daemon/impl/request_catalog/adapter/routes/compat_examples.py",
        "daemon/impl/application/owner_routing/__init__.py",
        "daemon/impl/composition/conversations/owner_binding.py",
        "daemon/impl/org/membership/receivers.py",
        "daemon/impl/org/membership/service.py",
        "daemon/impl/orgfs/document/register/__init__.py",
        "daemon/impl/request_catalog/org/membership/lifecycle.py",
        "daemon/impl/identity/profiles.py",
        "daemon/impl/orgfs/document/structured.py",
    }
)

_SERVICE_CONNECT_RESIDENT_STUBS = frozenset(
    {
        "daemon/impl/application/netendpoints/services.py",
        "daemon/impl/desired_state/services.py",
        "daemon/impl/forwarding/services.py",
        "daemon/impl/service_connect/__init__.py",
        "daemon/impl/service_connect/io.py",
        "daemon/impl/service_connect/manager.py",
        "daemon/impl/service_connect/models.py",
        "daemon/impl/service_connect/registry.py",
        "daemon/impl/service_connect/resolver.py",
        "daemon/impl/state_persistence/services.py",
    }
)
_SERVICE_CONNECT_RESIDENT_ORIGIN = (
    "service-connect-v0.4: new resident implementation",
)
