"""Explicit public bindings for the daemon domain."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.daemon.impl.correlation.alarm import ALARM_THROTTLE_WINDOW_MS as ALARM_THROTTLE_WINDOW_MS
    from hyprial.daemon.impl.owner_migration.vocabulary import ARCHIVAL_COLUMNS as ARCHIVAL_COLUMNS
    from hyprial.daemon.impl.autoupdate import AUTOUPDATE_CHILD_ENV as AUTOUPDATE_CHILD_ENV
    from hyprial.daemon.impl.autoupdate import AUTOUPDATE_TRIGGER_ENV as AUTOUPDATE_TRIGGER_ENV
    from hyprial.daemon.impl.bootstrap.adapter_registration import AdapterConfigConflictError as AdapterConfigConflictError
    from hyprial.daemon.impl.bootstrap.adapter_registration import AdapterExistsError as AdapterExistsError
    from hyprial.daemon.impl.bootstrap.adapter_registration import AdapterNotFoundError as AdapterNotFoundError
    from hyprial.daemon.impl.correlation.alarm import Alarm as Alarm
    from hyprial.daemon.impl.correlation.alarm import AlarmDelivery as AlarmDelivery
    from hyprial.daemon.impl.correlation.alarm import AlarmEmitter as AlarmEmitter
    from hyprial.daemon.impl.correlation.alarm import AlarmResult as AlarmResult
    from hyprial.daemon.impl.correlation.availability_loud import AttemptIdentity as AttemptIdentity
    from hyprial.daemon.impl.autoupdate import AutoUpdateManager as AutoUpdateManager
    from hyprial.daemon.impl.transfer.archive.bundle import BundleError as BundleError
    from hyprial.daemon.impl.harnesses.claude.runtime import CLAUDE_RUNTIME_ENVIRONMENT as CLAUDE_RUNTIME_ENVIRONMENT
    from hyprial.daemon.impl.harnesses.claude.runtime import ClaudeRuntimeError as ClaudeRuntimeError
    from hyprial.daemon.impl.transfer.cleanup import CleanupError as CleanupError
    from hyprial.daemon.impl.harnesses.codex.process import CodexAppServerRpcError as CodexAppServerRpcError
    from hyprial.daemon.impl.harnesses.codex.process import resolve_codex_executable as resolve_codex_executable
    from hyprial.daemon.impl.harnesses.codex.app_server import CodexInteractiveAppServer as CodexInteractiveAppServer
    from hyprial.daemon.impl.harnesses.codex.carrier import CodexInteractiveCarrier as CodexInteractiveCarrier
    from hyprial.daemon.impl.harnesses.capabilities import DECLARED_HARNESSES as DECLARED_HARNESSES
    from hyprial.daemon.impl.configuration.network_profile import DEFAULT_PROFILE as DEFAULT_PROFILE
    from hyprial.daemon.impl.application import DaemonApplication as DaemonApplication
    from hyprial.daemon.impl.configuration.ownership import DaemonOwnershipBusy as DaemonOwnershipBusy
    from hyprial.daemon.impl.configuration.ownership import DaemonStateOwnershipFence as DaemonStateOwnershipFence
    from hyprial.daemon.impl.desired_state.store import DesiredStateStore as DesiredStateStore
    from hyprial.daemon.impl.harnesses.dsh.api import DshHttpApi as DshHttpApi
    from hyprial.daemon.impl.operations.management import EnsureSquireRegistryCommand as EnsureSquireRegistryCommand
    from hyprial.daemon.impl.transfer.archive.envelope import EnvelopeError as EnvelopeError
    from hyprial.daemon.impl.forwarding_config import FORWARDING_SETTINGS_KEY as FORWARDING_SETTINGS_KEY
    from hyprial.daemon.impl.forwarding_config import ForwardingConfigurationError as ForwardingConfigurationError
    from hyprial.daemon.impl.adapters.lark.state.records import IDENTITY_KINDS as IDENTITY_KINDS
    from hyprial.daemon.impl.adapters.lark.state.records import Identity as Identity
    from hyprial.daemon.impl.adapters.lark.credentials.onboarding import LARK_ONBOARDING_REQUIRED_EVENTS as LARK_ONBOARDING_REQUIRED_EVENTS
    from hyprial.daemon.impl.adapters.lark.credentials.onboarding import LarkOnboardingError as LarkOnboardingError
    from hyprial.daemon.impl.owner_migration.vocabulary import MIGRATION_DATABASES as MIGRATION_DATABASES
    from hyprial.daemon.impl.owner_migration.vocabulary import MIGRATION_TEXT_FILES as MIGRATION_TEXT_FILES
    from hyprial.daemon.impl.operations.management import ManagementError as ManagementError
    from hyprial.daemon.impl.adapters.lark.media.media import MediaFetchError as MediaFetchError
    from hyprial.daemon.impl.owner_migration.vocabulary import MigrationPlan as MigrationPlan
    from hyprial.daemon.impl.harnesses.model_provider import ModelProviderError as ModelProviderError
    from hyprial.daemon.impl.configuration.network_profile import NetworkProfile as NetworkProfile
    from hyprial.daemon.impl.orgfs.webserver.identity import NodekeyOwnerResolver as NodekeyOwnerResolver
    from hyprial.daemon.impl.operations.management import OfflineManagementLease as OfflineManagementLease
    from hyprial.daemon.impl.org.store import OrgContextStore as OrgContextStore
    from hyprial.daemon.impl.org.store import OrgStoreError as OrgStoreError
    from hyprial.daemon.impl.owner_migration.vocabulary import OwnerMigrationAborted as OwnerMigrationAborted
    from hyprial.daemon.impl.owner_migration.vocabulary import OwnerMigrationHostedConflict as OwnerMigrationHostedConflict
    from hyprial.daemon.impl.mcp.channel.ownership import _OwnerProcessStatus as OwnerProcessStatus
    from hyprial.daemon.impl.network.peer_reachability import PEER_CONNECT_START_TIMEOUT_SECONDS as PEER_CONNECT_START_TIMEOUT_SECONDS
    from hyprial.daemon.impl.harnesses.pi import PI_HARNESS_ATTACH_EXTENSION as PI_HARNESS_ATTACH_EXTENSION
    from hyprial.daemon.impl.harnesses.capabilities import PLUGIN_KINDS as PLUGIN_KINDS
    from hyprial.daemon.impl.configuration.network_profile import PROFILE_FILENAME as PROFILE_FILENAME
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore as PacGraphStore
    from hyprial.daemon.impl.pac.graphs.reactor import PacReactor as PacReactor
    from hyprial.daemon.impl.pac.graphs.projection import Projection as Projection
    from hyprial.daemon.impl.application.ports import QuotaWatchdogDeps as QuotaWatchdogDeps
    from hyprial.daemon.impl.transfer.landing.receive import ReceiveError as ReceiveError
    from hyprial.daemon.impl.autoupdate.alert import RestartProcessObservation as RestartProcessObservation
    from hyprial.daemon.impl.autoupdate.alert import RestartProcessState as RestartProcessState
    from hyprial.daemon.impl.bootstrap.adapter_registration import RouteInput as RouteInput
    from hyprial.daemon.impl.application.ports import RoutinePortError as RoutinePortError
    from hyprial.daemon.impl.application.ports import RoutinePortTimeout as RoutinePortTimeout
    from hyprial.daemon.impl.application.ports import RoutineRuntime as RoutineRuntime
    from hyprial.daemon.impl.application.ports import RoutineRuntimeDeps as RoutineRuntimeDeps
    from hyprial.daemon.impl.application.ports import RoutineSchemaPortError as RoutineSchemaPortError
    from hyprial.daemon.impl.squire.probe import RuntimeProber as RuntimeProber
    from hyprial.daemon.impl.autoupdate import SCHEDULE as SCHEDULE
    from hyprial.daemon.impl.configuration.network_profile import SECRETS_DIRNAME as SECRETS_DIRNAME
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import SENSITIVE_CAPABILITY_NOTES as SENSITIVE_CAPABILITY_NOTES
    from hyprial.daemon.impl.correlation.readiness_budget import START_ADMISSION_WIDTH_DEFAULT as START_ADMISSION_WIDTH_DEFAULT
    from hyprial.daemon.impl.correlation.readiness_budget import START_TIMEOUT_SECONDS_DEFAULT as START_TIMEOUT_SECONDS_DEFAULT
    from hyprial.daemon.impl.transfer.archive.session_files import SessionFileError as SessionFileError
    from hyprial.daemon.impl.operations.management import SquireRegistryResult as SquireRegistryResult
    from hyprial.daemon.impl.squire.setup import SquireSetup as SquireSetup
    from hyprial.daemon.impl.transfer.execution.ssh import SshRunner as SshRunner
    from hyprial.daemon.impl.mcp.proxy import StatelessDaemonProxy as StatelessDaemonProxy
    from hyprial.daemon.impl.network.tailcat import TAILCAT_COMMIT as TAILCAT_COMMIT
    from hyprial.daemon.impl.network.tailcat import TailcatSidecarError as TailcatSidecarError
    from hyprial.daemon.impl.autoupdate import TimerConfig as TimerConfig
    from hyprial.daemon.impl.transfer.orchestrator import TransferError as TransferError
    from hyprial.daemon.impl.autoupdate.alert import UPGRADE_ALREADY_CURRENT as UPGRADE_ALREADY_CURRENT
    from hyprial.daemon.impl.autoupdate.alert import UPGRADE_AWAITING_RESTART as UPGRADE_AWAITING_RESTART
    from hyprial.daemon.impl.autoupdate.alert import UPGRADE_DECLINED_DOWNGRADE as UPGRADE_DECLINED_DOWNGRADE
    from hyprial.daemon.impl.autoupdate.alert import UPGRADE_FAILED as UPGRADE_FAILED
    from hyprial.daemon.impl.autoupdate.alert import UPGRADE_INSTALLED as UPGRADE_INSTALLED
    from hyprial.daemon.impl.autoupdate.alert import UPGRADE_UNCONFIRMED as UPGRADE_UNCONFIRMED
    from hyprial.daemon.impl.mcp.unix import UnixDaemonConnectionFactory as UnixDaemonConnectionFactory
    from hyprial.daemon.impl.adapters.lark.credentials.onboarding import UserActionRequiredError as UserActionRequiredError
    from hyprial.identity import UserProfileError as UserProfileError
    from hyprial.identity import UserProfileStore as UserProfileStore
    from hyprial.daemon.impl.orgfs.webserver.config import WebServiceConfig as WebServiceConfig
    from hyprial.daemon.impl.pac.contracts.workflow import WorkflowSchemaError as WorkflowSchemaError
    from hyprial.daemon.impl.pac.contracts.workflow import WorkflowSpec as WorkflowSpec
    from hyprial.daemon.impl.pac.graphs.edits import activate_graph as activate_graph
    from hyprial.daemon.impl.adapters.lark.credentials.identities import adapter_namespace as adapter_namespace
    from hyprial.daemon.impl.bootstrap.adapter_registration import add_gateway_route as add_gateway_route
    from hyprial.daemon.impl.bootstrap.adapter_registration import add_lark_gateway as add_lark_gateway
    from hyprial.daemon.impl.pac.graphs.edits import add_node as add_node
    from hyprial.daemon.impl.application.netendpoints.endpoints import _lock_wait_timeout as application_lock_wait_timeout
    from hyprial.daemon.impl.transfer.archive.bundle import assert_bundle_identity as assert_bundle_identity
    from hyprial.daemon.impl.correlation.alarm import audience_for_sender as audience_for_sender
    from hyprial.daemon.impl.owner_migration.planning import build_plan as build_plan
    from hyprial.daemon.impl.dispatch.matrix import candidate_json as candidate_json
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import capability_scopes as capability_scopes
    from hyprial.daemon.impl.orgfs.webserver.config import check_config as check_config
    from hyprial.daemon.impl.network.peer_reachability import classify_peer_reachability as classify_peer_reachability
    from hyprial.daemon.impl.harnesses.model_provider import claude_provider_environment as claude_provider_environment
    from hyprial.daemon.impl.transfer.archive.session_files import claude_session_target as claude_session_target
    from hyprial.daemon.impl.harnesses.runtime._launch_cleanup import cleanup_launch_resources as cleanup_launch_resources
    from hyprial.daemon.impl.pac.graphs.edits import close_graph as close_graph
    from hyprial.daemon.impl.harnesses.model_provider import codex_provider_configuration as codex_provider_configuration
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import configured_app_id as configured_app_id
    from hyprial.daemon.impl.adapters.lark.credentials.identities import configured_identity_gateway as configured_identity_gateway
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import configured_scope_client as configured_scope_client
    import hyprial.daemon.impl.adapters.lark.contracts.lifecycle as contracts_lifecycle
    from hyprial.daemon.impl.pac.graphs.edits import create_graph as create_graph
    import hyprial.daemon.impl.adapters.lark.credentials.reauth as credentials_reauth
    from hyprial.daemon.impl.forwarding_config import daemon_forwarding_environment as daemon_forwarding_environment
    from hyprial.daemon.impl.harnesses.capabilities import declare as declare
    from hyprial.daemon.impl.pac.storage.store import default_database_path as default_database_path
    from hyprial.daemon.impl.squire.setup import derive_setup_identity as derive_setup_identity
    from hyprial.daemon.impl.autoupdate import detect_platform as detect_platform
    from hyprial.daemon.impl.owner_migration.apply import detect_previous_owner as detect_previous_owner
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import developer_console_permission_url as developer_console_permission_url
    from hyprial.daemon.impl.dispatch.matrix import diagnose as diagnose
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import diagnose_scopes as diagnose_scopes
    from hyprial.daemon.impl.dispatch.matrix import dispatch_reminders as dispatch_reminders
    from hyprial.daemon.impl.org.document import document_summary as document_summary
    from hyprial.daemon.impl.transfer.landing.runtime import downgrade_state as downgrade_state
    from hyprial.daemon.impl.network.tailcat import ensure_device_key as ensure_device_key
    from hyprial.daemon.impl.dispatch.matrix import ensure_dispatch_policy as ensure_dispatch_policy
    from hyprial.daemon.impl.bootstrap.adapter_registration import ensure_lark_gateway_addable as ensure_lark_gateway_addable
    from hyprial.daemon.impl.pac.graphs.subscription import events_since as events_since
    from hyprial.daemon.impl.transfer.cleanup import execute_cleanup as execute_cleanup
    import hyprial.daemon.impl.transfer.execution.container as execution_container
    from hyprial.daemon.impl.transfer.archive.bundle import export_bundle as export_bundle
    from hyprial.daemon.impl.adapters.lark.media.media import fetch_media as fetch_media
    from hyprial.daemon.impl.adapters.lark.credentials.reauth import find_lark_cli as find_lark_cli
    from hyprial.daemon.impl.harnesses.pi.loader import find_pi_package_root as find_pi_package_root
    from hyprial.daemon.impl.pac.graphs.events import follow_lifetime as follow_lifetime
    import hyprial.daemon.impl.harnesses.runtime.tmux as harnesses_tmux
    from hyprial.daemon.impl.correlation.availability_loud import is_human_facing_requester as is_human_facing_requester
    import hyprial.daemon.impl.adapters.lark.contracts.lifecycle as lark_lifecycle
    import hyprial.daemon.impl.adapters.lark.credentials.reauth as lark_reauth
    from hyprial.daemon.impl.autoupdate.alert import latest_start_failure as latest_start_failure
    from hyprial.daemon.impl.pac.contracts.workflow import launch as launch
    from hyprial.daemon.impl.bootstrap.adapter_registration import list_gateway_routes as list_gateway_routes
    from hyprial.daemon.impl.configuration.home_guard import live_daemon_pid as live_daemon_pid
    from hyprial.daemon.impl.transfer.archive.envelope import load_envelope as load_envelope
    from hyprial.daemon.impl.pac.contracts.workflow import load_workflow_text as load_workflow_text
    from hyprial.daemon.impl.network.tailcat import locate_tailcat_sidecar as locate_tailcat_sidecar
    from hyprial.daemon.impl.pac.contracts.workflow import name as name
    from hyprial.daemon.impl.application.netendpoints.endpoints import network_isolated_from_environment as network_isolated_from_environment
    from hyprial.daemon.impl.correlation.availability_loud import no_progress_budget_seconds as no_progress_budget_seconds
    from hyprial.daemon.impl.correlation.availability_loud import no_progress_notice as no_progress_notice
    from hyprial.daemon.impl.pac.views.context import node_context as node_context
    from hyprial.daemon.impl.configuration.identity import node_owner_or_none as node_owner_or_none
    from hyprial.daemon.impl.autoupdate.alert import notify_restore_followup as notify_restore_followup
    from hyprial.daemon.impl.autoupdate.alert import notify_upgrade_failure as notify_upgrade_failure
    from hyprial.daemon.impl.autoupdate.alert import notify_upgrade_outcome as notify_upgrade_outcome
    from hyprial.daemon.impl.adapters.lark.state.readers import observed_chats as observed_chats
    from hyprial.daemon.impl.transfer.archive.envelope import open_envelope as open_envelope
    from hyprial.daemon.impl.adapters.lark.credentials.identities import open_identity_store as open_identity_store
    from hyprial.daemon.impl.mcp.channel.ownership import _owner_process_status as owner_process_status
    import hyprial.daemon.impl.pac.views.missions as pac_missions
    import hyprial.daemon.impl.pac.views.overview as pac_overview
    from hyprial.daemon.impl.org.document import parse_document as parse_document
    from hyprial.daemon.impl.adapters.lark.media.media import parse_media_ref as parse_media_ref
    from hyprial.daemon.impl.harnesses.model_provider import pi_model_args as pi_model_args
    from hyprial.daemon.impl.harnesses.pi.loader import pi_sdk_launch_from_public_projection as pi_sdk_launch_from_public_projection
    from hyprial.daemon.impl.harnesses.pi.session import pi_session_id as pi_session_id
    from hyprial.daemon.impl.transfer.archive.session_files import pi_session_target as pi_session_target
    from hyprial.daemon.impl.transfer.cleanup import plan_cleanup as plan_cleanup
    from hyprial.daemon.impl.pac.graphs.reactor import planned_to_json as planned_to_json
    from hyprial.daemon.impl.squire.probe import probe_combinations as probe_combinations
    from hyprial.daemon.impl.transfer.execution.smolvm import probe_smolvm as probe_smolvm
    from hyprial.daemon.impl.processes.process_diagnostics import process_cpu_seconds as process_cpu_seconds
    from hyprial.daemon.impl.autoupdate import read_last_run as read_last_run
    from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity as read_process_identity
    from hyprial.daemon.impl.configuration.network_profile import read_profile as read_profile
    from hyprial.daemon.impl.configuration.identity import read_settings_identity as read_settings_identity
    from hyprial.daemon.impl.adapters.lark.credentials.onboarding import reauthorize_lark_app as reauthorize_lark_app
    from hyprial.daemon.impl.transfer.landing.receive import receive_bundle as receive_bundle
    from hyprial.daemon.impl.autoupdate.alert import record_alert_outcome as record_alert_outcome
    from hyprial.daemon.impl.bootstrap.adapter_registration import remove_gateway_route as remove_gateway_route
    from hyprial.daemon.impl.bootstrap.adapter_registration import remove_lark_gateway as remove_lark_gateway
    from hyprial.daemon.impl.adapters.lark.state.readers import replied_chats as replied_chats
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import request_scope_authorization as request_scope_authorization
    from hyprial.daemon.impl.dispatch.matrix import resolve as resolve
    from hyprial.daemon.impl.harnesses.pi.loader import resolve_approved_pi_project as resolve_approved_pi_project
    from hyprial.daemon.impl.adapters.lark.credentials.onboarding import resolve_lark_app_credential as resolve_lark_app_credential
    from hyprial.daemon.impl.configuration.identity import resolve_node_owner as resolve_node_owner
    from hyprial.daemon.impl.transfer.archive.envelope import resolve_policy as resolve_policy
    from hyprial.daemon.impl.configuration.network_profile import resolve_profile as resolve_profile
    from hyprial.daemon.impl.autoupdate.alert import restart_failure_detail as restart_failure_detail
    from hyprial.daemon.impl.correlation.readiness_budget import restore_followup_budget_seconds as restore_followup_budget_seconds
    from hyprial.daemon.impl.autoupdate.alert import run_self_check as run_self_check
    from hyprial.daemon.impl.transfer.orchestrator import run_transfer as run_transfer
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import scope_apply_url as scope_apply_url
    from hyprial.daemon.impl.transfer.archive.envelope import seal_envelope as seal_envelope
    from hyprial.daemon.impl.org.document import serialize_document as serialize_document
    from hyprial.daemon.impl.orgfs.webserver.server import serve as serve
    from hyprial.daemon.impl.mcp.channel.session import serve_channel_stdio as serve_channel_stdio
    from hyprial.daemon.impl.mcp.server import serve_worker_stdio as serve_worker_stdio
    from hyprial.daemon.impl.harnesses.runtime.tmux import session_name_for_actor as session_name_for_actor
    from hyprial.daemon.impl.mcp.channel.loops import signal_channel_recovery as signal_channel_recovery
    from hyprial.daemon.impl.pac.graphs.events import silence_broken_pipe as silence_broken_pipe
    from hyprial.daemon.impl.transfer.execution.smolvm import _regular_read as smolvm_regular_read
    from hyprial.daemon.impl.pac.graphs.subscription import snapshot as snapshot
    from hyprial.daemon.impl.autoupdate.alert import start_failure_line as start_failure_line
    from hyprial.daemon.impl.harnesses.capabilities import support as support
    from hyprial.daemon.impl.adapters.lark.credentials.identities import sync_identities as sync_identities
    from hyprial.daemon.impl.network.peer_reachability import tcp_probe as tcp_probe
    import hyprial.daemon.impl.transfer.execution.container as transfer_container
    from hyprial.daemon.impl.correlation.availability_loud import unavailable_notice as unavailable_notice
    import hyprial.daemon.impl.updates as updates
    from hyprial.daemon.impl.network.proxy_route import url_opener as url_opener
    from hyprial.daemon.impl.adapters.lark.credentials.scopes import valid_scope_name as valid_scope_name
    from hyprial.daemon.impl.transfer.archive.bundle import validate_bundle as validate_bundle
    from hyprial.daemon.impl.harnesses.claude.runtime import validate_claude_auth_environment as validate_claude_auth_environment
    from hyprial.daemon.impl.harnesses.model_provider import validate_model_selection as validate_model_selection
    from hyprial.daemon.impl.configuration.network_profile import validate_profile as validate_profile
    import hyprial.daemon.impl.pac.views.missions as views_missions
    import hyprial.daemon.impl.pac.views.overview as views_overview
    import hyprial.daemon.impl.pac.views.work_items as views_work_items
    from hyprial.daemon.impl.network.tailcat import verify_tailcat_sidecar as verify_tailcat_sidecar
    from hyprial.daemon.impl.orgfs.web import with_human_web_urls as with_human_web_urls
    from hyprial.daemon.impl.dispatch.matrix import workflow_reminders as workflow_reminders
    from hyprial.daemon.impl.transfer.archive.envelope import write_envelope as write_envelope
    from hyprial.daemon.impl.autoupdate.alert import write_failure_marker as write_failure_marker
    from hyprial.daemon.impl.forwarding_config import write_forwarding_mode as write_forwarding_mode
    from hyprial.daemon.impl.autoupdate import write_last_run as write_last_run
    from hyprial.daemon.impl.org.orgfs_migration import write_org_fetch_source as write_org_fetch_source
    from hyprial.daemon.impl.configuration.network_profile import write_profile as write_profile
    from hyprial.daemon.impl.configuration.identity import write_settings_identity as write_settings_identity
    from hyprial.daemon.impl.configuration.identity import write_settings_owner as write_settings_owner
    from hyprial.daemon.impl.dispatch.matrix import write_workflow_reminder as write_workflow_reminder

_FACADE_EXPORTS = {
    'ALARM_THROTTLE_WINDOW_MS': ('hyprial.daemon.impl.correlation.alarm', 'ALARM_THROTTLE_WINDOW_MS'),
    'ARCHIVAL_COLUMNS': ('hyprial.daemon.impl.owner_migration.vocabulary', 'ARCHIVAL_COLUMNS'),
    'AUTOUPDATE_CHILD_ENV': ('hyprial.daemon.impl.autoupdate', 'AUTOUPDATE_CHILD_ENV'),
    'AUTOUPDATE_TRIGGER_ENV': ('hyprial.daemon.impl.autoupdate', 'AUTOUPDATE_TRIGGER_ENV'),
    'AdapterConfigConflictError': ('hyprial.daemon.impl.bootstrap.adapter_registration', 'AdapterConfigConflictError'),
    'AdapterExistsError': ('hyprial.daemon.impl.bootstrap.adapter_registration', 'AdapterExistsError'),
    'AdapterNotFoundError': ('hyprial.daemon.impl.bootstrap.adapter_registration', 'AdapterNotFoundError'),
    'Alarm': ('hyprial.daemon.impl.correlation.alarm', 'Alarm'),
    'AlarmDelivery': ('hyprial.daemon.impl.correlation.alarm', 'AlarmDelivery'),
    'AlarmEmitter': ('hyprial.daemon.impl.correlation.alarm', 'AlarmEmitter'),
    'AlarmResult': ('hyprial.daemon.impl.correlation.alarm', 'AlarmResult'),
    'AttemptIdentity': ('hyprial.daemon.impl.correlation.availability_loud', 'AttemptIdentity'),
    'AutoUpdateManager': ('hyprial.daemon.impl.autoupdate', 'AutoUpdateManager'),
    'BundleError': ('hyprial.daemon.impl.transfer.archive.bundle', 'BundleError'),
    'CLAUDE_RUNTIME_ENVIRONMENT': ('hyprial.daemon.impl.harnesses.claude.runtime', 'CLAUDE_RUNTIME_ENVIRONMENT'),
    'ClaudeRuntimeError': ('hyprial.daemon.impl.harnesses.claude.runtime', 'ClaudeRuntimeError'),
    'CleanupError': ('hyprial.daemon.impl.transfer.cleanup', 'CleanupError'),
    'CodexAppServerRpcError': ('hyprial.daemon.impl.harnesses.codex.process', 'CodexAppServerRpcError'),
    'CodexInteractiveAppServer': ('hyprial.daemon.impl.harnesses.codex.app_server', 'CodexInteractiveAppServer'),
    'CodexInteractiveCarrier': ('hyprial.daemon.impl.harnesses.codex.carrier', 'CodexInteractiveCarrier'),
    'resolve_codex_executable': ('hyprial.daemon.impl.harnesses.codex.process', 'resolve_codex_executable'),
    'DECLARED_HARNESSES': ('hyprial.daemon.impl.harnesses.capabilities', 'DECLARED_HARNESSES'),
    'DEFAULT_PROFILE': ('hyprial.daemon.impl.configuration.network_profile', 'DEFAULT_PROFILE'),
    'DaemonApplication': ('hyprial.daemon.impl.application', 'DaemonApplication'),
    'DaemonOwnershipBusy': ('hyprial.daemon.impl.configuration.ownership', 'DaemonOwnershipBusy'),
    'DaemonStateOwnershipFence': ('hyprial.daemon.impl.configuration.ownership', 'DaemonStateOwnershipFence'),
    'DesiredStateStore': ('hyprial.daemon.impl.desired_state.store', 'DesiredStateStore'),
    'DshHttpApi': ('hyprial.daemon.impl.harnesses.dsh.api', 'DshHttpApi'),
    'EnsureSquireRegistryCommand': ('hyprial.daemon.impl.operations.management', 'EnsureSquireRegistryCommand'),
    'EnvelopeError': ('hyprial.daemon.impl.transfer.archive.envelope', 'EnvelopeError'),
    'FORWARDING_SETTINGS_KEY': ('hyprial.daemon.impl.forwarding_config', 'FORWARDING_SETTINGS_KEY'),
    'ForwardingConfigurationError': ('hyprial.daemon.impl.forwarding_config', 'ForwardingConfigurationError'),
    'IDENTITY_KINDS': ('hyprial.daemon.impl.adapters.lark.state.records', 'IDENTITY_KINDS'),
    'Identity': ('hyprial.daemon.impl.adapters.lark.state.records', 'Identity'),
    'LARK_ONBOARDING_REQUIRED_EVENTS': ('hyprial.daemon.impl.adapters.lark.credentials.onboarding', 'LARK_ONBOARDING_REQUIRED_EVENTS'),
    'LarkOnboardingError': ('hyprial.daemon.impl.adapters.lark.credentials.onboarding', 'LarkOnboardingError'),
    'MIGRATION_DATABASES': ('hyprial.daemon.impl.owner_migration.vocabulary', 'MIGRATION_DATABASES'),
    'MIGRATION_TEXT_FILES': ('hyprial.daemon.impl.owner_migration.vocabulary', 'MIGRATION_TEXT_FILES'),
    'ManagementError': ('hyprial.daemon.impl.operations.management', 'ManagementError'),
    'MediaFetchError': ('hyprial.daemon.impl.adapters.lark.media.media', 'MediaFetchError'),
    'MigrationPlan': ('hyprial.daemon.impl.owner_migration.vocabulary', 'MigrationPlan'),
    'ModelProviderError': ('hyprial.daemon.impl.harnesses.model_provider', 'ModelProviderError'),
    'NetworkProfile': ('hyprial.daemon.impl.configuration.network_profile', 'NetworkProfile'),
    'NodekeyOwnerResolver': ('hyprial.daemon.impl.orgfs.webserver.identity', 'NodekeyOwnerResolver'),
    'OfflineManagementLease': ('hyprial.daemon.impl.operations.management', 'OfflineManagementLease'),
    'OrgContextStore': ('hyprial.daemon.impl.org.store', 'OrgContextStore'),
    'OrgStoreError': ('hyprial.daemon.impl.org.store', 'OrgStoreError'),
    'OwnerMigrationAborted': ('hyprial.daemon.impl.owner_migration.vocabulary', 'OwnerMigrationAborted'),
    'OwnerMigrationHostedConflict': ('hyprial.daemon.impl.owner_migration.vocabulary', 'OwnerMigrationHostedConflict'),
    'OwnerProcessStatus': ('hyprial.daemon.impl.mcp.channel.ownership', '_OwnerProcessStatus'),
    'PEER_CONNECT_START_TIMEOUT_SECONDS': ('hyprial.daemon.impl.network.peer_reachability', 'PEER_CONNECT_START_TIMEOUT_SECONDS'),
    'PI_HARNESS_ATTACH_EXTENSION': ('hyprial.daemon.impl.harnesses.pi', 'PI_HARNESS_ATTACH_EXTENSION'),
    'PLUGIN_KINDS': ('hyprial.daemon.impl.harnesses.capabilities', 'PLUGIN_KINDS'),
    'PROFILE_FILENAME': ('hyprial.daemon.impl.configuration.network_profile', 'PROFILE_FILENAME'),
    'PacGraphStore': ('hyprial.daemon.impl.pac.storage.store', 'PacGraphStore'),
    'PacReactor': ('hyprial.daemon.impl.pac.graphs.reactor', 'PacReactor'),
    'Projection': ('hyprial.daemon.impl.pac.graphs.projection', 'Projection'),
    'QuotaWatchdogDeps': ('hyprial.daemon.impl.application.ports', 'QuotaWatchdogDeps'),
    'ReceiveError': ('hyprial.daemon.impl.transfer.landing.receive', 'ReceiveError'),
    'RestartProcessObservation': ('hyprial.daemon.impl.autoupdate.alert', 'RestartProcessObservation'),
    'RestartProcessState': ('hyprial.daemon.impl.autoupdate.alert', 'RestartProcessState'),
    'RouteInput': ('hyprial.daemon.impl.bootstrap.adapter_registration', 'RouteInput'),
    'RoutinePortError': ('hyprial.daemon.impl.application.ports', 'RoutinePortError'),
    'RoutinePortTimeout': ('hyprial.daemon.impl.application.ports', 'RoutinePortTimeout'),
    'RoutineRuntime': ('hyprial.daemon.impl.application.ports', 'RoutineRuntime'),
    'RoutineRuntimeDeps': ('hyprial.daemon.impl.application.ports', 'RoutineRuntimeDeps'),
    'RoutineSchemaPortError': ('hyprial.daemon.impl.application.ports', 'RoutineSchemaPortError'),
    'RuntimeProber': ('hyprial.daemon.impl.squire.probe', 'RuntimeProber'),
    'SCHEDULE': ('hyprial.daemon.impl.autoupdate', 'SCHEDULE'),
    'SECRETS_DIRNAME': ('hyprial.daemon.impl.configuration.network_profile', 'SECRETS_DIRNAME'),
    'SENSITIVE_CAPABILITY_NOTES': ('hyprial.daemon.impl.adapters.lark.credentials.scopes', 'SENSITIVE_CAPABILITY_NOTES'),
    'START_ADMISSION_WIDTH_DEFAULT': ('hyprial.daemon.impl.correlation.readiness_budget', 'START_ADMISSION_WIDTH_DEFAULT'),
    'START_TIMEOUT_SECONDS_DEFAULT': ('hyprial.daemon.impl.correlation.readiness_budget', 'START_TIMEOUT_SECONDS_DEFAULT'),
    'SessionFileError': ('hyprial.daemon.impl.transfer.archive.session_files', 'SessionFileError'),
    'SquireRegistryResult': ('hyprial.daemon.impl.operations.management', 'SquireRegistryResult'),
    'SquireSetup': ('hyprial.daemon.impl.squire.setup', 'SquireSetup'),
    'SshRunner': ('hyprial.daemon.impl.transfer.execution.ssh', 'SshRunner'),
    'StatelessDaemonProxy': ('hyprial.daemon.impl.mcp.proxy', 'StatelessDaemonProxy'),
    'TAILCAT_COMMIT': ('hyprial.daemon.impl.network.tailcat', 'TAILCAT_COMMIT'),
    'TailcatSidecarError': ('hyprial.daemon.impl.network.tailcat', 'TailcatSidecarError'),
    'TimerConfig': ('hyprial.daemon.impl.autoupdate', 'TimerConfig'),
    'TransferError': ('hyprial.daemon.impl.transfer.orchestrator', 'TransferError'),
    'UPGRADE_ALREADY_CURRENT': ('hyprial.daemon.impl.autoupdate.alert', 'UPGRADE_ALREADY_CURRENT'),
    'UPGRADE_AWAITING_RESTART': ('hyprial.daemon.impl.autoupdate.alert', 'UPGRADE_AWAITING_RESTART'),
    'UPGRADE_DECLINED_DOWNGRADE': ('hyprial.daemon.impl.autoupdate.alert', 'UPGRADE_DECLINED_DOWNGRADE'),
    'UPGRADE_FAILED': ('hyprial.daemon.impl.autoupdate.alert', 'UPGRADE_FAILED'),
    'UPGRADE_INSTALLED': ('hyprial.daemon.impl.autoupdate.alert', 'UPGRADE_INSTALLED'),
    'UPGRADE_UNCONFIRMED': ('hyprial.daemon.impl.autoupdate.alert', 'UPGRADE_UNCONFIRMED'),
    'UnixDaemonConnectionFactory': ('hyprial.daemon.impl.mcp.unix', 'UnixDaemonConnectionFactory'),
    'UserActionRequiredError': ('hyprial.daemon.impl.adapters.lark.credentials.onboarding', 'UserActionRequiredError'),
    'UserProfileError': ('hyprial.identity', 'UserProfileError'),
    'UserProfileStore': ('hyprial.identity', 'UserProfileStore'),
    'WebServiceConfig': ('hyprial.daemon.impl.orgfs.webserver.config', 'WebServiceConfig'),
    'WorkflowSchemaError': ('hyprial.daemon.impl.pac.contracts.workflow', 'WorkflowSchemaError'),
    'WorkflowSpec': ('hyprial.daemon.impl.pac.contracts.workflow', 'WorkflowSpec'),
    'activate_graph': ('hyprial.daemon.impl.pac.graphs.edits', 'activate_graph'),
    'adapter_namespace': ('hyprial.daemon.impl.adapters.lark.credentials.identities', 'adapter_namespace'),
    'add_gateway_route': ('hyprial.daemon.impl.bootstrap.adapter_registration', 'add_gateway_route'),
    'add_lark_gateway': ('hyprial.daemon.impl.bootstrap.adapter_registration', 'add_lark_gateway'),
    'add_node': ('hyprial.daemon.impl.pac.graphs.edits', 'add_node'),
    'application_lock_wait_timeout': ('hyprial.daemon.impl.application.netendpoints.endpoints', '_lock_wait_timeout'),
    'assert_bundle_identity': ('hyprial.daemon.impl.transfer.archive.bundle', 'assert_bundle_identity'),
    'audience_for_sender': ('hyprial.daemon.impl.correlation.alarm', 'audience_for_sender'),
    'build_plan': ('hyprial.daemon.impl.owner_migration.planning', 'build_plan'),
    'candidate_json': ('hyprial.daemon.impl.dispatch.matrix', 'candidate_json'),
    'capability_scopes': ('hyprial.daemon.impl.adapters.lark.credentials.scopes', 'capability_scopes'),
    'check_config': ('hyprial.daemon.impl.orgfs.webserver.config', 'check_config'),
    'classify_peer_reachability': ('hyprial.daemon.impl.network.peer_reachability', 'classify_peer_reachability'),
    'claude_provider_environment': ('hyprial.daemon.impl.harnesses.model_provider', 'claude_provider_environment'),
    'claude_session_target': ('hyprial.daemon.impl.transfer.archive.session_files', 'claude_session_target'),
    'cleanup_launch_resources': ('hyprial.daemon.impl.harnesses.runtime._launch_cleanup', 'cleanup_launch_resources'),
    'close_graph': ('hyprial.daemon.impl.pac.graphs.edits', 'close_graph'),
    'codex_provider_configuration': ('hyprial.daemon.impl.harnesses.model_provider', 'codex_provider_configuration'),
    'configured_app_id': ('hyprial.daemon.impl.adapters.lark.credentials.scopes', 'configured_app_id'),
    'configured_identity_gateway': ('hyprial.daemon.impl.adapters.lark.credentials.identities', 'configured_identity_gateway'),
    'configured_scope_client': ('hyprial.daemon.impl.adapters.lark.credentials.scopes', 'configured_scope_client'),
    'contracts_lifecycle': ('hyprial.daemon.impl.adapters.lark.contracts.lifecycle', None),
    'create_graph': ('hyprial.daemon.impl.pac.graphs.edits', 'create_graph'),
    'credentials_reauth': ('hyprial.daemon.impl.adapters.lark.credentials.reauth', None),
    'daemon_forwarding_environment': ('hyprial.daemon.impl.forwarding_config', 'daemon_forwarding_environment'),
    'declare': ('hyprial.daemon.impl.harnesses.capabilities', 'declare'),
    'default_database_path': ('hyprial.daemon.impl.pac.storage.store', 'default_database_path'),
    'derive_setup_identity': ('hyprial.daemon.impl.squire.setup', 'derive_setup_identity'),
    'detect_platform': ('hyprial.daemon.impl.autoupdate', 'detect_platform'),
    'detect_previous_owner': ('hyprial.daemon.impl.owner_migration.apply', 'detect_previous_owner'),
    'developer_console_permission_url': ('hyprial.daemon.impl.adapters.lark.credentials.scopes', 'developer_console_permission_url'),
    'diagnose': ('hyprial.daemon.impl.dispatch.matrix', 'diagnose'),
    'diagnose_scopes': ('hyprial.daemon.impl.adapters.lark.credentials.scopes', 'diagnose_scopes'),
    'dispatch_reminders': ('hyprial.daemon.impl.dispatch.matrix', 'dispatch_reminders'),
    'document_summary': ('hyprial.daemon.impl.org.document', 'document_summary'),
    'downgrade_state': ('hyprial.daemon.impl.transfer.landing.runtime', 'downgrade_state'),
    'ensure_device_key': ('hyprial.daemon.impl.network.tailcat', 'ensure_device_key'),
    'ensure_dispatch_policy': ('hyprial.daemon.impl.dispatch.matrix', 'ensure_dispatch_policy'),
    'ensure_lark_gateway_addable': ('hyprial.daemon.impl.bootstrap.adapter_registration', 'ensure_lark_gateway_addable'),
    'events_since': ('hyprial.daemon.impl.pac.graphs.subscription', 'events_since'),
    'execute_cleanup': ('hyprial.daemon.impl.transfer.cleanup', 'execute_cleanup'),
    'execution_container': ('hyprial.daemon.impl.transfer.execution.container', None),
    'export_bundle': ('hyprial.daemon.impl.transfer.archive.bundle', 'export_bundle'),
    'fetch_media': ('hyprial.daemon.impl.adapters.lark.media.media', 'fetch_media'),
    'find_lark_cli': ('hyprial.daemon.impl.adapters.lark.credentials.reauth', 'find_lark_cli'),
    'find_pi_package_root': ('hyprial.daemon.impl.harnesses.pi.loader', 'find_pi_package_root'),
    'follow_lifetime': ('hyprial.daemon.impl.pac.graphs.events', 'follow_lifetime'),
    'harnesses_tmux': ('hyprial.daemon.impl.harnesses.runtime.tmux', None),
    'is_human_facing_requester': ('hyprial.daemon.impl.correlation.availability_loud', 'is_human_facing_requester'),
    'lark_lifecycle': ('hyprial.daemon.impl.adapters.lark.contracts.lifecycle', None),
    'lark_reauth': ('hyprial.daemon.impl.adapters.lark.credentials.reauth', None),
    'latest_start_failure': ('hyprial.daemon.impl.autoupdate.alert', 'latest_start_failure'),
    'launch': ('hyprial.daemon.impl.pac.contracts.workflow', 'launch'),
    'list_gateway_routes': ('hyprial.daemon.impl.bootstrap.adapter_registration', 'list_gateway_routes'),
    'live_daemon_pid': ('hyprial.daemon.impl.configuration.home_guard', 'live_daemon_pid'),
    'load_envelope': ('hyprial.daemon.impl.transfer.archive.envelope', 'load_envelope'),
    'load_workflow_text': ('hyprial.daemon.impl.pac.contracts.workflow', 'load_workflow_text'),
    'locate_tailcat_sidecar': ('hyprial.daemon.impl.network.tailcat', 'locate_tailcat_sidecar'),
    'name': ('hyprial.daemon.impl.pac.contracts.workflow', 'name'),
    'network_isolated_from_environment': ('hyprial.daemon.impl.application.netendpoints.endpoints', 'network_isolated_from_environment'),
    'no_progress_budget_seconds': ('hyprial.daemon.impl.correlation.availability_loud', 'no_progress_budget_seconds'),
    'no_progress_notice': ('hyprial.daemon.impl.correlation.availability_loud', 'no_progress_notice'),
    'node_context': ('hyprial.daemon.impl.pac.views.context', 'node_context'),
    'node_owner_or_none': ('hyprial.daemon.impl.configuration.identity', 'node_owner_or_none'),
    'notify_restore_followup': ('hyprial.daemon.impl.autoupdate.alert', 'notify_restore_followup'),
    'notify_upgrade_failure': ('hyprial.daemon.impl.autoupdate.alert', 'notify_upgrade_failure'),
    'notify_upgrade_outcome': ('hyprial.daemon.impl.autoupdate.alert', 'notify_upgrade_outcome'),
    'observed_chats': ('hyprial.daemon.impl.adapters.lark.state.readers', 'observed_chats'),
    'open_envelope': ('hyprial.daemon.impl.transfer.archive.envelope', 'open_envelope'),
    'open_identity_store': ('hyprial.daemon.impl.adapters.lark.credentials.identities', 'open_identity_store'),
    'owner_process_status': ('hyprial.daemon.impl.mcp.channel.ownership', '_owner_process_status'),
    'pac_missions': ('hyprial.daemon.impl.pac.views.missions', None),
    'pac_overview': ('hyprial.daemon.impl.pac.views.overview', None),
    'parse_document': ('hyprial.daemon.impl.org.document', 'parse_document'),
    'parse_media_ref': ('hyprial.daemon.impl.adapters.lark.media.media', 'parse_media_ref'),
    'pi_model_args': ('hyprial.daemon.impl.harnesses.model_provider', 'pi_model_args'),
    'pi_sdk_launch_from_public_projection': ('hyprial.daemon.impl.harnesses.pi.loader', 'pi_sdk_launch_from_public_projection'),
    'pi_session_id': ('hyprial.daemon.impl.harnesses.pi.session', 'pi_session_id'),
    'pi_session_target': ('hyprial.daemon.impl.transfer.archive.session_files', 'pi_session_target'),
    'plan_cleanup': ('hyprial.daemon.impl.transfer.cleanup', 'plan_cleanup'),
    'planned_to_json': ('hyprial.daemon.impl.pac.graphs.reactor', 'planned_to_json'),
    'probe_combinations': ('hyprial.daemon.impl.squire.probe', 'probe_combinations'),
    'probe_smolvm': ('hyprial.daemon.impl.transfer.execution.smolvm', 'probe_smolvm'),
    'process_cpu_seconds': ('hyprial.daemon.impl.processes.process_diagnostics', 'process_cpu_seconds'),
    'read_last_run': ('hyprial.daemon.impl.autoupdate', 'read_last_run'),
    'read_process_identity': ('hyprial.daemon.impl.mcp.channel.ownership', '_read_process_identity'),
    'read_profile': ('hyprial.daemon.impl.configuration.network_profile', 'read_profile'),
    'read_settings_identity': ('hyprial.daemon.impl.configuration.identity', 'read_settings_identity'),
    'reauthorize_lark_app': ('hyprial.daemon.impl.adapters.lark.credentials.onboarding', 'reauthorize_lark_app'),
    'receive_bundle': ('hyprial.daemon.impl.transfer.landing.receive', 'receive_bundle'),
    'record_alert_outcome': ('hyprial.daemon.impl.autoupdate.alert', 'record_alert_outcome'),
    'remove_gateway_route': ('hyprial.daemon.impl.bootstrap.adapter_registration', 'remove_gateway_route'),
    'remove_lark_gateway': ('hyprial.daemon.impl.bootstrap.adapter_registration', 'remove_lark_gateway'),
    'replied_chats': ('hyprial.daemon.impl.adapters.lark.state.readers', 'replied_chats'),
    'request_scope_authorization': ('hyprial.daemon.impl.adapters.lark.credentials.scopes', 'request_scope_authorization'),
    'resolve': ('hyprial.daemon.impl.dispatch.matrix', 'resolve'),
    'resolve_approved_pi_project': ('hyprial.daemon.impl.harnesses.pi.loader', 'resolve_approved_pi_project'),
    'resolve_lark_app_credential': ('hyprial.daemon.impl.adapters.lark.credentials.onboarding', 'resolve_lark_app_credential'),
    'resolve_node_owner': ('hyprial.daemon.impl.configuration.identity', 'resolve_node_owner'),
    'resolve_policy': ('hyprial.daemon.impl.transfer.archive.envelope', 'resolve_policy'),
    'resolve_profile': ('hyprial.daemon.impl.configuration.network_profile', 'resolve_profile'),
    'restart_failure_detail': ('hyprial.daemon.impl.autoupdate.alert', 'restart_failure_detail'),
    'restore_followup_budget_seconds': ('hyprial.daemon.impl.correlation.readiness_budget', 'restore_followup_budget_seconds'),
    'run_self_check': ('hyprial.daemon.impl.autoupdate.alert', 'run_self_check'),
    'run_transfer': ('hyprial.daemon.impl.transfer.orchestrator', 'run_transfer'),
    'scope_apply_url': ('hyprial.daemon.impl.adapters.lark.credentials.scopes', 'scope_apply_url'),
    'seal_envelope': ('hyprial.daemon.impl.transfer.archive.envelope', 'seal_envelope'),
    'serialize_document': ('hyprial.daemon.impl.org.document', 'serialize_document'),
    'serve': ('hyprial.daemon.impl.orgfs.webserver.server', 'serve'),
    'serve_channel_stdio': ('hyprial.daemon.impl.mcp.channel.session', 'serve_channel_stdio'),
    'serve_worker_stdio': ('hyprial.daemon.impl.mcp.server', 'serve_worker_stdio'),
    'session_name_for_actor': ('hyprial.daemon.impl.harnesses.runtime.tmux', 'session_name_for_actor'),
    'signal_channel_recovery': ('hyprial.daemon.impl.mcp.channel.loops', 'signal_channel_recovery'),
    'silence_broken_pipe': ('hyprial.daemon.impl.pac.graphs.events', 'silence_broken_pipe'),
    'smolvm_regular_read': ('hyprial.daemon.impl.transfer.execution.smolvm', '_regular_read'),
    'snapshot': ('hyprial.daemon.impl.pac.graphs.subscription', 'snapshot'),
    'start_failure_line': ('hyprial.daemon.impl.autoupdate.alert', 'start_failure_line'),
    'support': ('hyprial.daemon.impl.harnesses.capabilities', 'support'),
    'sync_identities': ('hyprial.daemon.impl.adapters.lark.credentials.identities', 'sync_identities'),
    'tcp_probe': ('hyprial.daemon.impl.network.peer_reachability', 'tcp_probe'),
    'transfer_container': ('hyprial.daemon.impl.transfer.execution.container', None),
    'unavailable_notice': ('hyprial.daemon.impl.correlation.availability_loud', 'unavailable_notice'),
    'updates': ('hyprial.daemon.impl.updates', None),
    'url_opener': ('hyprial.daemon.impl.network.proxy_route', 'url_opener'),
    'valid_scope_name': ('hyprial.daemon.impl.adapters.lark.credentials.scopes', 'valid_scope_name'),
    'validate_bundle': ('hyprial.daemon.impl.transfer.archive.bundle', 'validate_bundle'),
    'validate_claude_auth_environment': ('hyprial.daemon.impl.harnesses.claude.runtime', 'validate_claude_auth_environment'),
    'validate_model_selection': ('hyprial.daemon.impl.harnesses.model_provider', 'validate_model_selection'),
    'validate_profile': ('hyprial.daemon.impl.configuration.network_profile', 'validate_profile'),
    'verify_tailcat_sidecar': ('hyprial.daemon.impl.network.tailcat', 'verify_tailcat_sidecar'),
    'views_missions': ('hyprial.daemon.impl.pac.views.missions', None),
    'views_overview': ('hyprial.daemon.impl.pac.views.overview', None),
    'views_work_items': ('hyprial.daemon.impl.pac.views.work_items', None),
    'with_human_web_urls': ('hyprial.daemon.impl.orgfs.web', 'with_human_web_urls'),
    'workflow_reminders': ('hyprial.daemon.impl.dispatch.matrix', 'workflow_reminders'),
    'write_envelope': ('hyprial.daemon.impl.transfer.archive.envelope', 'write_envelope'),
    'write_failure_marker': ('hyprial.daemon.impl.autoupdate.alert', 'write_failure_marker'),
    'write_forwarding_mode': ('hyprial.daemon.impl.forwarding_config', 'write_forwarding_mode'),
    'write_last_run': ('hyprial.daemon.impl.autoupdate', 'write_last_run'),
    'write_org_fetch_source': ('hyprial.daemon.impl.org.orgfs_migration', 'write_org_fetch_source'),
    'write_profile': ('hyprial.daemon.impl.configuration.network_profile', 'write_profile'),
    'write_settings_identity': ('hyprial.daemon.impl.configuration.identity', 'write_settings_identity'),
    'write_settings_owner': ('hyprial.daemon.impl.configuration.identity', 'write_settings_owner'),
    'write_workflow_reminder': ('hyprial.daemon.impl.dispatch.matrix', 'write_workflow_reminder'),
}


def __getattr__(name: str):
    try:
        module_path, symbol_name = _FACADE_EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    from importlib import import_module

    provider = import_module(module_path)
    export = provider if symbol_name is None else getattr(provider, symbol_name)
    globals()[name] = export
    return export


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    'ALARM_THROTTLE_WINDOW_MS',
    'ARCHIVAL_COLUMNS',
    'AUTOUPDATE_CHILD_ENV',
    'AUTOUPDATE_TRIGGER_ENV',
    'AdapterConfigConflictError',
    'AdapterExistsError',
    'AdapterNotFoundError',
    'Alarm',
    'AlarmDelivery',
    'AlarmEmitter',
    'AlarmResult',
    'AttemptIdentity',
    'AutoUpdateManager',
    'BundleError',
    'CLAUDE_RUNTIME_ENVIRONMENT',
    'ClaudeRuntimeError',
    'CleanupError',
    'CodexAppServerRpcError',
    'CodexInteractiveAppServer',
    'CodexInteractiveCarrier',
    'resolve_codex_executable',
    'DECLARED_HARNESSES',
    'DEFAULT_PROFILE',
    'DaemonApplication',
    'DaemonOwnershipBusy',
    'DaemonStateOwnershipFence',
    'DesiredStateStore',
    'DshHttpApi',
    'EnsureSquireRegistryCommand',
    'EnvelopeError',
    'FORWARDING_SETTINGS_KEY',
    'ForwardingConfigurationError',
    'IDENTITY_KINDS',
    'Identity',
    'LARK_ONBOARDING_REQUIRED_EVENTS',
    'LarkOnboardingError',
    'MIGRATION_DATABASES',
    'MIGRATION_TEXT_FILES',
    'ManagementError',
    'MediaFetchError',
    'MigrationPlan',
    'ModelProviderError',
    'NetworkProfile',
    'NodekeyOwnerResolver',
    'OfflineManagementLease',
    'OrgContextStore',
    'OrgStoreError',
    'OwnerMigrationAborted',
    'OwnerMigrationHostedConflict',
    'OwnerProcessStatus',
    'PEER_CONNECT_START_TIMEOUT_SECONDS',
    'PI_HARNESS_ATTACH_EXTENSION',
    'PLUGIN_KINDS',
    'PROFILE_FILENAME',
    'PacGraphStore',
    'PacReactor',
    'Projection',
    'QuotaWatchdogDeps',
    'ReceiveError',
    'RestartProcessObservation',
    'RestartProcessState',
    'RouteInput',
    'RoutinePortError',
    'RoutinePortTimeout',
    'RoutineRuntime',
    'RoutineRuntimeDeps',
    'RoutineSchemaPortError',
    'RuntimeProber',
    'SCHEDULE',
    'SECRETS_DIRNAME',
    'SENSITIVE_CAPABILITY_NOTES',
    'START_ADMISSION_WIDTH_DEFAULT',
    'START_TIMEOUT_SECONDS_DEFAULT',
    'SessionFileError',
    'SquireRegistryResult',
    'SquireSetup',
    'SshRunner',
    'StatelessDaemonProxy',
    'TAILCAT_COMMIT',
    'TailcatSidecarError',
    'TimerConfig',
    'TransferError',
    'UPGRADE_ALREADY_CURRENT',
    'UPGRADE_AWAITING_RESTART',
    'UPGRADE_DECLINED_DOWNGRADE',
    'UPGRADE_FAILED',
    'UPGRADE_INSTALLED',
    'UPGRADE_UNCONFIRMED',
    'UnixDaemonConnectionFactory',
    'UserActionRequiredError',
    'UserProfileError',
    'UserProfileStore',
    'WebServiceConfig',
    'WorkflowSchemaError',
    'WorkflowSpec',
    'activate_graph',
    'adapter_namespace',
    'add_gateway_route',
    'add_lark_gateway',
    'add_node',
    'application_lock_wait_timeout',
    'assert_bundle_identity',
    'audience_for_sender',
    'build_plan',
    'candidate_json',
    'capability_scopes',
    'check_config',
    'classify_peer_reachability',
    'claude_provider_environment',
    'claude_session_target',
    'cleanup_launch_resources',
    'close_graph',
    'codex_provider_configuration',
    'configured_app_id',
    'configured_identity_gateway',
    'configured_scope_client',
    'contracts_lifecycle',
    'create_graph',
    'credentials_reauth',
    'daemon_forwarding_environment',
    'declare',
    'default_database_path',
    'derive_setup_identity',
    'detect_platform',
    'detect_previous_owner',
    'developer_console_permission_url',
    'diagnose',
    'diagnose_scopes',
    'dispatch_reminders',
    'document_summary',
    'downgrade_state',
    'ensure_device_key',
    'ensure_dispatch_policy',
    'ensure_lark_gateway_addable',
    'events_since',
    'execute_cleanup',
    'execution_container',
    'export_bundle',
    'fetch_media',
    'find_lark_cli',
    'find_pi_package_root',
    'follow_lifetime',
    'harnesses_tmux',
    'is_human_facing_requester',
    'lark_lifecycle',
    'lark_reauth',
    'latest_start_failure',
    'launch',
    'list_gateway_routes',
    'live_daemon_pid',
    'load_envelope',
    'load_workflow_text',
    'locate_tailcat_sidecar',
    'name',
    'network_isolated_from_environment',
    'no_progress_budget_seconds',
    'no_progress_notice',
    'node_context',
    'node_owner_or_none',
    'notify_restore_followup',
    'notify_upgrade_failure',
    'notify_upgrade_outcome',
    'observed_chats',
    'open_envelope',
    'open_identity_store',
    'owner_process_status',
    'pac_missions',
    'pac_overview',
    'parse_document',
    'parse_media_ref',
    'pi_model_args',
    'pi_sdk_launch_from_public_projection',
    'pi_session_id',
    'pi_session_target',
    'plan_cleanup',
    'planned_to_json',
    'probe_combinations',
    'probe_smolvm',
    'process_cpu_seconds',
    'read_last_run',
    'read_process_identity',
    'read_profile',
    'read_settings_identity',
    'reauthorize_lark_app',
    'receive_bundle',
    'record_alert_outcome',
    'remove_gateway_route',
    'remove_lark_gateway',
    'replied_chats',
    'request_scope_authorization',
    'resolve',
    'resolve_approved_pi_project',
    'resolve_lark_app_credential',
    'resolve_node_owner',
    'resolve_policy',
    'resolve_profile',
    'restart_failure_detail',
    'restore_followup_budget_seconds',
    'run_self_check',
    'run_transfer',
    'scope_apply_url',
    'seal_envelope',
    'serialize_document',
    'serve',
    'serve_channel_stdio',
    'serve_worker_stdio',
    'session_name_for_actor',
    'signal_channel_recovery',
    'silence_broken_pipe',
    'smolvm_regular_read',
    'snapshot',
    'start_failure_line',
    'support',
    'sync_identities',
    'tcp_probe',
    'transfer_container',
    'unavailable_notice',
    'updates',
    'url_opener',
    'valid_scope_name',
    'validate_bundle',
    'validate_claude_auth_environment',
    'validate_model_selection',
    'validate_profile',
    'verify_tailcat_sidecar',
    'views_missions',
    'views_overview',
    'views_work_items',
    'with_human_web_urls',
    'workflow_reminders',
    'write_envelope',
    'write_failure_marker',
    'write_forwarding_mode',
    'write_last_run',
    'write_org_fetch_source',
    'write_profile',
    'write_settings_identity',
    'write_settings_owner',
    'write_workflow_reminder',
]
