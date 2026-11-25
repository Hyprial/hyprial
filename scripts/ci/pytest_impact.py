#!/usr/bin/env python3
"""Layered PR-only pytest selection; pushes, schedules and release PRs always run tests/.

Shard decision 2026-11-15 (KP-069/R5): the nested-xdist FD probe runs as its own
bounded wave inside its shard; only that wave drops to one outer worker while the
rest of the shard keeps the declared ``-n``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
import re
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
NESTED_XDIST_PROBE = "tests/tooling/isolation/test_fd_hygiene_collection.py"
ISOLATED_DIRECTORIES = ("tests/integration/teleport",)
try:
    from scripts.git_environment import (
        assert_no_credentials,
        uncredentialed_subprocess_environment,
    )
except ModuleNotFoundError:  # direct ``python scripts/ci/pytest_impact.py``
    sys.path.insert(0, str(ROOT))
    from scripts.git_environment import (
        assert_no_credentials,
        uncredentialed_subprocess_environment,
    )

# Per-file pytest seconds used only to BALANCE shards (never to select or skip).
# Source and refresh procedure are recorded inside the file itself.
DURATIONS = ROOT / "scripts/ci/pytest-durations.json"
MAX_SHARDS = 16

# .github/workflows/gate-python.yml declares these to drive real test behaviour: the pinned CLI
# binary (contract/cli-blackbox/run.py selects the executable through HYPRIAL_BIN), the wait
# budget, the user-namespace sandbox suppression, the tmp root, the CI profile and the Pi SDK
# requirement. They are control inputs, not credentials, but they are also not part of the
# generic untrusted-code allowlist, so they are forwarded here explicitly instead of widening
# that allowlist for every subprocess in the tree.
PYTEST_CONTROL_ENVIRONMENT = (
    "HYPRIAL_BIN",
    "HYPRIAL_CI_NO_USERNS_CODEX_SANDBOX",
    "HYPRIAL_CI_PROFILE",
    "HYPRIAL_REQUIRE_PI_SDK",
    "HYPRIAL_TEST_TMP_ROOT",
    "HYPRIAL_TEST_WAIT_SECONDS",
)

# Small audited first wave; unknown/new files always remain in the second wave.
# Whole files preserve fixture/order ownership. This is ordering, not omission.
FAST_FILES = (
    "tests/kernel/addressing/test_uri_leaf.py",
    "tests/daemon/pac/workflows/runtime/test_pac_workflow_schema.py",
    "tests/tooling/ci/execution/test_ci_internal_origin.py",
    "tests/tooling/pytest/test_pytest_impact.py",
)

# Diagnostic ownership, not consumer/impact selection. Any unrecognized file
# remains in the catch-all tests/ pass; no new test needs an opt-in to run.
PARTITION_PREFIXES = (
    ("ci", ("tests/test_ci_", "tests/test_pytest_", "tests/test_pac_workflow_schema")),
    ("storage", ("tests/test_state_db", "tests/test_desired_state", "tests/test_inbox",
                 "tests/test_outbox", "tests/test_lark_state_sqlite", "tests/test_durable")),
    ("runtime", ("tests/actor_runtime/", "tests/test_lifecycle", "tests/test_atomic_lifecycle",
                 "tests/test_daemon", "tests/test_routine", "tests/test_agent_session")),
    ("harness", ("tests/test_mcp", "tests/test_harness", "tests/test_codex",
                 "tests/test_claude", "tests/test_pi_", "tests/test_dsh", "tests/test_native")),
    ("transport", ("tests/test_lark_", "tests/test_zenoh", "tests/test_nats",
                   "tests/test_transport", "tests/test_adapter", "tests/test_transfer",
                   "tests/test_hosted_transfer", "tests/test_sidecar")),
    ("cli", ("tests/test_cli_", "tests/test_gui_",
             "tests/test_hyprial_rename", "tests/test_hyprial_merge")),
)

# Exact successors of the former actor_runtime package. A whole new domain
# prefix would accidentally change ownership of unrelated actor tests.
MIGRATED_ACTOR_RUNTIME = frozenset({
    "tests/tooling/architecture/actors/test_cadence_api_boundary.py",
    "tests/daemon/composition/events/test_daemon_composition_events.py",
    "tests/daemon/delivery/custody/test_delivery_custody.py",
    "tests/daemon/composition/boundaries/test_domain_port_contracts.py",
    "tests/kernel/actors/effects/test_effect_lane.py",
    "tests/daemon/composition/boundaries/test_final_composition_boundary.py",
    "tests/daemon/inbox/authority/test_inbox_full_authority.py",
    "tests/kernel/actors/scheduler/test_scheduler.py",
    "tests/daemon/sessions/actors/test_session_agent_actors.py",
    "tests/daemon/sessions/actors/test_session_persistence_overload.py",
})


# Exact provenance of approved semantic test splits. This only preserves
# diagnostic waves; it is not a consumer subset or future-file exemption.
SPLIT_DIAGNOSTIC_IDENTITIES = {
    'tests/daemon/adapters/lark/actors/actor_domain/admission/test_admission.py': 'tests/daemon/adapters/lark/actors/test_lark_actor_domain.py',
    'tests/daemon/adapters/lark/actors/actor_domain/shutdown/test_shutdown.py': 'tests/daemon/adapters/lark/actors/test_lark_actor_domain.py',
    'tests/daemon/adapters/lark/actors/adapter/command_replay/test_command_replay.py': 'tests/daemon/adapters/lark/actors/test_lark_adapter.py',
    'tests/daemon/adapters/lark/actors/adapter/commands/test_commands.py': 'tests/daemon/adapters/lark/actors/test_lark_adapter.py',
    'tests/daemon/adapters/lark/actors/adapter/inbound/test_inbound.py': 'tests/daemon/adapters/lark/actors/test_lark_adapter.py',
    'tests/daemon/adapters/lark/actors/adapter/reply/test_reply.py': 'tests/daemon/adapters/lark/actors/test_lark_adapter.py',
    'tests/daemon/adapters/lark/actors/adapter/timezone/test_timezone.py': 'tests/daemon/adapters/lark/actors/test_lark_adapter.py',
    'tests/daemon/adapters/lark/identity/book/cli/test_cli.py': 'tests/daemon/adapters/lark/identity/test_lark_identities.py',
    'tests/daemon/adapters/lark/identity/book/collection/test_collection.py': 'tests/daemon/adapters/lark/identity/test_lark_identities.py',
    'tests/daemon/adapters/lark/identity/book/observations/test_observations.py': 'tests/daemon/adapters/lark/identity/test_lark_identities.py',
    'tests/daemon/adapters/lark/identity/book/schema/test_schema.py': 'tests/daemon/adapters/lark/identity/test_lark_identities.py',
    'tests/daemon/adapters/lark/identity/scopes/authorization/test_authorization.py': 'tests/daemon/adapters/lark/identity/test_lark_scopes.py',
    'tests/daemon/adapters/lark/identity/scopes/recovery/test_recovery.py': 'tests/daemon/adapters/lark/identity/test_lark_scopes.py',
    'tests/daemon/adapters/lark/identity/scopes/scope_contracts/test_scope_contracts.py': 'tests/daemon/adapters/lark/identity/test_lark_scopes.py',
    'tests/daemon/adapters/lark/identity/scopes/throttle/test_throttle.py': 'tests/daemon/adapters/lark/identity/test_lark_scopes.py',
    'tests/daemon/adapters/lark/inbound/recovery/dead_letters/test_dead_letters.py': 'tests/daemon/adapters/lark/inbound/test_lark_inbound_recovery.py',
    'tests/daemon/adapters/lark/inbound/recovery/health/test_health.py': 'tests/daemon/adapters/lark/inbound/test_lark_inbound_recovery.py',
    'tests/daemon/adapters/lark/inbound/recovery/live_and_recover/test_live_and_recover.py': 'tests/daemon/adapters/lark/inbound/test_lark_inbound_recovery.py',
    'tests/daemon/adapters/lark/inbound/recovery/reconciliation/test_reconciliation.py': 'tests/daemon/adapters/lark/inbound/test_lark_inbound_recovery.py',
    'tests/daemon/adapters/lark/lifecycle/worker/bridge/test_bridge.py': 'tests/daemon/adapters/lark/lifecycle/test_lark_worker_runtime.py',
    'tests/daemon/adapters/lark/lifecycle/worker/configuration_and_status/test_configuration_and_status.py': 'tests/daemon/adapters/lark/lifecycle/test_lark_worker_runtime.py',
    'tests/daemon/adapters/lark/lifecycle/worker/coordination/test_coordination.py': 'tests/daemon/adapters/lark/lifecycle/test_lark_worker_runtime.py',
    'tests/daemon/adapters/lark/lifecycle/worker/readiness/test_readiness.py': 'tests/daemon/adapters/lark/lifecycle/test_lark_worker_runtime.py',
    'tests/daemon/adapters/lark/sdk/gateway/content/test_content.py': 'tests/daemon/adapters/lark/sdk/test_lark_sdk.py',
    'tests/daemon/adapters/lark/sdk/gateway/event_stream/test_event_stream.py': 'tests/daemon/adapters/lark/sdk/test_lark_sdk.py',
    'tests/daemon/adapters/lark/sdk/gateway/history/test_history.py': 'tests/daemon/adapters/lark/sdk/test_lark_sdk.py',
    'tests/daemon/adapters/lark/sdk/gateway/outbound/test_outbound.py': 'tests/daemon/adapters/lark/sdk/test_lark_sdk.py',
    'tests/daemon/adapters/lark/sdk/gateway/permission/test_permission.py': 'tests/daemon/adapters/lark/sdk/test_lark_sdk.py',
    'tests/daemon/application/transport/channel/delivery/test_delivery.py': 'tests/daemon/application/transport/test_daemon_channel.py',
    'tests/daemon/application/transport/channel/routing/test_routing.py': 'tests/daemon/application/transport/test_daemon_channel.py',
    'tests/daemon/application/transport/channel/sessions/test_sessions.py': 'tests/daemon/application/transport/test_daemon_channel.py',
    'tests/daemon/application/transport/channel/supervision/test_supervision.py': 'tests/daemon/application/transport/test_daemon_channel.py',
    'tests/daemon/application/transport/resilience/errors/test_errors.py': 'tests/daemon/application/transport/test_daemon_ipc_resilience.py',
    'tests/daemon/application/transport/resilience/requests/test_requests.py': 'tests/daemon/application/transport/test_daemon_ipc_resilience.py',
    'tests/daemon/application/transport/resilience/shutdown/test_shutdown.py': 'tests/daemon/application/transport/test_daemon_ipc_resilience.py',
    'tests/daemon/application/transport/resilience/startup/test_startup.py': 'tests/daemon/application/transport/test_daemon_ipc_resilience.py',
    'tests/daemon/channels/mcp/lifecycle/supersession/handoff/test_handoff.py': 'tests/daemon/channels/mcp/lifecycle/test_mcp_channel_supersede.py',
    'tests/daemon/delivery/custody/coordinator/admission/test_admission.py': 'tests/daemon/delivery/custody/test_delivery_custody.py',
    'tests/daemon/delivery/custody/coordinator/completion/test_completion.py': 'tests/daemon/delivery/custody/test_delivery_custody.py',
    'tests/daemon/delivery/custody/coordinator/drain/test_drain.py': 'tests/daemon/delivery/custody/test_delivery_custody.py',
    'tests/daemon/harnesses/actors/authority/lifecycle/test_lifecycle.py': 'tests/daemon/harnesses/actors/test_harness_actor.py',
    'tests/daemon/harnesses/actors/authority/settlement/test_settlement.py': 'tests/daemon/harnesses/actors/test_harness_actor.py',
    'tests/daemon/harnesses/actors/authority/surface/test_surface.py': 'tests/daemon/harnesses/actors/test_harness_actor.py',
    'tests/daemon/harnesses/actors/authority/turns/test_turns.py': 'tests/daemon/harnesses/actors/test_harness_actor.py',
    'tests/daemon/harnesses/agent_sdk/driver/client_contracts/test_client_contracts.py': 'tests/daemon/harnesses/agent_sdk/test_agent_sdk_driver.py',
    'tests/daemon/harnesses/agent_sdk/driver/delivery/test_delivery.py': 'tests/daemon/harnesses/agent_sdk/test_agent_sdk_driver.py',
    'tests/daemon/harnesses/agent_sdk/driver/identity/test_identity.py': 'tests/daemon/harnesses/agent_sdk/test_agent_sdk_driver.py',
    'tests/daemon/harnesses/codex/app_server/delivery/test_delivery.py': 'tests/daemon/harnesses/codex/test_codex_app_server.py',
    'tests/daemon/harnesses/codex/app_server/ownership/test_ownership.py': 'tests/daemon/harnesses/codex/test_codex_app_server.py',
    'tests/daemon/harnesses/codex/app_server/session/test_session.py': 'tests/daemon/harnesses/codex/test_codex_app_server.py',
    'tests/daemon/harnesses/codex/app_server/startup/test_startup.py': 'tests/daemon/harnesses/codex/test_codex_app_server.py',
    'tests/daemon/harnesses/codex/native_home/home_contracts/test_home_contracts.py': 'tests/daemon/harnesses/codex/test_codex_agent_home.py',
    'tests/daemon/harnesses/pi/rpc/process/delivery/test_delivery.py': 'tests/daemon/harnesses/pi/rpc/test_pi_rpc_process.py',
    'tests/daemon/harnesses/pi/rpc/process/failure/test_failure.py': 'tests/daemon/harnesses/pi/rpc/test_pi_rpc_process.py',
    'tests/daemon/harnesses/pi/rpc/process/ownership/test_ownership.py': 'tests/daemon/harnesses/pi/rpc/test_pi_rpc_process.py',
    'tests/daemon/harnesses/pi/rpc/process/protocol/test_protocol.py': 'tests/daemon/harnesses/pi/rpc/test_pi_rpc_process.py',
    'tests/daemon/home/guard/home_contracts/test_home_contracts.py': 'tests/daemon/home/test_home_guard.py',
    'tests/daemon/inbox/authority/full_writer/admission/test_admission.py': 'tests/daemon/inbox/authority/test_inbox_full_authority.py',
    'tests/daemon/inbox/authority/full_writer/custody/test_custody.py': 'tests/daemon/inbox/authority/test_inbox_full_authority.py',
    'tests/daemon/inbox/authority/full_writer/projection/test_projection.py': 'tests/daemon/inbox/authority/test_inbox_full_authority.py',
    'tests/daemon/lifecycle/lifecycle/ports/close/test_close.py': 'tests/daemon/lifecycle/lifecycle/test_atomic_lifecycle_ports.py',
    'tests/daemon/lifecycle/lifecycle/ports/mutation/test_mutation.py': 'tests/daemon/lifecycle/lifecycle/test_atomic_lifecycle_ports.py',
    'tests/daemon/lifecycle/lifecycle/ports/receipt/test_receipt.py': 'tests/daemon/lifecycle/lifecycle/test_atomic_lifecycle_ports.py',
    'tests/daemon/lifecycle/lifecycle/ports/surface/test_surface.py': 'tests/daemon/lifecycle/lifecycle/test_atomic_lifecycle_ports.py',
    'tests/daemon/lifecycle/process/manager/admission/test_admission.py': 'tests/daemon/lifecycle/process/test_lifecycle_process_manager.py',
    'tests/daemon/lifecycle/process/manager/compensation/test_compensation.py': 'tests/daemon/lifecycle/process/test_lifecycle_process_manager.py',
    'tests/daemon/lifecycle/process/manager/coordinator/test_coordinator.py': 'tests/daemon/lifecycle/process/test_lifecycle_process_manager.py',
    'tests/daemon/lifecycle/process/manager/recovery/test_recovery.py': 'tests/daemon/lifecycle/process/test_lifecycle_process_manager.py',
    'tests/daemon/orgfs/authority/admission/drain/test_drain.py': 'tests/daemon/orgfs/authority/test_orgfs_admission.py',
    'tests/daemon/orgfs/authority/admission/membership/test_membership.py': 'tests/daemon/orgfs/authority/test_orgfs_admission.py',
    'tests/daemon/orgfs/authority/admission/validation/test_validation.py': 'tests/daemon/orgfs/authority/test_orgfs_admission.py',
    'tests/daemon/orgfs/mesh/replication/blobs/test_blobs.py': 'tests/daemon/orgfs/mesh/test_orgfs_mesh_rework.py',
    'tests/daemon/orgfs/mesh/replication/content/test_content.py': 'tests/daemon/orgfs/mesh/test_orgfs_mesh_rework.py',
    'tests/daemon/orgfs/mesh/replication/presence/test_presence.py': 'tests/daemon/orgfs/mesh/test_orgfs_mesh_rework.py',
    'tests/daemon/orgfs/server/http/request_contracts/test_request_contracts.py': 'tests/daemon/orgfs/server/test_orgfs_webserver.py',
    'tests/daemon/pac/workflows/runtime/graph_service/completion/test_completion.py': 'tests/daemon/pac/workflows/runtime/test_pac_workflow_runtime.py',
    'tests/daemon/pac/workflows/runtime/graph_service/deadline/test_deadline.py': 'tests/daemon/pac/workflows/runtime/test_pac_workflow_runtime.py',
    'tests/daemon/pac/workflows/runtime/graph_service/graph_contracts/test_graph_contracts.py': 'tests/daemon/pac/workflows/runtime/test_pac_workflow_runtime.py',
    'tests/daemon/pac/workflows/runtime/graph_service/workers/test_workers.py': 'tests/daemon/pac/workflows/runtime/test_pac_workflow_runtime.py',
    'tests/daemon/sessions/actors/domains/binding/test_binding.py': 'tests/daemon/sessions/actors/test_session_agent_actors.py',
    'tests/daemon/sessions/actors/domains/identity/test_identity.py': 'tests/daemon/sessions/actors/test_session_agent_actors.py',
    'tests/daemon/sessions/actors/domains/recovery/test_recovery.py': 'tests/daemon/sessions/actors/test_session_agent_actors.py',
    'tests/daemon/sessions/actors/domains/session/test_session.py': 'tests/daemon/sessions/actors/test_session_agent_actors.py',
    'tests/daemon/squire/addressing/routing/adapter_contracts/test_adapter_contracts.py': 'tests/daemon/squire/addressing/test_user_addressing.py',
    'tests/daemon/squire/addressing/routing/delivery/test_delivery.py': 'tests/daemon/squire/addressing/test_user_addressing.py',
    'tests/daemon/squire/addressing/routing/profile_resolution/test_profile_resolution.py': 'tests/daemon/squire/addressing/test_user_addressing.py',
    'tests/daemon/squire/addressing/routing/receiver_guidance/test_receiver_guidance.py': 'tests/daemon/squire/addressing/test_user_addressing.py',
    'tests/daemon/turns/hooks/annotations/test_annotations.py': 'tests/daemon/turns/test_turn_hooks.py',
    'tests/daemon/turns/hooks/configuration/test_configuration.py': 'tests/daemon/turns/test_turn_hooks.py',
    'tests/daemon/turns/hooks/recap_writer/test_recap_writer.py': 'tests/daemon/turns/test_turn_hooks.py',
    'tests/daemon/turns/hooks/runtime/test_runtime.py': 'tests/daemon/turns/test_turn_hooks.py',
    'tests/daemon/turns/hooks/transport/test_transport.py': 'tests/daemon/turns/test_turn_hooks.py',
    'tests/identity/agents/home/migration/p2/test_ownership.py': 'tests/identity/agents/home/migration/test_agent_home_p2_migration.py',
    'tests/identity/agents/home/migration/p2/test_preflight.py': 'tests/identity/agents/home/migration/test_agent_home_p2_migration.py',
    'tests/identity/agents/home/migration/p2/test_projection.py': 'tests/identity/agents/home/migration/test_agent_home_p2_migration.py',
    'tests/identity/agents/home/migration/p2/test_publication.py': 'tests/identity/agents/home/migration/test_agent_home_p2_migration.py',
    'tests/identity/agents/home/roots/lifecycle/test_destruction.py': 'tests/identity/agents/home/roots/test_agent_home_dirs.py',
    'tests/identity/agents/home/roots/lifecycle/test_launch_fencing.py': 'tests/identity/agents/home/roots/test_agent_home_dirs.py',
    'tests/identity/agents/home/roots/lifecycle/test_layout.py': 'tests/identity/agents/home/roots/test_agent_home_dirs.py',
    'tests/identity/agents/home/roots/lifecycle/test_provider_auth.py': 'tests/identity/agents/home/roots/test_agent_home_dirs.py',
    'tests/identity/agents/home/roots/lifecycle/test_startup_recovery.py': 'tests/identity/agents/home/roots/test_agent_home_dirs.py',
    'tests/identity/agents/registry/entity/test_capabilities.py': 'tests/identity/agents/registry/test_agent_entity.py',
    'tests/identity/agents/registry/entity/test_lifecycle.py': 'tests/identity/agents/registry/test_agent_entity.py',
    'tests/identity/agents/registry/entity/test_recovery.py': 'tests/identity/agents/registry/test_agent_entity.py',
    'tests/identity/agents/registry/entity/test_registry.py': 'tests/identity/agents/registry/test_agent_entity.py',
    'tests/identity/agents/registry/entity/test_routines.py': 'tests/identity/agents/registry/test_agent_entity.py',
    'tests/identity/providers/authentication/episodes/test_custody.py': 'tests/identity/providers/authentication/test_provider_auth_actor.py',
    'tests/identity/providers/authentication/episodes/test_native_recovery.py': 'tests/identity/providers/authentication/test_provider_auth_actor.py',
    'tests/identity/providers/authentication/episodes/test_notification.py': 'tests/identity/providers/authentication/test_provider_auth_actor.py',
    'tests/identity/providers/authentication/episodes/test_retry.py': 'tests/identity/providers/authentication/test_provider_auth_actor.py',
    'tests/identity/providers/lifecycle/availability/test_attempts.py': 'tests/identity/providers/lifecycle/test_provider_availability_contract.py',
    'tests/identity/providers/lifecycle/availability/test_owner_notices.py': 'tests/identity/providers/lifecycle/test_provider_availability_contract.py',
    'tests/identity/providers/lifecycle/availability/test_receiver_drill.py': 'tests/identity/providers/lifecycle/test_provider_availability_contract.py',
    'tests/identity/providers/lifecycle/availability/test_selection.py': 'tests/identity/providers/lifecycle/test_provider_availability_contract.py',
    'tests/integration/actor_preservation/delivery/channel/test_operational_lease.py': 'tests/integration/actor_preservation/delivery/test_actor_preserved_daemon_channel.py',
    'tests/integration/actor_preservation/delivery/channel/test_registration.py': 'tests/integration/actor_preservation/delivery/test_actor_preserved_daemon_channel.py',
    'tests/integration/actor_preservation/delivery/channel/test_reply_settlement.py': 'tests/integration/actor_preservation/delivery/test_actor_preserved_daemon_channel.py',
    'tests/integration/actor_preservation/delivery/channel/test_route_custody.py': 'tests/integration/actor_preservation/delivery/test_actor_preserved_daemon_channel.py',
    'tests/integration/actor_preservation/delivery/channel/test_route_replacement.py': 'tests/integration/actor_preservation/delivery/test_actor_preserved_daemon_channel.py',
    'tests/integration/actor_preservation/orgfs/mesh/test_announcement.py': 'tests/integration/actor_preservation/orgfs/test_actor_preserved_orgfs_mesh_rework.py',
    'tests/integration/actor_preservation/orgfs/mesh/test_blob_custody.py': 'tests/integration/actor_preservation/orgfs/test_actor_preserved_orgfs_mesh_rework.py',
    'tests/integration/actor_preservation/orgfs/mesh/test_document_merge.py': 'tests/integration/actor_preservation/orgfs/test_actor_preserved_orgfs_mesh_rework.py',
    'tests/kernel/actors/identity/test_composition.py': 'tests/kernel/actors/test_actor_identity.py',
    'tests/kernel/actors/identity/test_mailboxes.py': 'tests/kernel/actors/test_actor_identity.py',
    'tests/kernel/actors/identity/test_resolution.py': 'tests/kernel/actors/test_actor_identity.py',
    'tests/kernel/actors/identity/test_wire_delivery.py': 'tests/kernel/actors/test_actor_identity.py',
    'tests/shell/cli/agents/claude/test_hooks.py': 'tests/shell/cli/agents/test_cli_claude_interactive.py',
    'tests/shell/cli/agents/claude/test_launch.py': 'tests/shell/cli/agents/test_cli_claude_interactive.py',
    'tests/shell/cli/agents/claude/test_profile.py': 'tests/shell/cli/agents/test_cli_claude_interactive.py',
    'tests/shell/cli/agents/claude/test_recovery.py': 'tests/shell/cli/agents/test_cli_claude_interactive.py',
    'tests/shell/cli/agents/codex/test_delivery.py': 'tests/shell/cli/agents/test_cli_codex_interactive.py',
    'tests/shell/cli/agents/codex/test_discovery_launch.py': 'tests/shell/cli/agents/test_cli_codex_interactive.py',
    'tests/shell/cli/agents/codex/test_ipc.py': 'tests/shell/cli/agents/test_cli_codex_interactive.py',
    'tests/shell/cli/agents/codex/test_settlement.py': 'tests/shell/cli/agents/test_cli_codex_interactive.py',
    'tests/shell/cli/installation/restart/test_failure_markers.py': 'tests/shell/cli/installation/test_cli_upgrade_restart.py',
    'tests/shell/cli/installation/restart/test_launch.py': 'tests/shell/cli/installation/test_cli_upgrade_restart.py',
    'tests/shell/cli/installation/restart/test_notification.py': 'tests/shell/cli/installation/test_cli_upgrade_restart.py',
    'tests/shell/cli/installation/restart/test_reconciliation.py': 'tests/shell/cli/installation/test_cli_upgrade_restart.py',
    'tests/shell/login/device/test_boundaries.py': 'tests/shell/login/test_login.py',
    'tests/shell/login/device/test_persistence.py': 'tests/shell/login/test_login.py',
    'tests/shell/login/device/test_polling.py': 'tests/shell/login/test_login.py',
    'tests/shell/login/device/test_profile.py': 'tests/shell/login/test_login.py',
    'tests/shell/login/orchestration/test_custody.py': 'tests/shell/login/test_login_orchestrate_daemon.py',
    'tests/shell/login/orchestration/test_failure_projection.py': 'tests/shell/login/test_login_orchestrate_daemon.py',
    'tests/shell/login/orchestration/test_identity_switch.py': 'tests/shell/login/test_login_orchestrate_daemon.py',
    'tests/shell/login/orchestration/test_recovery.py': 'tests/shell/login/test_login_orchestrate_daemon.py',
    'tests/shell/updates/general/resolution/test_constraints.py': 'tests/shell/updates/general/test_updates.py',
    'tests/shell/updates/general/resolution/test_remote_tags.py': 'tests/shell/updates/general/test_updates.py',
    'tests/shell/updates/general/resolution/test_sources_tracks.py': 'tests/shell/updates/general/test_updates.py',
    'tests/shell/updates/general/resolution/test_track_installation.py': 'tests/shell/updates/general/test_updates.py',
    'tests/shell/updates/general/resolution/test_upgrade.py': 'tests/shell/updates/general/test_updates.py',
    'tests/tooling/release/publish/github/test_publication.py': 'tests/tooling/release/publish/test_github_publish.py',
    'tests/tooling/release/publish/github/test_retry_workflow.py': 'tests/tooling/release/publish/test_github_publish.py',
    'tests/tooling/release/publish/github/test_security.py': 'tests/tooling/release/publish/test_github_publish.py',
    'tests/tooling/release/publish/github/test_snapshot.py': 'tests/tooling/release/publish/test_github_publish.py',
}


def partition_name(path: str) -> str:
    # Filename identities survive the semantic-directory migration. They only
    # name diagnostic waves: unknown names still belong to the final tests/
    # catch-all and none of these aliases is an impact/consumer exemption.
    original = SPLIT_DIAGNOSTIC_IDENTITIES.get(path, path)
    legacy_identity = f"tests/{PurePosixPath(original).name}" if original.startswith("tests/") else original
    if original in MIGRATED_ACTOR_RUNTIME:
        return "runtime"
    if path == "tests/shell/gui/ci/test_gui_ci_migration.py" or legacy_identity == "tests/test_gui_ci_migration.py":
        return "ci"  # CI policy, despite its historical GUI-prefixed filename.
    # This exact file is a storage contract, not transport behavior.
    if path == "tests/daemon/adapters/lark/storage/test_lark_state_sqlite.py" or legacy_identity == "tests/test_lark_state_sqlite.py":
        return "storage"
    identities = (original, legacy_identity)
    owners = [name for name, prefixes in PARTITION_PREFIXES
              if any(identity.startswith(prefixes) for identity in identities)]
    if len(owners) > 1:
        raise ValueError(f"ambiguous pytest partition: {path}")
    return owners[0] if owners else "remaining"


def partitions(root: Path, selected: list[str]) -> list[tuple[str, list[str]]]:
    fast = set(expand(root, list(FAST_FILES)))
    whole = selected == ["tests/"]
    # Explicit Python test files go into named waves. The final tests/ traversal
    # still owns custom collectors, future naming conventions and unknown files.
    files = sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py") if path.is_file()) if whole else selected
    groups = {name: [] for name in ["fast", *(name for name, _ in PARTITION_PREFIXES), "remaining"]}
    for path in files:
        name = "fast" if path in fast else partition_name(path)
        groups[name].append(path)
    if whole:
        assigned = [path for name, paths in groups.items() if name != "remaining" for path in paths]
        groups["remaining"] = ["tests/", *(f"--ignore={path}" for path in assigned)]
    return [(name, paths) for name, paths in groups.items() if paths]


def full(reason: str) -> dict:
    return {"mode": "full", "tests": ["tests/"], "groups": [], "reasons": [reason]}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout


def safe_path(value: str, prefix: str) -> bool:
    return (isinstance(value, str) and value.startswith(prefix)
            and not any(part in {"", ".", ".."} for part in value.split("/"))
            and not PurePosixPath(value).is_absolute()
            and re.fullmatch(r"[A-Za-z0-9_./*\-]+", value) is not None)


def expand(root: Path, patterns: list[str]) -> list[str]:
    if not isinstance(patterns, list) or not patterns:
        raise ValueError("empty test mapping")
    selected = set()
    for pattern in patterns:
        if not safe_path(pattern, "tests/") or not pattern.endswith(".py"):
            raise ValueError("unsafe test pattern")
        matches = sorted(root.glob(pattern))
        if not matches:
            raise ValueError("test mapping has no match")
        for path in matches:
            if not path.is_file() or path.is_symlink():
                raise ValueError("test mapping is not a regular file")
            relative = path.relative_to(root)
            if any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
                raise ValueError("symlink in test path")
            if not path.resolve().is_relative_to((root / "tests").resolve()):
                raise ValueError("test outside repository")
            selected.add(relative.as_posix())
    return sorted(selected)


def documentation(path: str) -> bool:
    if path in {"docs/domains/squire/SKILL-template.md", "docs/guides/cutover-runbook.md"}:
        return False
    return path in {"README.md", "CONTRIBUTING.md", "CHANGELOG.md"} or (
        path.startswith(("docs/", "notes/")) and path.endswith((".md", ".rst", ".adoc")))


#: The product layers, lowest first. Imports only point downward
#: (kernel <- identity <- daemon <- biz <- shell; measured 2026-11-06 over
#: src/hyprial), so a change in one layer can only break that layer and the
#: layers above it. tests/<layer>/ mirrors src/hyprial/<layer>/.
LAYERS = ("kernel", "identity", "daemon", "biz", "shell")

#: Paths whose consumers are not one layer: any of them runs the full suite.
#: Everything not matched by a rule below is full as well (fail closed).
_FULL_PREFIXES = (
    "tests/support/", "tests/fixtures/", "src/hyprial/skills/",
)
_FULL_FILES = {
    "pyproject.toml", "uv.lock", "tests/conftest.py", "tests/__init__.py",
    "src/hyprial/__init__.py", "src/hyprial/cli.py", "src/hyprial/gui_build_hook.py",
    "scripts/ci/pytest_impact.py",
}

#: Structural guards whose subject is the whole src/hyprial tree rather than
#: one layer: bounded shutdown waits, unique identity-address parsing, public
#: identity/daemon seams, and the actor-rustify inventory. Each file lives
#: under a single test layer, so a product change in another layer never
#: selects it without this union. Every selected plan that changes product
#: code runs all four; a missing guard fails closed to the full suite.
CROSS_CUTTING_GUARDS = (
    "tests/tooling/isolation/test_teardown_guarantee.py",
    "tests/kernel/addressing/test_address_parsing_guard.py",
    "tests/identity/provider_keys/structure/test_public_seams.py",
    "tests/scripts/test_actor_rustify_inventory.py",
)


def layer_targets(path: str) -> tuple[str, ...] | None:
    """Test directories a changed path can break; None means the full suite."""

    if path in _FULL_FILES or path.startswith(_FULL_PREFIXES):
        return None
    parts = path.split("/")
    if len(parts) >= 4 and parts[:2] == ["src", "hyprial"] and parts[2] in LAYERS:
        above = LAYERS[LAYERS.index(parts[2]):]
        return (*(f"tests/{layer}/" for layer in above), "tests/integration/")
    if len(parts) >= 3 and parts[0] == "tests":
        if parts[1] in LAYERS or parts[1] in ("integration", "tooling", "scripts"):
            return (f"tests/{parts[1]}/",)
        return None
    if parts[0] in (".github",) or path.startswith("scripts/ci/"):
        return ("tests/tooling/",)
    if parts[0] == "scripts":
        return ("tests/tooling/", "tests/scripts/")
    return None


def standalone_test_modules(root: Path, candidates: list[str]) -> set[str]:
    """The candidate test modules that no other file under tests/ refers to.

    A changed test module can only break itself unless another test imports it
    or loads it by name, so it may run alone. The check is textual and errs
    toward the directory: any mention of the module's stem as a word elsewhere
    under tests/ keeps the wider selection. Each file is read and split into
    words once, so the cost does not grow with the number of candidates.
    """
    stems = {path: PurePosixPath(path).stem for path in candidates}
    alone = set(stems)
    if not alone:
        return alone
    for source in sorted((root / "tests").rglob("*.py")):
        name = source.relative_to(root).as_posix()
        try:
            words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            return set()
        alone -= {path for path in alone if path != name and stems[path] in words}
        if not alone:
            break
    return alone


def plan(root: Path, event: dict, event_name: str) -> dict:
    # This boundary deliberately precedes all diff handling. A push, manual
    # run or schedule can never inherit a PR subset into publication: every
    # dev/main push and every nightly runs the whole suite.
    if event_name != "pull_request":
        return full("non-PR event: full release/manual/scheduled validation")
    target = event.get("pull_request", {}).get("base", {}).get("ref")
    if target == "main" or (isinstance(target, str) and target.startswith("release/")):
        return full("release gate: PRs into main or release/** always run the full suite")
    try:
        base = event["pull_request"]["base"]["sha"]
        head = event["pull_request"]["head"]["sha"]
        if not all(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha) for sha in (base, head)):
            raise ValueError("invalid comparison commits")
        if git(root, "rev-parse", "HEAD").strip() != head:
            return full("checkout does not match the PR head")
        ancestor = git(root, "merge-base", base, head).strip()
        raw = git(root, "diff", "--no-renames", "--name-status", "-z", ancestor, head, "--")
        fields = raw.split("\0")
        if fields[-1] != "" or len(fields[:-1]) % 2:
            raise ValueError("invalid diff records")
        changes = list(zip(fields[0:-1:2], fields[1:-1:2], strict=True))
        paths = [path for _, path in changes if not documentation(path)]
        if not paths:
            return full("no code change; coarse documentation gate owns skipping")
        available = test_files(root)
        present = set(available)
        alone = standalone_test_modules(root, [path for path in paths if path in present])
        targets: set[str] = set()
        for path in paths:
            selected = layer_targets(path)
            if selected is None:
                return full(f"shared or unlayered change: {path}")
            targets.update((path,) if path in alone else selected)
        tests = [path for path in available if path.startswith(tuple(sorted(targets)))]
        if not tests:
            return full("layer selection matched no test file")
        if any(path.startswith("src/hyprial/") for path in paths):
            available_set = set(available)
            missing = [guard for guard in CROSS_CUTTING_GUARDS if guard not in available_set]
            if missing:
                return full(f"cross-cutting guard missing: {missing[0]}")
            tests = sorted({*tests, *CROSS_CUTTING_GUARDS})
        groups = sorted(target.rstrip("/").split("/", 1)[1] for target in targets)
        reasons = [f"layered: {len(paths)} changed path(s) -> {', '.join(sorted(targets))}"]
        return {"mode": "selected", "tests": tests, "groups": groups,
                "reasons": reasons, "base": base, "mergeBase": ancestor, "head": head}
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        # Unknown history never creates a green empty suite.
        # Git stderr/event payloads can contain credentials: do not echo them.
        return full("comparison unavailable: fail closed to full suite")


def stages(root: Path, selected: list[str]) -> list[tuple[str, list[str]]]:
    fast = expand(root, list(FAST_FILES))
    if selected == ["tests/"]:
        return [("fast", fast), ("remaining", ["tests/", *(f"--ignore={path}" for path in fast)])]
    first = [path for path in selected if path in fast]
    rest = [path for path in selected if path not in fast]
    return [(name, paths) for name, paths in [("fast", first), ("remaining", rest)] if paths]


def bounded_shard_workers(paths: list[str], forwarded: list[str]) -> list[str]:
    """Leave room for the FD probe's own two-worker pytest inside a shard."""
    if NESTED_XDIST_PROBE not in paths:
        return forwarded
    args = forwarded.copy()
    for index, value in enumerate(args):
        if value == "-n" and index + 1 < len(args):
            args[index + 1] = "1"
        elif value.startswith("-n="):
            args[index] = "-n=1"
    return args


