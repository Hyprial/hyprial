"""Explicit public bindings for the identity domain."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.identity.impl.org_directory import ACTION_INVITE as ACTION_INVITE
    from hyprial.identity.impl.org_directory import ACTION_LEAVE as ACTION_LEAVE
    from hyprial.identity.impl.org_directory import ACTION_READ as ACTION_READ
    from hyprial.identity.impl.org_directory import ACTION_REMOVE as ACTION_REMOVE
    from hyprial.identity.impl.org_directory import ACTION_WRITE as ACTION_WRITE
    from hyprial.identity.impl.device import ADDRESS_RELPATH as ADDRESS_RELPATH
    from hyprial.identity.impl.agents.state.liveness import AGENT_HEARTBEAT_TTL_SECONDS as AGENT_HEARTBEAT_TTL_SECONDS
    from hyprial.identity.impl.agents.actor.ports import AcquireAgentRuntimeLaunchCommand as AcquireAgentRuntimeLaunchCommand
    from hyprial.identity.impl.agents.registry._base import Agent as Agent
    from hyprial.identity.impl.agents.actor._core import AgentActor as AgentActor
    from hyprial.identity.impl.agents.state.liveness import AgentAlreadyRunning as AgentAlreadyRunning
    from hyprial.identity.impl.agents.state.liveness import AgentBinding as AgentBinding
    from hyprial.identity.impl.agents.actor.ports import AgentCapabilityGrantCompleted as AgentCapabilityGrantCompleted
    from hyprial.identity.impl.agents.actor.ports import AgentCommand as AgentCommand
    from hyprial.identity.impl.agents.home.config import AgentConfig as AgentConfig
    from hyprial.identity.impl.agents.home.config import AgentConfigError as AgentConfigError
    from hyprial.identity.impl.agents.actor.ports import AgentDestroyReservationCompleted as AgentDestroyReservationCompleted
    from hyprial.identity.impl.agents.actor.ports import AgentDestroySettled as AgentDestroySettled
    from hyprial.identity.impl.agents.registry._base import AgentError as AgentError
    from hyprial.identity.impl.agents.actor.ports import AgentEvent as AgentEvent
    from hyprial.identity.impl.agents.home.provisioner import AgentHomeError as AgentHomeError
    from hyprial.identity.impl.agents.state.keep_actor import AgentKeepListAuthority as AgentKeepListAuthority
    from hyprial.identity.impl.agents.state.activity import AgentKeepListError as AgentKeepListError
    from hyprial.identity.impl.agents.actor.ports import AgentLifecycleReceiptCompleted as AgentLifecycleReceiptCompleted
    from hyprial.identity.impl.agents.migration.coordinator import AgentMigrationCoordinator as AgentMigrationCoordinator
    from hyprial.identity.impl.agents.actor.ports import AgentMutationCompleted as AgentMutationCompleted
    from hyprial.identity.impl.agents.actor.ports import AgentProjection as AgentProjection
    from hyprial.identity.impl.agents.registry._core import AgentRegistry as AgentRegistry
    from hyprial.identity.impl.agents.runtime.context import AgentRuntimeContext as AgentRuntimeContext
    from hyprial.identity.impl.agents.runtime.context import AgentRuntimeError as AgentRuntimeError
    from hyprial.identity.impl.agents.actor.ports import AgentRuntimeLaunchLeaseCompleted as AgentRuntimeLaunchLeaseCompleted
    from hyprial.identity.impl.agents.migration._bindings import AgentRuntimeMigrationBindings as AgentRuntimeMigrationBindings
    from hyprial.identity.impl.agents.actor.ports import AgentSecretGrantCompleted as AgentSecretGrantCompleted
    from hyprial.identity.impl.org_directory import AllowAllPolicy as AllowAllPolicy
    from hyprial.identity.impl.users.store._base import Ambiguous as Ambiguous
    from hyprial.identity.impl.org_directory import BOOTSTRAP_STEP_KINDS as BOOTSTRAP_STEP_KINDS
    from hyprial.identity.impl.agents.actor.ports import BindAgentCommand as BindAgentCommand
    from hyprial.identity.impl.agents.actor.ports import BlockAgentCommand as BlockAgentCommand
    from hyprial.identity.impl.agents.state.worker_state import BlockingFailureAuthority as BlockingFailureAuthority
    from hyprial.identity.impl.org_directory import BootstrapStep as BootstrapStep
    from hyprial.identity.impl.agents.runtime.enforcement import CAPABILITY_ENTRY_POINTS as CAPABILITY_ENTRY_POINTS
    from hyprial.identity.impl.agents.runtime.visibility import CallerIdentity as CallerIdentity
    from hyprial.identity.impl.agents.runtime.enforcement import CapabilityRecord as CapabilityRecord
    from hyprial.identity.impl.org_directory import CasbinOrgPolicy as CasbinOrgPolicy
    from hyprial.identity.impl.agents.home.environment import ChildEnvironmentLaunch as ChildEnvironmentLaunch
    from hyprial.identity.impl.agents.actor.ports import CleanupRevokedAgentHomeCommand as CleanupRevokedAgentHomeCommand
    from hyprial.identity.impl.agents.actor.ports import ClearRestoreDispositionCommand as ClearRestoreDispositionCommand
    from hyprial.identity.impl.agents.home.config import ConfigProjectionReceipt as ConfigProjectionReceipt
    from hyprial.identity.impl.agents.actor.ports import ConfirmAgentLifecycleReceiptCommand as ConfirmAgentLifecycleReceiptCommand
    from hyprial.identity.impl.agents.actor.ports import CreateAgentCommand as CreateAgentCommand
    from hyprial.identity.impl.agents.actor.ports import CreateHostInvitedAgentCommand as CreateHostInvitedAgentCommand
    from hyprial.identity.impl.agents.actor.ports import CreateTransferHostedAgentCommand as CreateTransferHostedAgentCommand
    from hyprial.identity.impl.agents.runtime.context import DEFAULT_AGENT_TOOL_PROFILE as DEFAULT_AGENT_TOOL_PROFILE
    from hyprial.identity.impl.device import DEVICE_KEY_RELPATH as DEVICE_KEY_RELPATH
    from hyprial.identity.impl.device import DEVICE_RECORD_RELPATH as DEVICE_RECORD_RELPATH
    from hyprial.identity.impl.org_directory import DIRECTORY_DIR as DIRECTORY_DIR
    from hyprial.identity.impl.org_directory import BINDING_ASSERTION_CLIENT_ID as BINDING_ASSERTION_CLIENT_ID
    from hyprial.identity.impl.org_directory import BINDING_ASSERTION_OWNER as BINDING_ASSERTION_OWNER
    from hyprial.identity.impl.org_directory import binding_assertion_claims_unverified as binding_assertion_claims_unverified
    from hyprial.identity.impl.org_directory import binding_assertion_publish_after as binding_assertion_publish_after
    from hyprial.identity.impl.org_directory import binding_numeric_date as binding_numeric_date
    from hyprial.identity.impl.org_directory import PEOPLE_DIR as PEOPLE_DIR
    from hyprial.identity.impl.agents.actor.ports import DestroyAgentCommand as DestroyAgentCommand
    from hyprial.identity.impl.provider_auth.helper import DeviceLoginRunner as DeviceLoginRunner
    from hyprial.identity.impl.device import DeviceRecord as DeviceRecord
    from hyprial.identity.impl.org_directory import DirectoryDevice as DirectoryDevice
    from hyprial.identity.impl.org_directory import DirectoryStore as DirectoryStore
    from hyprial.identity.impl.org_directory import directory_device_path as directory_device_path
    from hyprial.identity.impl.org_directory import directory_binding_path as directory_binding_path
    from hyprial.identity.impl.org_directory import directory_owner_principal as directory_owner_principal
    from hyprial.identity.impl.org_directory import ProtectedDocIdTooLongError as ProtectedDocIdTooLongError
    from hyprial.identity.impl.org_directory import parse_protected_directory_node_id as parse_protected_directory_node_id
    from hyprial.identity.impl.org_directory import parse_protected_directory_doc_id as parse_protected_directory_doc_id
    from hyprial.identity.impl.org_directory import protected_directory_node_id as protected_directory_node_id
    from hyprial.identity.impl.org_directory import protected_directory_doc_id as protected_directory_doc_id
    from hyprial.identity.impl.org_directory import protected_directory_author_allowed as protected_directory_author_allowed
    from hyprial.identity.impl.agents.runtime.enforcement import EnforcementError as EnforcementError
    from hyprial.identity.impl.agents.runtime.enforcement import GRANTABLE_CAPABILITIES as GRANTABLE_CAPABILITIES
    from hyprial.identity.impl.agents.actor.ports import GrantAgentCapabilityCommand as GrantAgentCapabilityCommand
    from hyprial.identity.impl.agents.actor.ports import GrantAgentSecretCommand as GrantAgentSecretCommand
    from hyprial.identity.impl.agents.runtime.visibility import GrantRecord as GrantRecord
    from hyprial.identity.impl.agents.registry._base import HandoverNotice as HandoverNotice
    from hyprial.identity.impl.agents.home.provisioner import HomePayloadFile as HomePayloadFile
    from hyprial.identity.impl.agents.home.effects import HomeRuntimePreparer as HomeRuntimePreparer
    from hyprial.identity.impl.identity_transaction import IDENTITY_TRANSACTION_FD_ENV as IDENTITY_TRANSACTION_FD_ENV
    from hyprial.identity.impl.org_directory import INVITES_DIR as INVITES_DIR
    from hyprial.identity.impl.org_directory import INVITE_SCHEME as INVITE_SCHEME
    from hyprial.identity.impl.identity_transaction import IdentityTransactionBusy as IdentityTransactionBusy
    from hyprial.identity.impl.identity_transaction import IdentityTransactionLock as IdentityTransactionLock
    from hyprial.identity.impl.org_directory import InviteError as InviteError
    from hyprial.identity.impl.org_directory import InviteLink as InviteLink
    from hyprial.identity.impl.org_directory import LEAVES_DIR as LEAVES_DIR
    from hyprial.identity.impl.users.store._lazy import LazyUserStore as LazyUserStore
    from hyprial.identity.impl.agents.migration.entry import MigrationAuthorizationWindow as MigrationAuthorizationWindow
    from hyprial.identity.impl.agents.migration.entry import MigrationPreflightManifest as MigrationPreflightManifest
    from hyprial.identity.impl.users.profile import NotificationRule as NotificationRule
    from hyprial.identity.impl.org_directory import ORG_META_DOC as ORG_META_DOC
    from hyprial.identity.impl.org_directory import ORG_SPACE_PREFIX as ORG_SPACE_PREFIX
    from hyprial.identity.impl.org_directory import OrgPolicy as OrgPolicy
    from hyprial.identity.impl.users.profile import OwnerOpenId as OwnerOpenId
    from hyprial.identity.impl.pac_errors import PAC_EDGE_EXISTS as PAC_EDGE_EXISTS
    from hyprial.identity.impl.pac_errors import PAC_EDGE_INVALID as PAC_EDGE_INVALID
    from hyprial.identity.impl.pac_errors import PAC_EXPANSION_ALREADY_BOUND as PAC_EXPANSION_ALREADY_BOUND
    from hyprial.identity.impl.pac_errors import PAC_EXPANSION_INVALID as PAC_EXPANSION_INVALID
    from hyprial.identity.impl.pac_errors import PAC_EXPANSION_POLICY_UNAVAILABLE as PAC_EXPANSION_POLICY_UNAVAILABLE
    from hyprial.identity.impl.pac_errors import PAC_EXPANSION_SYSTEM_NODE as PAC_EXPANSION_SYSTEM_NODE
    from hyprial.identity.impl.pac_errors import PAC_EVENTS_RESYNC as PAC_EVENTS_RESYNC
    from hyprial.identity.impl.pac_errors import PAC_EVENT_ENVELOPE_INVALID as PAC_EVENT_ENVELOPE_INVALID
    from hyprial.identity.impl.pac_errors import PAC_EVENT_TYPE_UNKNOWN as PAC_EVENT_TYPE_UNKNOWN
    from hyprial.identity.impl.pac_errors import PAC_FLAG_ALREADY_SET as PAC_FLAG_ALREADY_SET
    from hyprial.identity.impl.pac_errors import PAC_FLAG_NOT_OWNER as PAC_FLAG_NOT_OWNER
    from hyprial.identity.impl.pac_errors import PAC_FLAG_NOT_SET as PAC_FLAG_NOT_SET
    from hyprial.identity.impl.pac_errors import PAC_GRAPH_CLOSED as PAC_GRAPH_CLOSED
    from hyprial.identity.impl.pac_errors import PAC_GRAPH_EXISTS as PAC_GRAPH_EXISTS
    from hyprial.identity.impl.pac_errors import PAC_GRAPH_FORWARD_CYCLE as PAC_GRAPH_FORWARD_CYCLE
    from hyprial.identity.impl.pac_errors import PAC_GRAPH_FROZEN as PAC_GRAPH_FROZEN
    from hyprial.identity.impl.pac_errors import PAC_GRAPH_NOT_ACTIVE as PAC_GRAPH_NOT_ACTIVE
    from hyprial.identity.impl.pac_errors import PAC_GRAPH_NOT_FOUND as PAC_GRAPH_NOT_FOUND
    from hyprial.identity.impl.pac_errors import PAC_GRAPH_NOT_OWNER as PAC_GRAPH_NOT_OWNER
    from hyprial.identity.impl.pac_errors import PAC_GRAPH_VERSION_CONFLICT as PAC_GRAPH_VERSION_CONFLICT
    from hyprial.identity.impl.pac_errors import PAC_MIGRATION_SOURCE_UNREADABLE as PAC_MIGRATION_SOURCE_UNREADABLE
    from hyprial.identity.impl.pac_errors import PAC_NODE_EXISTS as PAC_NODE_EXISTS
    from hyprial.identity.impl.pac_errors import PAC_NODE_NOT_FOUND as PAC_NODE_NOT_FOUND
    from hyprial.identity.impl.pac_errors import PAC_NODE_SHAPE_INVALID as PAC_NODE_SHAPE_INVALID
    from hyprial.identity.impl.pac_errors import PAC_NOTIFY_DELIVERY_FAILED as PAC_NOTIFY_DELIVERY_FAILED
    from hyprial.identity.impl.pac_errors import PAC_OPERATION_KEY_CONFLICT as PAC_OPERATION_KEY_CONFLICT
    from hyprial.identity.impl.pac_errors import PAC_OWNER_AMBIGUOUS as PAC_OWNER_AMBIGUOUS
    from hyprial.identity.impl.pac_errors import PAC_OWNER_UNKNOWN as PAC_OWNER_UNKNOWN
    from hyprial.identity.impl.pac_errors import PAC_OWNER_UNRESOLVED as PAC_OWNER_UNRESOLVED
    from hyprial.identity.impl.pac_errors import PAC_PRINCIPAL_SHAPE_INVALID as PAC_PRINCIPAL_SHAPE_INVALID
    from hyprial.identity.impl.pac_errors import PAC_PRINCIPAL_UNVERIFIED as PAC_PRINCIPAL_UNVERIFIED
    from hyprial.identity.impl.pac_errors import PAC_RESOLUTION_UNAVAILABLE as PAC_RESOLUTION_UNAVAILABLE
    from hyprial.identity.impl.pac_errors import PAC_WORKTREE_CLEANUP_FAILED as PAC_WORKTREE_CLEANUP_FAILED
    from hyprial.identity.impl.pac_errors import PAC_WORKTREE_IDENTITY_MISMATCH as PAC_WORKTREE_IDENTITY_MISMATCH
    from hyprial.identity.impl.pac_errors import PAC_WORKTREE_PREPARE_FAILED as PAC_WORKTREE_PREPARE_FAILED
    from hyprial.identity.impl.pac_errors import PacError as PacError
    from hyprial.identity.impl.agents.actor.ports import PinAgentAdapterCommand as PinAgentAdapterCommand
    from hyprial.identity.impl.agents.registry._base import PinConflictError as PinConflictError
    from hyprial.identity.impl.users.profile import PreferredReceiver as PreferredReceiver
    from hyprial.identity.impl.agents.runtime.enforcement import Principal as Principal
    from hyprial.identity.impl.provider_auth.actor._authority import ProviderAuthAuthority as ProviderAuthAuthority
    from hyprial.identity.impl.org_directory import RESOURCE_DIRECTORY_ALL as RESOURCE_DIRECTORY_ALL
    from hyprial.identity.impl.org_directory import RESOURCE_ORG_MEMBERS as RESOURCE_ORG_MEMBERS
    from hyprial.identity.impl.org_directory import RESOURCE_ORG_SELF as RESOURCE_ORG_SELF
    from hyprial.identity.impl.org_directory import ROLE_ADMIN as ROLE_ADMIN
    from hyprial.identity.impl.org_directory import ROLE_MEMBER as ROLE_MEMBER
    from hyprial.identity.impl.org_directory import ROLE_OWNER as ROLE_OWNER
    from hyprial.identity.impl.agents.state.liveness import RUNTIME_HEADLESS as RUNTIME_HEADLESS
    from hyprial.identity.impl.agents.state.liveness import RUNTIME_INTERACTIVE as RUNTIME_INTERACTIVE
    from hyprial.identity.impl.agents.actor.ports import RecordAgentActivityCommand as RecordAgentActivityCommand
    from hyprial.identity.impl.agents.actor.ports import RecordAgentSessionRefCommand as RecordAgentSessionRefCommand
    from hyprial.identity.impl.agents.actor.ports import ReleaseAgentCommand as ReleaseAgentCommand
    from hyprial.identity.impl.agents.actor.ports import ReleaseAgentDestroyReservationCommand as ReleaseAgentDestroyReservationCommand
    from hyprial.identity.impl.agents.actor.ports import ReleaseAgentRuntimeLaunchCommand as ReleaseAgentRuntimeLaunchCommand
    from hyprial.identity.impl.agents.actor.ports import ReserveAgentDestroyCommand as ReserveAgentDestroyCommand
    from hyprial.identity.impl.users.store._base import ResolvedUser as ResolvedUser
    from hyprial.identity.impl.agents.state.worker_state import RestorePolicyAuthority as RestorePolicyAuthority
    from hyprial.identity.impl.agents.state.worker_state import RestorePolicyError as RestorePolicyError
    from hyprial.identity.impl.agents.actor.ports import RetireAgentLifecycleReceiptCommand as RetireAgentLifecycleReceiptCommand
    from hyprial.identity.impl.agents.actor.ports import RetireAgentSessionRefsCommand as RetireAgentSessionRefsCommand
    from hyprial.identity.impl.agents.actor.ports import RollbackRetiredAgentSessionRefsCommand as RollbackRetiredAgentSessionRefsCommand
    from hyprial.identity.impl.agents.actor.ports import RevokeAgentCapabilityCommand as RevokeAgentCapabilityCommand
    from hyprial.identity.impl.agents.actor.ports import RevokeAgentSecretCommand as RevokeAgentSecretCommand
    from hyprial.identity.impl.users.profile import RuntimeCapability as RuntimeCapability
    from hyprial.identity.impl.agents.home.effects import RuntimeHomePreparers as RuntimeHomePreparers
    from hyprial.identity.impl.agents.runtime.visibility import SEE_ACTORS as SEE_ACTORS
    from hyprial.identity.impl.agents.runtime.visibility import SEND_TO as SEND_TO
    from hyprial.identity.impl.agents.runtime.secrets import SecretResolver as SecretResolver
    from hyprial.identity.impl.agents.runtime.secrets import SecretResolutionError as SecretResolutionError
    from hyprial.identity.impl.agents.runtime.secrets import SecretSource as SecretSource
    from hyprial.identity.impl.agents.actor.ports import SetRestoreDispositionCommand as SetRestoreDispositionCommand
    from hyprial.identity.impl.agents.actor.ports import SettleAgentDestroyCommand as SettleAgentDestroyCommand
    from hyprial.identity.impl.agents.runtime.context import SharedCredentialBinding as SharedCredentialBinding
    from hyprial.identity.impl.users.store._base import USER_NOT_FOUND as USER_NOT_FOUND
    from hyprial.identity.impl.agents.actor.ports import UnblockAgentCommand as UnblockAgentCommand
    from hyprial.identity.impl.agents.actor.ports import UnpinAgentAdapterCommand as UnpinAgentAdapterCommand
    from hyprial.identity.impl.agents.actor.ports import UpdateAgentCommand as UpdateAgentCommand
    from hyprial.identity.impl.users.home import UserHomeError as UserHomeError
    from hyprial.identity.impl.users.profile import UserProfile as UserProfile
    from hyprial.identity.impl.users.profile import UserProfileError as UserProfileError
    from hyprial.identity.impl.users.profile import UserProfileStore as UserProfileStore
    from hyprial.identity.impl.users.store._store import UserStore as UserStore
    from hyprial.identity.impl.users.store._base import UserStoreError as UserStoreError
    from hyprial.identity.impl.agents.runtime.visibility import VisibilityError as VisibilityError
    from hyprial.identity.impl.agents.state.worker_proxy import WORKER_PROXY_FIELDS as WORKER_PROXY_FIELDS
    from hyprial.identity.impl.agents.state.worker_proxy import WORKER_PROXY_SETTINGS_KEY as WORKER_PROXY_SETTINGS_KEY
    from hyprial.identity.impl.pac_errors import WORKFLOW_REMOTE_OWNER_UNSUPPORTED as WORKFLOW_REMOTE_OWNER_UNSUPPORTED
    from hyprial.identity.impl.pac_errors import WORKFLOW_WORKER_RECEIPT_MISMATCH as WORKFLOW_WORKER_RECEIPT_MISMATCH
    from hyprial.identity.impl.agents.state.worker_proxy import WorkerProxyError as WorkerProxyError
    from hyprial.identity.impl.device import address_path as address_path
    from hyprial.identity.impl.agents.runtime.context import agent_home_mode as agent_home_mode
    from hyprial.identity.impl.agents.state.worker_proxy import ambient_proxy_absent as ambient_proxy_absent
    from hyprial.identity.impl.agents.home.environment import apply_runtime_environment_profile as apply_runtime_environment_profile
    from hyprial.identity.impl.agents.home.environment import resolve_agent_secrets as resolve_agent_secrets
    from hyprial.identity.impl.org_directory import bootstrap_yaml as bootstrap_yaml
    from hyprial.identity.impl.agents.runtime.enforcement import check_channel as check_channel
    from hyprial.identity.impl.agents.runtime.enforcement import check_org_context as check_org_context
    from hyprial.identity.impl.agents.runtime.enforcement import check_shared_path as check_shared_path
    from hyprial.identity.impl.agents.home.environment import compose_worker_child_launch as compose_worker_child_launch
    from hyprial.identity.impl.org_directory import decode_invite as decode_invite
    from hyprial.identity.impl.org_directory import default_bootstrap as default_bootstrap
    from hyprial.identity.impl.org_directory import default_role_grants_document as default_role_grants_document
    from hyprial.identity.impl.agents.home.environment import derived_proxy_environment as derived_proxy_environment
    from hyprial.identity.impl.agents.state.worker_state import desired_generation as desired_generation
    from hyprial.identity.impl.device import device_key_path as device_key_path
    from hyprial.identity.impl.device import device_record_path as device_record_path
    from hyprial.identity.impl.org_directory import encode_invite as encode_invite
    from hyprial.identity.impl.agents.runtime.enforcement import explain as explain
    from hyprial.identity.impl.agents.runtime.visibility import explain_target as explain_target
    from hyprial.identity.impl.identity_slug import identity_slug as identity_slug
    from hyprial.identity.impl.org_directory import invite_deep_link as invite_deep_link
    from hyprial.identity.impl.org_directory import invite_https_url as invite_https_url
    from hyprial.identity.impl.org_directory import is_org_acl_space as is_org_acl_space
    from hyprial.identity.impl.org_directory import is_org_directory_space as is_org_directory_space
    from hyprial.identity.impl.agents.state.worker_proxy import launch_worker_proxy_route as launch_worker_proxy_route
    from hyprial.identity.impl.agents.migration.entry import load_packaged_support_matrix as load_packaged_support_matrix
    from hyprial.identity.impl.agents.runtime.visibility import may_send as may_send
    from hyprial.identity.impl.agents.registry._base import normalize_capabilities as normalize_capabilities
    from hyprial.identity.impl.agents.registry._base import normalize_harness_args as normalize_harness_args
    from hyprial.identity.impl.agents.runtime.capabilities import option_value as option_value
    from hyprial.identity.impl.org_directory import org_from_space_name as org_from_space_name
    from hyprial.identity.impl.org_directory import org_space_name as org_space_name
    from hyprial.identity.impl.org_directory import parse_bootstrap_yaml as parse_bootstrap_yaml
    from hyprial.identity.impl.org_directory import role_grants_from_document as role_grants_from_document
    from hyprial.identity.impl.principal import parse_principal as parse_principal
    from hyprial.identity.impl.principal import principal_kind as principal_kind
    from hyprial.identity.impl.principal import principal_matches_local_actor as principal_matches_local_actor
    from hyprial.identity.impl.device import read_device_record as read_device_record
    from hyprial.identity.impl.agents.state.worker_proxy import read_worker_proxy as read_worker_proxy
    from hyprial.identity.impl.agents.runtime.capabilities import runtime_capabilities as runtime_capabilities
    from hyprial.identity.impl.agents.runtime.context import shared_credential_status as shared_credential_status
    from hyprial.identity.impl.agents.home.provisioner import snapshot_home_payload as snapshot_home_payload
    from hyprial.identity.impl.agents.runtime.context import validate_shared_credential_binding as validate_shared_credential_binding
    from hyprial.identity.impl.agents.runtime.context import validate_shared_credential_environment as validate_shared_credential_environment
    from hyprial.identity.impl.agents.home.config import verify_native_projection as verify_native_projection
    from hyprial.identity.impl.agents.runtime.visibility import visible_targets as visible_targets
    from hyprial.identity.impl.agents.home.environment import whitelist_replacement_environment as whitelist_replacement_environment
    from hyprial.identity.impl.device import write_device_record as write_device_record
    from hyprial.identity.impl.agents.state.worker_proxy import write_worker_proxy_field as write_worker_proxy_field

_FACADE_EXPORTS = {
    'ACTION_INVITE': ('hyprial.identity.impl.org_directory', 'ACTION_INVITE'),
    'ACTION_LEAVE': ('hyprial.identity.impl.org_directory', 'ACTION_LEAVE'),
    'ACTION_READ': ('hyprial.identity.impl.org_directory', 'ACTION_READ'),
    'ACTION_REMOVE': ('hyprial.identity.impl.org_directory', 'ACTION_REMOVE'),
    'ACTION_WRITE': ('hyprial.identity.impl.org_directory', 'ACTION_WRITE'),
    'ADDRESS_RELPATH': ('hyprial.identity.impl.device', 'ADDRESS_RELPATH'),
    'AGENT_HEARTBEAT_TTL_SECONDS': ('hyprial.identity.impl.agents.state.liveness', 'AGENT_HEARTBEAT_TTL_SECONDS'),
    'AcquireAgentRuntimeLaunchCommand': ('hyprial.identity.impl.agents.actor.ports', 'AcquireAgentRuntimeLaunchCommand'),
    'Agent': ('hyprial.identity.impl.agents.registry._base', 'Agent'),
    'AgentActor': ('hyprial.identity.impl.agents.actor._core', 'AgentActor'),
    'AgentAlreadyRunning': ('hyprial.identity.impl.agents.state.liveness', 'AgentAlreadyRunning'),
    'AgentBinding': ('hyprial.identity.impl.agents.state.liveness', 'AgentBinding'),
    'AgentCapabilityGrantCompleted': ('hyprial.identity.impl.agents.actor.ports', 'AgentCapabilityGrantCompleted'),
    'AgentCommand': ('hyprial.identity.impl.agents.actor.ports', 'AgentCommand'),
    'AgentConfig': ('hyprial.identity.impl.agents.home.config', 'AgentConfig'),
    'AgentConfigError': ('hyprial.identity.impl.agents.home.config', 'AgentConfigError'),
    'AgentDestroyReservationCompleted': ('hyprial.identity.impl.agents.actor.ports', 'AgentDestroyReservationCompleted'),
    'AgentDestroySettled': ('hyprial.identity.impl.agents.actor.ports', 'AgentDestroySettled'),
    'AgentError': ('hyprial.identity.impl.agents.registry._base', 'AgentError'),
    'AgentEvent': ('hyprial.identity.impl.agents.actor.ports', 'AgentEvent'),
    'AgentHomeError': ('hyprial.identity.impl.agents.home.provisioner', 'AgentHomeError'),
    'AgentKeepListAuthority': ('hyprial.identity.impl.agents.state.keep_actor', 'AgentKeepListAuthority'),
    'AgentKeepListError': ('hyprial.identity.impl.agents.state.activity', 'AgentKeepListError'),
    'AgentLifecycleReceiptCompleted': ('hyprial.identity.impl.agents.actor.ports', 'AgentLifecycleReceiptCompleted'),
    'AgentMigrationCoordinator': ('hyprial.identity.impl.agents.migration.coordinator', 'AgentMigrationCoordinator'),
    'AgentMutationCompleted': ('hyprial.identity.impl.agents.actor.ports', 'AgentMutationCompleted'),
    'AgentProjection': ('hyprial.identity.impl.agents.actor.ports', 'AgentProjection'),
    'AgentRegistry': ('hyprial.identity.impl.agents.registry._core', 'AgentRegistry'),
    'AgentRuntimeContext': ('hyprial.identity.impl.agents.runtime.context', 'AgentRuntimeContext'),
    'AgentRuntimeError': ('hyprial.identity.impl.agents.runtime.context', 'AgentRuntimeError'),
    'AgentRuntimeLaunchLeaseCompleted': ('hyprial.identity.impl.agents.actor.ports', 'AgentRuntimeLaunchLeaseCompleted'),
    'AgentRuntimeMigrationBindings': ('hyprial.identity.impl.agents.migration._bindings', 'AgentRuntimeMigrationBindings'),
    'AgentSecretGrantCompleted': ('hyprial.identity.impl.agents.actor.ports', 'AgentSecretGrantCompleted'),
    'AllowAllPolicy': ('hyprial.identity.impl.org_directory', 'AllowAllPolicy'),
    'Ambiguous': ('hyprial.identity.impl.users.store._base', 'Ambiguous'),
    'BOOTSTRAP_STEP_KINDS': ('hyprial.identity.impl.org_directory', 'BOOTSTRAP_STEP_KINDS'),
    'BindAgentCommand': ('hyprial.identity.impl.agents.actor.ports', 'BindAgentCommand'),
    'BlockAgentCommand': ('hyprial.identity.impl.agents.actor.ports', 'BlockAgentCommand'),
    'BlockingFailureAuthority': ('hyprial.identity.impl.agents.state.worker_state', 'BlockingFailureAuthority'),
    'BootstrapStep': ('hyprial.identity.impl.org_directory', 'BootstrapStep'),
    'CAPABILITY_ENTRY_POINTS': ('hyprial.identity.impl.agents.runtime.enforcement', 'CAPABILITY_ENTRY_POINTS'),
    'CallerIdentity': ('hyprial.identity.impl.agents.runtime.visibility', 'CallerIdentity'),
    'CapabilityRecord': ('hyprial.identity.impl.agents.runtime.enforcement', 'CapabilityRecord'),
    'CasbinOrgPolicy': ('hyprial.identity.impl.org_directory', 'CasbinOrgPolicy'),
    'ChildEnvironmentLaunch': ('hyprial.identity.impl.agents.home.environment', 'ChildEnvironmentLaunch'),
    'CleanupRevokedAgentHomeCommand': ('hyprial.identity.impl.agents.actor.ports', 'CleanupRevokedAgentHomeCommand'),
    'ClearRestoreDispositionCommand': ('hyprial.identity.impl.agents.actor.ports', 'ClearRestoreDispositionCommand'),
    'ConfigProjectionReceipt': ('hyprial.identity.impl.agents.home.config', 'ConfigProjectionReceipt'),
    'ConfirmAgentLifecycleReceiptCommand': ('hyprial.identity.impl.agents.actor.ports', 'ConfirmAgentLifecycleReceiptCommand'),
    'CreateAgentCommand': ('hyprial.identity.impl.agents.actor.ports', 'CreateAgentCommand'),
    'CreateHostInvitedAgentCommand': ('hyprial.identity.impl.agents.actor.ports', 'CreateHostInvitedAgentCommand'),
    'CreateTransferHostedAgentCommand': ('hyprial.identity.impl.agents.actor.ports', 'CreateTransferHostedAgentCommand'),
    'DEFAULT_AGENT_TOOL_PROFILE': ('hyprial.identity.impl.agents.runtime.context', 'DEFAULT_AGENT_TOOL_PROFILE'),
    'DEVICE_KEY_RELPATH': ('hyprial.identity.impl.device', 'DEVICE_KEY_RELPATH'),
    'DEVICE_RECORD_RELPATH': ('hyprial.identity.impl.device', 'DEVICE_RECORD_RELPATH'),
    'DIRECTORY_DIR': ('hyprial.identity.impl.org_directory', 'DIRECTORY_DIR'),
    'BINDING_ASSERTION_CLIENT_ID': ('hyprial.identity.impl.org_directory', 'BINDING_ASSERTION_CLIENT_ID'),
    'BINDING_ASSERTION_OWNER': ('hyprial.identity.impl.org_directory', 'BINDING_ASSERTION_OWNER'),
    'binding_assertion_claims_unverified': ('hyprial.identity.impl.org_directory', 'binding_assertion_claims_unverified'),
    'binding_assertion_publish_after': ('hyprial.identity.impl.org_directory', 'binding_assertion_publish_after'),
    'binding_numeric_date': ('hyprial.identity.impl.org_directory', 'binding_numeric_date'),
    'PEOPLE_DIR': ('hyprial.identity.impl.org_directory', 'PEOPLE_DIR'),
    'DestroyAgentCommand': ('hyprial.identity.impl.agents.actor.ports', 'DestroyAgentCommand'),
    'DeviceLoginRunner': ('hyprial.identity.impl.provider_auth.helper', 'DeviceLoginRunner'),
    'DeviceRecord': ('hyprial.identity.impl.device', 'DeviceRecord'),
    'DirectoryDevice': ('hyprial.identity.impl.org_directory', 'DirectoryDevice'),
    'DirectoryStore': ('hyprial.identity.impl.org_directory', 'DirectoryStore'),
    'directory_device_path': ('hyprial.identity.impl.org_directory', 'directory_device_path'),
    'directory_binding_path': ('hyprial.identity.impl.org_directory', 'directory_binding_path'),
    'directory_owner_principal': ('hyprial.identity.impl.org_directory', 'directory_owner_principal'),
    'ProtectedDocIdTooLongError': ('hyprial.identity.impl.org_directory', 'ProtectedDocIdTooLongError'),
    'parse_protected_directory_node_id': ('hyprial.identity.impl.org_directory', 'parse_protected_directory_node_id'),
    'parse_protected_directory_doc_id': ('hyprial.identity.impl.org_directory', 'parse_protected_directory_doc_id'),
    'protected_directory_node_id': ('hyprial.identity.impl.org_directory', 'protected_directory_node_id'),
    'protected_directory_doc_id': ('hyprial.identity.impl.org_directory', 'protected_directory_doc_id'),
    'protected_directory_author_allowed': ('hyprial.identity.impl.org_directory', 'protected_directory_author_allowed'),
    'EnforcementError': ('hyprial.identity.impl.agents.runtime.enforcement', 'EnforcementError'),
    'GRANTABLE_CAPABILITIES': ('hyprial.identity.impl.agents.runtime.enforcement', 'GRANTABLE_CAPABILITIES'),
    'GrantAgentCapabilityCommand': ('hyprial.identity.impl.agents.actor.ports', 'GrantAgentCapabilityCommand'),
    'GrantAgentSecretCommand': ('hyprial.identity.impl.agents.actor.ports', 'GrantAgentSecretCommand'),
    'GrantRecord': ('hyprial.identity.impl.agents.runtime.visibility', 'GrantRecord'),
    'HandoverNotice': ('hyprial.identity.impl.agents.registry._base', 'HandoverNotice'),
    'HomePayloadFile': ('hyprial.identity.impl.agents.home.provisioner', 'HomePayloadFile'),
    'HomeRuntimePreparer': ('hyprial.identity.impl.agents.home.effects', 'HomeRuntimePreparer'),
    'IDENTITY_TRANSACTION_FD_ENV': ('hyprial.identity.impl.identity_transaction', 'IDENTITY_TRANSACTION_FD_ENV'),
    'INVITES_DIR': ('hyprial.identity.impl.org_directory', 'INVITES_DIR'),
    'INVITE_SCHEME': ('hyprial.identity.impl.org_directory', 'INVITE_SCHEME'),
    'IdentityTransactionBusy': ('hyprial.identity.impl.identity_transaction', 'IdentityTransactionBusy'),
    'IdentityTransactionLock': ('hyprial.identity.impl.identity_transaction', 'IdentityTransactionLock'),
    'InviteError': ('hyprial.identity.impl.org_directory', 'InviteError'),
    'InviteLink': ('hyprial.identity.impl.org_directory', 'InviteLink'),
    'LEAVES_DIR': ('hyprial.identity.impl.org_directory', 'LEAVES_DIR'),
    'LazyUserStore': ('hyprial.identity.impl.users.store._lazy', 'LazyUserStore'),
    'MigrationAuthorizationWindow': ('hyprial.identity.impl.agents.migration.entry', 'MigrationAuthorizationWindow'),
    'MigrationPreflightManifest': ('hyprial.identity.impl.agents.migration.entry', 'MigrationPreflightManifest'),
    'NotificationRule': ('hyprial.identity.impl.users.profile', 'NotificationRule'),
    'ORG_META_DOC': ('hyprial.identity.impl.org_directory', 'ORG_META_DOC'),
    'ORG_SPACE_PREFIX': ('hyprial.identity.impl.org_directory', 'ORG_SPACE_PREFIX'),
    'OrgPolicy': ('hyprial.identity.impl.org_directory', 'OrgPolicy'),
    'OwnerOpenId': ('hyprial.identity.impl.users.profile', 'OwnerOpenId'),
    'PAC_EDGE_EXISTS': ('hyprial.identity.impl.pac_errors', 'PAC_EDGE_EXISTS'),
    'PAC_EDGE_INVALID': ('hyprial.identity.impl.pac_errors', 'PAC_EDGE_INVALID'),
    'PAC_EXPANSION_ALREADY_BOUND': ('hyprial.identity.impl.pac_errors', 'PAC_EXPANSION_ALREADY_BOUND'),
    'PAC_EXPANSION_INVALID': ('hyprial.identity.impl.pac_errors', 'PAC_EXPANSION_INVALID'),
    'PAC_EXPANSION_POLICY_UNAVAILABLE': ('hyprial.identity.impl.pac_errors', 'PAC_EXPANSION_POLICY_UNAVAILABLE'),
    'PAC_EXPANSION_SYSTEM_NODE': ('hyprial.identity.impl.pac_errors', 'PAC_EXPANSION_SYSTEM_NODE'),
    'PAC_EVENTS_RESYNC': ('hyprial.identity.impl.pac_errors', 'PAC_EVENTS_RESYNC'),
    'PAC_EVENT_ENVELOPE_INVALID': ('hyprial.identity.impl.pac_errors', 'PAC_EVENT_ENVELOPE_INVALID'),
    'PAC_EVENT_TYPE_UNKNOWN': ('hyprial.identity.impl.pac_errors', 'PAC_EVENT_TYPE_UNKNOWN'),
    'PAC_FLAG_ALREADY_SET': ('hyprial.identity.impl.pac_errors', 'PAC_FLAG_ALREADY_SET'),
    'PAC_FLAG_NOT_OWNER': ('hyprial.identity.impl.pac_errors', 'PAC_FLAG_NOT_OWNER'),
    'PAC_FLAG_NOT_SET': ('hyprial.identity.impl.pac_errors', 'PAC_FLAG_NOT_SET'),
    'PAC_GRAPH_CLOSED': ('hyprial.identity.impl.pac_errors', 'PAC_GRAPH_CLOSED'),
    'PAC_GRAPH_EXISTS': ('hyprial.identity.impl.pac_errors', 'PAC_GRAPH_EXISTS'),
    'PAC_GRAPH_FORWARD_CYCLE': ('hyprial.identity.impl.pac_errors', 'PAC_GRAPH_FORWARD_CYCLE'),
    'PAC_GRAPH_FROZEN': ('hyprial.identity.impl.pac_errors', 'PAC_GRAPH_FROZEN'),
    'PAC_GRAPH_NOT_ACTIVE': ('hyprial.identity.impl.pac_errors', 'PAC_GRAPH_NOT_ACTIVE'),
    'PAC_GRAPH_NOT_FOUND': ('hyprial.identity.impl.pac_errors', 'PAC_GRAPH_NOT_FOUND'),
    'PAC_GRAPH_NOT_OWNER': ('hyprial.identity.impl.pac_errors', 'PAC_GRAPH_NOT_OWNER'),
    'PAC_GRAPH_VERSION_CONFLICT': ('hyprial.identity.impl.pac_errors', 'PAC_GRAPH_VERSION_CONFLICT'),
    'PAC_MIGRATION_SOURCE_UNREADABLE': ('hyprial.identity.impl.pac_errors', 'PAC_MIGRATION_SOURCE_UNREADABLE'),
    'PAC_NODE_EXISTS': ('hyprial.identity.impl.pac_errors', 'PAC_NODE_EXISTS'),
    'PAC_NODE_NOT_FOUND': ('hyprial.identity.impl.pac_errors', 'PAC_NODE_NOT_FOUND'),
    'PAC_NODE_SHAPE_INVALID': ('hyprial.identity.impl.pac_errors', 'PAC_NODE_SHAPE_INVALID'),
    'PAC_NOTIFY_DELIVERY_FAILED': ('hyprial.identity.impl.pac_errors', 'PAC_NOTIFY_DELIVERY_FAILED'),
    'PAC_OPERATION_KEY_CONFLICT': ('hyprial.identity.impl.pac_errors', 'PAC_OPERATION_KEY_CONFLICT'),
    'PAC_OWNER_AMBIGUOUS': ('hyprial.identity.impl.pac_errors', 'PAC_OWNER_AMBIGUOUS'),
    'PAC_OWNER_UNKNOWN': ('hyprial.identity.impl.pac_errors', 'PAC_OWNER_UNKNOWN'),
    'PAC_OWNER_UNRESOLVED': ('hyprial.identity.impl.pac_errors', 'PAC_OWNER_UNRESOLVED'),
    'PAC_PRINCIPAL_SHAPE_INVALID': ('hyprial.identity.impl.pac_errors', 'PAC_PRINCIPAL_SHAPE_INVALID'),
    'PAC_PRINCIPAL_UNVERIFIED': ('hyprial.identity.impl.pac_errors', 'PAC_PRINCIPAL_UNVERIFIED'),
    'PAC_RESOLUTION_UNAVAILABLE': ('hyprial.identity.impl.pac_errors', 'PAC_RESOLUTION_UNAVAILABLE'),
    'PAC_WORKTREE_CLEANUP_FAILED': ('hyprial.identity.impl.pac_errors', 'PAC_WORKTREE_CLEANUP_FAILED'),
    'PAC_WORKTREE_IDENTITY_MISMATCH': ('hyprial.identity.impl.pac_errors', 'PAC_WORKTREE_IDENTITY_MISMATCH'),
    'PAC_WORKTREE_PREPARE_FAILED': ('hyprial.identity.impl.pac_errors', 'PAC_WORKTREE_PREPARE_FAILED'),
    'PacError': ('hyprial.identity.impl.pac_errors', 'PacError'),
    'PinAgentAdapterCommand': ('hyprial.identity.impl.agents.actor.ports', 'PinAgentAdapterCommand'),
    'PinConflictError': ('hyprial.identity.impl.agents.registry._base', 'PinConflictError'),
    'PreferredReceiver': ('hyprial.identity.impl.users.profile', 'PreferredReceiver'),
    'Principal': ('hyprial.identity.impl.agents.runtime.enforcement', 'Principal'),
    'ProviderAuthAuthority': ('hyprial.identity.impl.provider_auth.actor._authority', 'ProviderAuthAuthority'),
    'RESOURCE_DIRECTORY_ALL': ('hyprial.identity.impl.org_directory', 'RESOURCE_DIRECTORY_ALL'),
    'RESOURCE_ORG_MEMBERS': ('hyprial.identity.impl.org_directory', 'RESOURCE_ORG_MEMBERS'),
    'RESOURCE_ORG_SELF': ('hyprial.identity.impl.org_directory', 'RESOURCE_ORG_SELF'),
    'ROLE_ADMIN': ('hyprial.identity.impl.org_directory', 'ROLE_ADMIN'),
    'ROLE_MEMBER': ('hyprial.identity.impl.org_directory', 'ROLE_MEMBER'),
    'ROLE_OWNER': ('hyprial.identity.impl.org_directory', 'ROLE_OWNER'),
    'RUNTIME_HEADLESS': ('hyprial.identity.impl.agents.state.liveness', 'RUNTIME_HEADLESS'),
    'RUNTIME_INTERACTIVE': ('hyprial.identity.impl.agents.state.liveness', 'RUNTIME_INTERACTIVE'),
    'RecordAgentActivityCommand': ('hyprial.identity.impl.agents.actor.ports', 'RecordAgentActivityCommand'),
    'RecordAgentSessionRefCommand': ('hyprial.identity.impl.agents.actor.ports', 'RecordAgentSessionRefCommand'),
    'ReleaseAgentCommand': ('hyprial.identity.impl.agents.actor.ports', 'ReleaseAgentCommand'),
    'ReleaseAgentDestroyReservationCommand': ('hyprial.identity.impl.agents.actor.ports', 'ReleaseAgentDestroyReservationCommand'),
    'ReleaseAgentRuntimeLaunchCommand': ('hyprial.identity.impl.agents.actor.ports', 'ReleaseAgentRuntimeLaunchCommand'),
    'ReserveAgentDestroyCommand': ('hyprial.identity.impl.agents.actor.ports', 'ReserveAgentDestroyCommand'),
    'ResolvedUser': ('hyprial.identity.impl.users.store._base', 'ResolvedUser'),
    'RestorePolicyAuthority': ('hyprial.identity.impl.agents.state.worker_state', 'RestorePolicyAuthority'),
    'RestorePolicyError': ('hyprial.identity.impl.agents.state.worker_state', 'RestorePolicyError'),
    'RetireAgentLifecycleReceiptCommand': ('hyprial.identity.impl.agents.actor.ports', 'RetireAgentLifecycleReceiptCommand'),
    'RetireAgentSessionRefsCommand': ('hyprial.identity.impl.agents.actor.ports', 'RetireAgentSessionRefsCommand'),
    'RollbackRetiredAgentSessionRefsCommand': ('hyprial.identity.impl.agents.actor.ports', 'RollbackRetiredAgentSessionRefsCommand'),
    'RevokeAgentCapabilityCommand': ('hyprial.identity.impl.agents.actor.ports', 'RevokeAgentCapabilityCommand'),
    'RevokeAgentSecretCommand': ('hyprial.identity.impl.agents.actor.ports', 'RevokeAgentSecretCommand'),
    'RuntimeCapability': ('hyprial.identity.impl.users.profile', 'RuntimeCapability'),
    'RuntimeHomePreparers': ('hyprial.identity.impl.agents.home.effects', 'RuntimeHomePreparers'),
    'SEE_ACTORS': ('hyprial.identity.impl.agents.runtime.visibility', 'SEE_ACTORS'),
    'SEND_TO': ('hyprial.identity.impl.agents.runtime.visibility', 'SEND_TO'),
    'SecretResolver': ('hyprial.identity.impl.agents.runtime.secrets', 'SecretResolver'),
    'SecretResolutionError': ('hyprial.identity.impl.agents.runtime.secrets', 'SecretResolutionError'),
    'SecretSource': ('hyprial.identity.impl.agents.runtime.secrets', 'SecretSource'),
    'SetRestoreDispositionCommand': ('hyprial.identity.impl.agents.actor.ports', 'SetRestoreDispositionCommand'),
    'SettleAgentDestroyCommand': ('hyprial.identity.impl.agents.actor.ports', 'SettleAgentDestroyCommand'),
    'SharedCredentialBinding': ('hyprial.identity.impl.agents.runtime.context', 'SharedCredentialBinding'),
    'USER_NOT_FOUND': ('hyprial.identity.impl.users.store._base', 'USER_NOT_FOUND'),
    'UnblockAgentCommand': ('hyprial.identity.impl.agents.actor.ports', 'UnblockAgentCommand'),
    'UnpinAgentAdapterCommand': ('hyprial.identity.impl.agents.actor.ports', 'UnpinAgentAdapterCommand'),
    'UpdateAgentCommand': ('hyprial.identity.impl.agents.actor.ports', 'UpdateAgentCommand'),
    'UserHomeError': ('hyprial.identity.impl.users.home', 'UserHomeError'),
    'UserProfile': ('hyprial.identity.impl.users.profile', 'UserProfile'),
    'UserProfileError': ('hyprial.identity.impl.users.profile', 'UserProfileError'),
    'UserProfileStore': ('hyprial.identity.impl.users.profile', 'UserProfileStore'),
    'UserStore': ('hyprial.identity.impl.users.store._store', 'UserStore'),
    'UserStoreError': ('hyprial.identity.impl.users.store._base', 'UserStoreError'),
    'VisibilityError': ('hyprial.identity.impl.agents.runtime.visibility', 'VisibilityError'),
    'WORKER_PROXY_FIELDS': ('hyprial.identity.impl.agents.state.worker_proxy', 'WORKER_PROXY_FIELDS'),
    'WORKER_PROXY_SETTINGS_KEY': ('hyprial.identity.impl.agents.state.worker_proxy', 'WORKER_PROXY_SETTINGS_KEY'),
    'WORKFLOW_REMOTE_OWNER_UNSUPPORTED': ('hyprial.identity.impl.pac_errors', 'WORKFLOW_REMOTE_OWNER_UNSUPPORTED'),
    'WORKFLOW_WORKER_RECEIPT_MISMATCH': ('hyprial.identity.impl.pac_errors', 'WORKFLOW_WORKER_RECEIPT_MISMATCH'),
    'WorkerProxyError': ('hyprial.identity.impl.agents.state.worker_proxy', 'WorkerProxyError'),
    'address_path': ('hyprial.identity.impl.device', 'address_path'),
    'agent_home_mode': ('hyprial.identity.impl.agents.runtime.context', 'agent_home_mode'),
    'ambient_proxy_absent': ('hyprial.identity.impl.agents.state.worker_proxy', 'ambient_proxy_absent'),
    'apply_runtime_environment_profile': ('hyprial.identity.impl.agents.home.environment', 'apply_runtime_environment_profile'),
    'resolve_agent_secrets': ('hyprial.identity.impl.agents.home.environment', 'resolve_agent_secrets'),
    'bootstrap_yaml': ('hyprial.identity.impl.org_directory', 'bootstrap_yaml'),
    'check_channel': ('hyprial.identity.impl.agents.runtime.enforcement', 'check_channel'),
    'check_org_context': ('hyprial.identity.impl.agents.runtime.enforcement', 'check_org_context'),
    'check_shared_path': ('hyprial.identity.impl.agents.runtime.enforcement', 'check_shared_path'),
    'compose_worker_child_launch': ('hyprial.identity.impl.agents.home.environment', 'compose_worker_child_launch'),
    'decode_invite': ('hyprial.identity.impl.org_directory', 'decode_invite'),
    'default_bootstrap': ('hyprial.identity.impl.org_directory', 'default_bootstrap'),
    'default_role_grants_document': ('hyprial.identity.impl.org_directory', 'default_role_grants_document'),
    'derived_proxy_environment': ('hyprial.identity.impl.agents.home.environment', 'derived_proxy_environment'),
    'desired_generation': ('hyprial.identity.impl.agents.state.worker_state', 'desired_generation'),
    'device_key_path': ('hyprial.identity.impl.device', 'device_key_path'),
    'device_record_path': ('hyprial.identity.impl.device', 'device_record_path'),
    'encode_invite': ('hyprial.identity.impl.org_directory', 'encode_invite'),
    'explain': ('hyprial.identity.impl.agents.runtime.enforcement', 'explain'),
    'explain_target': ('hyprial.identity.impl.agents.runtime.visibility', 'explain_target'),
    'identity_slug': ('hyprial.identity.impl.identity_slug', 'identity_slug'),
    'invite_deep_link': ('hyprial.identity.impl.org_directory', 'invite_deep_link'),
    'invite_https_url': ('hyprial.identity.impl.org_directory', 'invite_https_url'),
    'is_org_acl_space': ('hyprial.identity.impl.org_directory', 'is_org_acl_space'),
    'is_org_directory_space': ('hyprial.identity.impl.org_directory', 'is_org_directory_space'),
    'launch_worker_proxy_route': ('hyprial.identity.impl.agents.state.worker_proxy', 'launch_worker_proxy_route'),
    'load_packaged_support_matrix': ('hyprial.identity.impl.agents.migration.entry', 'load_packaged_support_matrix'),
    'may_send': ('hyprial.identity.impl.agents.runtime.visibility', 'may_send'),
    'normalize_capabilities': ('hyprial.identity.impl.agents.registry._base', 'normalize_capabilities'),
    'normalize_harness_args': ('hyprial.identity.impl.agents.registry._base', 'normalize_harness_args'),
    'option_value': ('hyprial.identity.impl.agents.runtime.capabilities', 'option_value'),
    'org_from_space_name': ('hyprial.identity.impl.org_directory', 'org_from_space_name'),
    'org_space_name': ('hyprial.identity.impl.org_directory', 'org_space_name'),
    'parse_bootstrap_yaml': ('hyprial.identity.impl.org_directory', 'parse_bootstrap_yaml'),
    'role_grants_from_document': ('hyprial.identity.impl.org_directory', 'role_grants_from_document'),
    'parse_principal': ('hyprial.identity.impl.principal', 'parse_principal'),
    'principal_kind': ('hyprial.identity.impl.principal', 'principal_kind'),
    'principal_matches_local_actor': ('hyprial.identity.impl.principal', 'principal_matches_local_actor'),
    'read_device_record': ('hyprial.identity.impl.device', 'read_device_record'),
    'read_worker_proxy': ('hyprial.identity.impl.agents.state.worker_proxy', 'read_worker_proxy'),
    'runtime_capabilities': ('hyprial.identity.impl.agents.runtime.capabilities', 'runtime_capabilities'),
    'shared_credential_status': ('hyprial.identity.impl.agents.runtime.context', 'shared_credential_status'),
    'snapshot_home_payload': ('hyprial.identity.impl.agents.home.provisioner', 'snapshot_home_payload'),
    'validate_shared_credential_binding': ('hyprial.identity.impl.agents.runtime.context', 'validate_shared_credential_binding'),
    'validate_shared_credential_environment': ('hyprial.identity.impl.agents.runtime.context', 'validate_shared_credential_environment'),
    'verify_native_projection': ('hyprial.identity.impl.agents.home.config', 'verify_native_projection'),
    'visible_targets': ('hyprial.identity.impl.agents.runtime.visibility', 'visible_targets'),
    'whitelist_replacement_environment': ('hyprial.identity.impl.agents.home.environment', 'whitelist_replacement_environment'),
    'write_device_record': ('hyprial.identity.impl.device', 'write_device_record'),
    'write_worker_proxy_field': ('hyprial.identity.impl.agents.state.worker_proxy', 'write_worker_proxy_field'),
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
    'ACTION_INVITE',
    'ACTION_LEAVE',
    'ACTION_READ',
    'ACTION_REMOVE',
    'ACTION_WRITE',
    'ADDRESS_RELPATH',
    'AGENT_HEARTBEAT_TTL_SECONDS',
    'AcquireAgentRuntimeLaunchCommand',
    'Agent',
    'AgentActor',
    'AgentAlreadyRunning',
    'AgentBinding',
    'AgentCapabilityGrantCompleted',
    'AgentCommand',
    'AgentConfig',
    'AgentConfigError',
    'AgentDestroyReservationCompleted',
    'AgentDestroySettled',
    'AgentError',
    'AgentEvent',
    'AgentHomeError',
    'AgentKeepListAuthority',
    'AgentKeepListError',
    'AgentLifecycleReceiptCompleted',
    'AgentMigrationCoordinator',
    'AgentMutationCompleted',
    'AgentProjection',
    'AgentRegistry',
    'AgentRuntimeContext',
    'AgentRuntimeError',
    'AgentRuntimeLaunchLeaseCompleted',
    'AgentRuntimeMigrationBindings',
    'AgentSecretGrantCompleted',
    'AllowAllPolicy',
    'Ambiguous',
    'BOOTSTRAP_STEP_KINDS',
    'BindAgentCommand',
    'BlockAgentCommand',
    'BlockingFailureAuthority',
    'BootstrapStep',
    'CAPABILITY_ENTRY_POINTS',
    'CallerIdentity',
    'CapabilityRecord',
    'CasbinOrgPolicy',
    'ChildEnvironmentLaunch',
    'CleanupRevokedAgentHomeCommand',
    'ClearRestoreDispositionCommand',
    'ConfigProjectionReceipt',
    'ConfirmAgentLifecycleReceiptCommand',
    'CreateAgentCommand',
    'CreateHostInvitedAgentCommand',
    'CreateTransferHostedAgentCommand',
    'DEFAULT_AGENT_TOOL_PROFILE',
    'DEVICE_KEY_RELPATH',
    'DEVICE_RECORD_RELPATH',
    'DIRECTORY_DIR',
    'BINDING_ASSERTION_CLIENT_ID',
    'BINDING_ASSERTION_OWNER',
    'binding_assertion_claims_unverified',
    'binding_assertion_publish_after',
    'binding_numeric_date',
    'PEOPLE_DIR',
    'DestroyAgentCommand',
    'DeviceLoginRunner',
    'DeviceRecord',
    'DirectoryDevice',
    'DirectoryStore',
    'directory_device_path',
    'directory_binding_path',
    'directory_owner_principal',
    'ProtectedDocIdTooLongError',
    'parse_protected_directory_node_id',
    'parse_protected_directory_doc_id',
    'protected_directory_node_id',
    'protected_directory_doc_id',
    'protected_directory_author_allowed',
    'EnforcementError',
    'GRANTABLE_CAPABILITIES',
    'GrantAgentCapabilityCommand',
    'GrantAgentSecretCommand',
    'GrantRecord',
    'HandoverNotice',
    'HomePayloadFile',
    'HomeRuntimePreparer',
    'IDENTITY_TRANSACTION_FD_ENV',
    'INVITES_DIR',
    'INVITE_SCHEME',
    'IdentityTransactionBusy',
    'IdentityTransactionLock',
    'InviteError',
    'InviteLink',
    'LEAVES_DIR',
    'LazyUserStore',
    'MigrationAuthorizationWindow',
    'MigrationPreflightManifest',
    'NotificationRule',
    'ORG_META_DOC',
    'ORG_SPACE_PREFIX',
    'OrgPolicy',
    'OwnerOpenId',
    'PAC_EDGE_EXISTS',
    'PAC_EDGE_INVALID',
    'PAC_EXPANSION_ALREADY_BOUND',
    'PAC_EXPANSION_INVALID',
    'PAC_EXPANSION_POLICY_UNAVAILABLE',
    'PAC_EXPANSION_SYSTEM_NODE',
    'PAC_EVENTS_RESYNC',
    'PAC_EVENT_ENVELOPE_INVALID',
    'PAC_EVENT_TYPE_UNKNOWN',
    'PAC_FLAG_ALREADY_SET',
    'PAC_FLAG_NOT_OWNER',
    'PAC_FLAG_NOT_SET',
    'PAC_GRAPH_CLOSED',
    'PAC_GRAPH_EXISTS',
    'PAC_GRAPH_FORWARD_CYCLE',
    'PAC_GRAPH_FROZEN',
    'PAC_GRAPH_NOT_ACTIVE',
    'PAC_GRAPH_NOT_FOUND',
    'PAC_GRAPH_NOT_OWNER',
    'PAC_GRAPH_VERSION_CONFLICT',
    'PAC_MIGRATION_SOURCE_UNREADABLE',
    'PAC_NODE_EXISTS',
    'PAC_NODE_NOT_FOUND',
    'PAC_NODE_SHAPE_INVALID',
    'PAC_NOTIFY_DELIVERY_FAILED',
    'PAC_OPERATION_KEY_CONFLICT',
    'PAC_OWNER_AMBIGUOUS',
    'PAC_OWNER_UNKNOWN',
    'PAC_OWNER_UNRESOLVED',
    'PAC_PRINCIPAL_SHAPE_INVALID',
    'PAC_PRINCIPAL_UNVERIFIED',
    'PAC_RESOLUTION_UNAVAILABLE',
    'PAC_WORKTREE_CLEANUP_FAILED',
    'PAC_WORKTREE_IDENTITY_MISMATCH',
    'PAC_WORKTREE_PREPARE_FAILED',
    'PacError',
    'PinAgentAdapterCommand',
    'PinConflictError',
    'PreferredReceiver',
    'Principal',
    'ProviderAuthAuthority',
    'RESOURCE_DIRECTORY_ALL',
    'RESOURCE_ORG_MEMBERS',
    'RESOURCE_ORG_SELF',
    'ROLE_ADMIN',
    'ROLE_MEMBER',
    'ROLE_OWNER',
    'RUNTIME_HEADLESS',
    'RUNTIME_INTERACTIVE',
    'RecordAgentActivityCommand',
    'RecordAgentSessionRefCommand',
    'ReleaseAgentCommand',
    'ReleaseAgentDestroyReservationCommand',
    'ReleaseAgentRuntimeLaunchCommand',
    'ReserveAgentDestroyCommand',
    'ResolvedUser',
    'RestorePolicyAuthority',
    'RestorePolicyError',
    'RetireAgentLifecycleReceiptCommand',
    'RetireAgentSessionRefsCommand',
    'RollbackRetiredAgentSessionRefsCommand',
    'RevokeAgentCapabilityCommand',
    'RevokeAgentSecretCommand',
    'RuntimeCapability',
    'RuntimeHomePreparers',
    'SEE_ACTORS',
    'SEND_TO',
    'SecretResolver',
    'SecretResolutionError',
    'SecretSource',
    'SetRestoreDispositionCommand',
    'SettleAgentDestroyCommand',
    'SharedCredentialBinding',
    'USER_NOT_FOUND',
    'UnblockAgentCommand',
    'UnpinAgentAdapterCommand',
    'UpdateAgentCommand',
    'UserHomeError',
    'UserProfile',
    'UserProfileError',
    'UserProfileStore',
    'UserStore',
    'UserStoreError',
    'VisibilityError',
    'WORKER_PROXY_FIELDS',
    'WORKER_PROXY_SETTINGS_KEY',
    'WORKFLOW_REMOTE_OWNER_UNSUPPORTED',
    'WORKFLOW_WORKER_RECEIPT_MISMATCH',
    'WorkerProxyError',
    'address_path',
    'agent_home_mode',
    'ambient_proxy_absent',
    'apply_runtime_environment_profile',
    'resolve_agent_secrets',
    'bootstrap_yaml',
    'check_channel',
    'check_org_context',
    'check_shared_path',
    'compose_worker_child_launch',
    'decode_invite',
    'default_bootstrap',
    'default_role_grants_document',
    'derived_proxy_environment',
    'desired_generation',
    'device_key_path',
    'device_record_path',
    'encode_invite',
    'explain',
    'explain_target',
    'identity_slug',
    'invite_deep_link',
    'invite_https_url',
    'is_org_acl_space',
    'is_org_directory_space',
    'launch_worker_proxy_route',
    'load_packaged_support_matrix',
    'may_send',
    'normalize_capabilities',
    'normalize_harness_args',
    'option_value',
    'org_from_space_name',
    'org_space_name',
    'parse_bootstrap_yaml',
    'role_grants_from_document',
    'parse_principal',
    'principal_kind',
    'principal_matches_local_actor',
    'read_device_record',
    'read_worker_proxy',
    'runtime_capabilities',
    'shared_credential_status',
    'snapshot_home_payload',
    'validate_shared_credential_binding',
    'validate_shared_credential_environment',
    'verify_native_projection',
    'visible_targets',
    'whitelist_replacement_environment',
    'write_device_record',
    'write_worker_proxy_field',
]