def probe_wave(paths: list[str]) -> tuple[list[str], list[str]]:
    """Split a shard into its nested-xdist probe wave and the remaining files.

    The probe starts its own two-worker pytest, so only its wave runs on the
    bounded single outer worker while the rest of the shard keeps the caller's
    declared ``-n``. The waves cover the shard's file list exactly once and run
    in this order; the rest still runs when the probe wave fails, so a red
    probe never deletes the shard's other results.
    """
    probe = [path for path in paths if path == NESTED_XDIST_PROBE]
    return probe, [path for path in paths if path != NESTED_XDIST_PROBE]


def test_files(root: Path) -> list[str]:
    """Regular files matching pytest's default module patterns under tests/."""
    found = set()
    for pattern in ("test_*.py", "*_test.py"):
        for path in (root / "tests").rglob(pattern):
            if path.is_file() and not path.is_symlink():
                found.add(path.relative_to(root).as_posix())
    return sorted(found)


def is_isolated(path: str) -> bool:
    """Whether a test path belongs to the isolated lane instead of a shard."""
    return any(path == base or path.startswith(base + "/") for base in ISOLATED_DIRECTORIES)


def isolated_paths(root: Path, selected: list[str]) -> list[str]:
    """What the isolated lane runs for this selection; empty means nothing to run.

    The isolated directories hold modules that start two real daemons each.
    Beside a shard's two long-lived pytest workers they push the job past the
    runner's memory limit, so they run in a job of their own. A full run takes
    each existing directory whole; a PR selection takes the selected files
    that live in one.
    """
    if selected == ["tests/"]:
        return [base for base in ISOLATED_DIRECTORIES if (root / base).is_dir()]
    return sorted({path for path in selected if is_isolated(path)})


def load_weights(path: Path) -> dict[str, float]:
    """Per-file seconds from the durations file; anything unreadable weighs nothing."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {name: float(value) for name, value in files.items()
            if isinstance(name, str) and isinstance(value, (int, float)) and value >= 0}


def shard_plan(root: Path, selected: list[str], count: int, weights: dict[str, float]) -> list[list[str]]:
    """Deterministic, mutually exclusive, complete whole-file shards.

    Files are packed largest-first onto the lightest shard (ties by index and
    path), so every runner computes the same plan from the same tree. For the
    full suite the LAST shard is the catch-all ``tests/`` minus every file the
    other shards run explicitly: a file this planner does not recognise (new
    naming pattern, custom collector, unknown weight) still runs exactly once.
    The nested xdist FD probe runs first in its explicit shard while workers
    have not accumulated the rest of the shard's test state.
    A PR selection is an explicit file list, so it is packed exhaustively and a
    shard may legitimately be empty.
    """
    if not 1 <= count <= MAX_SHARDS:
        raise ValueError("invalid shard count")
    whole = selected == ["tests/"]
    files = [path for path in (test_files(root) if whole else sorted(set(selected))) if not is_isolated(path)]
    known = [weight for weight in weights.values() if weight > 0]
    default = statistics.median(known) if known else 1.0
    loads = [0.0] * count
    bins: list[list[str]] = [[] for _ in range(count)]
    for path in sorted(files, key=lambda name: (-weights.get(name, default), name)):
        index = min(range(count), key=lambda k: (loads[k], k))
        bins[index].append(path)
        loads[index] += weights.get(path, default)
    bins = [sorted(paths, key=lambda path: (path != NESTED_XDIST_PROBE, path)) for paths in bins]
    if whole:
        explicit = sorted(path for paths in bins[:-1] for path in paths)
        bins[-1] = ["tests/", *(f"--ignore={path}" for path in explicit),
                    *(f"--ignore={base}" for base in isolated_paths(root, selected))]
    return bins


def junit_file(root: Path, case) -> str | None:
    """The test module a junit <testcase> came from, as a repository path.

    xunit2 (pytest's default) has no ``file`` attribute and its ``classname``
    is ``tests.pkg.test_mod`` or, for test classes, ``tests.pkg.test_mod.Class``:
    take the longest dotted prefix that names an existing module.
    """
    if (name := case.get("file")) and safe_path(name, "tests/"):
        return name
    parts = case.get("classname", "").split(".")
    for end in range(len(parts), 0, -1):
        name = "/".join(parts[:end]) + ".py"
        if safe_path(name, "tests/") and (root / name).is_file():
            return name
    return None


def durations_from_junit(root: Path, paths: list[Path]) -> dict[str, float]:
    """Per-file seconds summed over every <testcase> in the given junit files."""
    import xml.etree.ElementTree as ET

    totals: dict[str, float] = {}
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            if name := junit_file(root, case):
                totals[name] = totals.get(name, 0.0) + float(case.get("time") or 0)
    return {name: round(seconds, 1) for name, seconds in sorted(totals.items())}


def write_durations(path: Path, junit: list[Path], source: str) -> int:
    files = durations_from_junit(ROOT, junit)
    if not files:
        raise SystemExit("no <testcase> with a tests/ file in the given junit XML")
    # One entry per line keeps reviews and the 1000-line gate readable.
    body = ",\n".join(f"    {json.dumps(name)}: {seconds}" for name, seconds in files.items())
    path.write_text(
        "{\n"
        '  "version": 1,\n'
        f'  "source": {json.dumps(source)},\n'
        '  "refresh": "uv run python scripts/ci/pytest_impact.py --write-durations SOURCE JUNIT... '
        '(junit from the pytest-groups-pytest-shard-* artifacts or a full local run)",\n'
        '  "use": "shard balancing only; never selects or skips a test",\n'
        f'  "files": {{\n{body}\n  }}\n'
        "}\n"
    )
    print(f"wrote {len(files)} files, {sum(files.values()):.0f}s total, to {path}")
    return 0


def plan_digest(bins: list[list[str]]) -> str:
    return hashlib.sha256(json.dumps(bins, separators=(",", ":")).encode()).hexdigest()[:16]


def parse_shard(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", value)
    if not match or not 1 <= int(match[1]) <= int(match[2]) <= MAX_SHARDS:
        raise argparse.ArgumentTypeError("--shard expects K/N with 1 <= K <= N <= 16")
    return int(match[1]), int(match[2])


def pytest_subprocess_environment(source: Mapping[str, str]) -> dict[str, str]:
    """The untrusted-code allowlist plus the explicit HYPRIAL_* test-control passthrough."""
    environment = uncredentialed_subprocess_environment(source)
    for name in PYTEST_CONTROL_ENVIRONMENT:
        if (value := source.get(name)) is not None:
            environment[name] = value
    assert_no_credentials(environment)
    return environment


def execute(root: Path, selected: list[str], forwarded: list[str], staged: bool, *, grouped: bool = False,
            waves: list[tuple[str, list[str]]] | None = None) -> int:
    if waves is None:
        waves = partitions(root, selected) if grouped else stages(root, selected) if staged else [("all", selected)]
    else:
        staged = True  # one named wave: suffix basetemp/junit like the other staged modes
    staged = staged or grouped
    child_env = pytest_subprocess_environment(os.environ)
    for name, paths in waves:
        # Separate pytest temporary roots; the next session must not remove
        # the previous stage's failure evidence or share stale fixtures.
        args = []
        for index, value in enumerate(forwarded):
            if staged and value.startswith("--basetemp="):
                value += f"-{name}"
            elif staged and index and forwarded[index - 1] == "--basetemp":
                value += f"-{name}"
            if staged and value.startswith("--junitxml="):
                value += f".{name}.xml"
            elif staged and index and forwarded[index - 1] == "--junitxml":
                value += f".{name}.xml"
            args.append(value)
        print("PYTEST_STAGE " + json.dumps({"name": name, "selection": paths}), flush=True)
        started = time.monotonic()
        assert_no_credentials(child_env)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *paths, *args],
            cwd=root,
            env=child_env,
        )
        outcome = {"name": name, "exitCode": result.returncode, "seconds": round(time.monotonic() - started, 3)}
        print("PYTEST_STAGE_RESULT " + json.dumps(outcome), flush=True)
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a") as stream:
                stream.write(f"\nPython {name}: exit={result.returncode}, {outcome['seconds']}s\n")
        if result.returncode:
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--staged", action="store_true", help="Run audited fast files before the remaining selection")
    parser.add_argument("--grouped", action="store_true", help="Run diagnostic whole-file groups in one fail-fast gate")
    parser.add_argument("--shard", type=parse_shard, metavar="K/N",
                        help="Run only whole-file shard K of N (deterministic, complete, exclusive)")
    parser.add_argument("--isolated", action="store_true",
                        help="Run only the isolated directories of the selection, in one wave")
    parser.add_argument("--write-durations", nargs="+", metavar=("SOURCE", "JUNIT"),
                        help="Rewrite scripts/ci/pytest-durations.json from junit XML files")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.write_durations:
        source, *junit = args.write_durations
        if not junit:
            parser.error("--write-durations needs SOURCE and at least one JUNIT file")
        return write_durations(DURATIONS, [Path(name) for name in junit], source)
    if args.shard and (args.grouped or args.staged):
        parser.error("--shard runs one wave; it cannot be combined with --grouped/--staged")
    if args.isolated and (args.shard or args.grouped or args.staged):
        parser.error("--isolated runs one wave; it cannot be combined with --shard/--grouped/--staged")
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    except (KeyError, OSError, ValueError):
        event = {}
    decision = plan(ROOT, event, os.environ.get("GITHUB_EVENT_NAME", ""))
    print("PYTEST_IMPACT " + json.dumps(decision, sort_keys=True), flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as stream:
            stream.write("\n### Python test selection\n\n```json\n" + json.dumps(decision, indent=2) + "\n```\n")
    if args.plan_only:
        return 0
    forwarded = args.pytest_args
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if args.isolated:
        paths = isolated_paths(ROOT, decision["tests"])
        if not paths:
            print("PYTEST_ISOLATED_EMPTY " + json.dumps({"mode": decision["mode"]}), flush=True)
            return 0
        print("PYTEST_ISOLATED " + json.dumps({"mode": decision["mode"], "paths": paths}, sort_keys=True), flush=True)
        return execute(ROOT, decision["tests"], forwarded, False, waves=[("isolated", paths)])
    if args.shard:
        index, count = args.shard
        bins = shard_plan(ROOT, decision["tests"], count, load_weights(DURATIONS))
        paths = bins[index - 1]
        print("PYTEST_SHARD " + json.dumps({
            "shard": index, "of": count, "plan": plan_digest(bins), "mode": decision["mode"],
            "explicitFiles": sum(1 for path in paths if path.endswith(".py")),
            "catchAll": paths[:1] == ["tests/"],
        }, sort_keys=True), flush=True)
        if not paths:
            # A PR selection smaller than N files. Never call pytest without
            # paths: that would silently run the whole suite in this shard.
            print("PYTEST_SHARD_EMPTY " + json.dumps({"shard": index, "of": count}), flush=True)
            return 0
        probe, rest = probe_wave(paths)
        if not probe:
            return execute(ROOT, decision["tests"], forwarded, False,
                           waves=[(f"shard-{index}of{count}", paths)])
        if not rest:
            return execute(ROOT, decision["tests"], bounded_shard_workers(paths, forwarded), False,
                           waves=[(f"shard-{index}of{count}", paths)])
        probe_exit = execute(ROOT, decision["tests"], bounded_shard_workers(probe, forwarded), False,
                             waves=[("fd-probe", probe)])
        rest_exit = execute(ROOT, decision["tests"], forwarded, False,
                            waves=[(f"shard-{index}of{count}", rest)])
        return probe_exit or rest_exit
    return execute(ROOT, decision["tests"], forwarded, args.staged, grouped=args.grouped)


if __name__ == "__main__":
    raise SystemExit(main())
