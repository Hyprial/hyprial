"""Public type surface of the ``identity`` lazy facade.

Decision: codeline-budget split 2026-11-07.  The facade keeps its export table
and ``__getattr__`` (the boundary gate requires them in that module) but no
longer carries the declaration-only imports: they live here and are re-exported
explicitly under ``TYPE_CHECKING``.  Nothing in this module is imported at
runtime, so the lazy export table remains the only runtime binding.

Names whose module path or public spelling carries the model-vendor vocabulary
stay imported in the facade itself: the terminology lint exempts that file only,
and copying those lines here would reintroduce non-whitelisted hits.  The same
holds for the two user-profile names, which the static analyser cannot follow
through a second hop.

Every import keeps its explicit ``X as X`` alias (that redundant alias is what
makes a re-export visible to ruff and to static analysers).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.identity.impl.agents.runtime.model_records.connections import ModelConnection as ModelConnection
    from hyprial.identity.impl.provider_keys import ProviderKeys as ProviderKeys
    from hyprial.identity.impl.sealed_envelope import SealedEnvelope as SealedEnvelope
    from hyprial.identity.impl.user_key import UserKey as UserKey
    from hyprial.identity.impl.user_key.encryption import EncryptionKey as EncryptionKey
    from hyprial.identity.impl.adapter_contracts import AgentIdentitySnapshot as AgentIdentitySnapshot
    from hyprial.identity.impl.adapter_contracts import ClaimReceiptV1 as ClaimReceiptV1
    from hyprial.identity.impl.adapter_contracts import ManagementAuthorizationRefV1 as ManagementAuthorizationRefV1
    from hyprial.identity.impl.adapter_contracts import OrganizationAuthorizationLeaseV1 as OrganizationAuthorizationLeaseV1
    from hyprial.identity.impl.adapter_contracts import OrgRef as OrgRef
    from hyprial.identity.impl.authorization import AuthorizationMechanism as AuthorizationMechanism
    from hyprial.identity.impl.device import BINDING_URI_NAMESPACE as BINDING_URI_NAMESPACE
    from hyprial.identity.impl.device import BINDING_URI_PREFIX as BINDING_URI_PREFIX
    from hyprial.identity.impl.device import CLAIM_FIELDS as CLAIM_FIELDS
    from hyprial.identity.impl.device import DEVICE_CERTIFICATE_ROUTE as DEVICE_CERTIFICATE_ROUTE
    from hyprial.identity.impl.device import ENVELOPE_VERSION as ENVELOPE_VERSION
    from hyprial.identity.impl.device import MAX_REQUEST_BYTES as MAX_REQUEST_BYTES
    from hyprial.identity.impl.device import MAX_RESPONSE_BYTES as MAX_RESPONSE_BYTES
    from hyprial.identity.impl.device import OUTCOME_NOT_SENT as OUTCOME_NOT_SENT
    from hyprial.identity.impl.device import OUTCOME_REJECTED as OUTCOME_REJECTED
    from hyprial.identity.impl.device import OUTCOME_UNKNOWN as OUTCOME_UNKNOWN
    from hyprial.identity.impl.device import PROFILE as PROFILE
    from hyprial.identity.impl.device import RETIRE_BINDING_ROUTE as RETIRE_BINDING_ROUTE
    from hyprial.identity.impl.device import ClientTlsMaterial as ClientTlsMaterial
    from hyprial.identity.impl.device import DeviceAuthorityConfig as DeviceAuthorityConfig
    from hyprial.identity.impl.device import DeviceTransportClient as DeviceTransportClient
    from hyprial.identity.impl.device import TransportClientError as TransportClientError
    from hyprial.identity.impl.device import binding_claims as binding_claims
    from hyprial.identity.impl.device import binding_digest as binding_digest
    from hyprial.identity.impl.device import binding_uri as binding_uri
    from hyprial.identity.impl.device import canonical_binding_bytes as canonical_binding_bytes
    from hyprial.identity.impl.device import load_client_tls_material as load_client_tls_material
    from hyprial.identity.impl.device import verify_certificate_binding as verify_certificate_binding
    from hyprial.identity.impl.device.tailnet import TailnetAuthorityBinding as TailnetAuthorityBinding
    from hyprial.identity.impl.device.tailnet.runtime import TailnetCredentials as TailnetCredentials
    from hyprial.identity.impl.device.tailnet import TailnetAuthorityPin as TailnetAuthorityPin
    from hyprial.identity.impl.device.tailnet import TailnetCredentialAssociation as TailnetCredentialAssociation
    from hyprial.identity.impl.device.tailnet import TailnetCredentialContext as TailnetCredentialContext
    from hyprial.identity.impl.device.tailnet import TailnetCredentialError as TailnetCredentialError
    from hyprial.identity.impl.device.tailnet import TailnetMember as TailnetMember
    from hyprial.identity.impl.device.tailnet import TailnetMembership as TailnetMembership
    from hyprial.identity.impl.device.tailnet import TailnetCredentialOperation as TailnetCredentialOperation
    from hyprial.identity.impl.device.tailnet import TailnetCredentialWorkflow as TailnetCredentialWorkflow
    from hyprial.identity.impl.device import StagedTransportCredential as StagedTransportCredential
    from hyprial.identity.impl.device import TransportCredential as TransportCredential
    from hyprial.identity.impl.device import TransportCredentialError as TransportCredentialError
    from hyprial.identity.impl.device import read_transport_credential as read_transport_credential
    from hyprial.identity.impl.device import retire_transport_credential as retire_transport_credential
    from hyprial.identity.impl.device import stage_transport_credential as stage_transport_credential
    from hyprial.identity.impl.device import transport_credential_status as transport_credential_status
    from hyprial.identity.impl.device import validate_enrollment_response as validate_enrollment_response
    from hyprial.identity.impl.device import commit_transport_credential as commit_transport_credential
    from hyprial.identity.impl.org_directory import JoinIdentityError as JoinIdentityError
    from hyprial.identity.impl.org_directory import JoinTokenLedger as JoinTokenLedger
    from hyprial.identity.impl.org_directory import protected_section_for_path as protected_section_for_path
    from hyprial.identity.impl.org_directory import validate_oidc_join as validate_oidc_join
    from hyprial.identity.impl.org_membership.invites import InviteSelection as InviteSelection
    from hyprial.identity.impl.org_membership.invites import InviteView as InviteView
    from hyprial.identity.impl.org_membership.invites import invite_identifier as invite_identifier
    from hyprial.identity.impl.org_membership.invites import invite_link as invite_link
    from hyprial.identity.impl.org_membership.invites import invite_view as invite_view
    from hyprial.identity.impl.org_membership.invites import invite_views as invite_views
    from hyprial.identity.impl.org_membership.invites import select_invites as select_invites
    import hyprial.identity.impl.org_membership as OrgMembership
    from hyprial.identity.impl.agents.actor._home_calls import (
        HomeOperationConflict as HomeOperationConflict, HomeOperationUnknown as HomeOperationUnknown,
    )
    from hyprial.identity.impl.agents.actor._base import (
        AgentHomeAccessPending as AgentHomeAccessPending, AgentHomeAccessError as AgentHomeAccessError,
    )
    from hyprial.identity.impl.org_directory import (
        ACTION_INVITE as ACTION_INVITE, ACTION_LEAVE as ACTION_LEAVE, ACTION_READ as ACTION_READ, ACTION_REMOVE as ACTION_REMOVE, ACTION_WRITE as ACTION_WRITE,
    )
    from hyprial.identity.impl.device import ADDRESS_RELPATH as ADDRESS_RELPATH
    from hyprial.identity.impl.agents.state.liveness import AGENT_HEARTBEAT_TTL_SECONDS as AGENT_HEARTBEAT_TTL_SECONDS
    from hyprial.identity.impl.agents.actor.ports import AcquireAgentRuntimeLaunchCommand as AcquireAgentRuntimeLaunchCommand
    from hyprial.identity.impl.agents.registry._base import Agent as Agent
    from hyprial.identity.impl.agents.actor._core import AgentActor as AgentActor
    from hyprial.identity.impl.agents.state.liveness import (
        AgentAlreadyRunning as AgentAlreadyRunning, AgentBinding as AgentBinding,
    )
    from hyprial.identity.impl.agents.actor.ports import (
        AgentCapabilityGrantCompleted as AgentCapabilityGrantCompleted, AgentCommand as AgentCommand,
    )
    from hyprial.identity.impl.agents.home.config import AgentConfig as AgentConfig
    from hyprial.identity.impl.agents.registry.config_binding import AgentConfigCasMismatch as AgentConfigCasMismatch
    from hyprial.identity.impl.agents.home.config import AgentConfigError as AgentConfigError
    from hyprial.identity.impl.agents.registry.config_binding import AgentIncarnationMismatch as AgentIncarnationMismatch
    from hyprial.identity.impl.agents.actor.ports import (
        AgentDestroyReservationCompleted as AgentDestroyReservationCompleted, AgentDestroySettled as AgentDestroySettled,
    )
    from hyprial.identity.impl.agents.registry._base import (
        AgentError as AgentError, HOSTED_BY_VALUES as HOSTED_BY_VALUES,
    )
    from hyprial.identity.impl.agents.actor.ports import AgentEvent as AgentEvent
    from hyprial.identity.impl.agents.home.provisioner import AgentHomeError as AgentHomeError
    from hyprial.identity.impl.agents.actor.ports import AgentLifecycleReceiptCompleted as AgentLifecycleReceiptCompleted
    from hyprial.identity.impl.agents.migration.coordinator import AgentMigrationCoordinator as AgentMigrationCoordinator
    from hyprial.identity.impl.agents.actor.ports import (
        AgentHarnessArgsUpdated as AgentHarnessArgsUpdated, AgentMutationCompleted as AgentMutationCompleted, AgentProjection as AgentProjection,
    )
    from hyprial.identity.impl.agents.registry._core import AgentRegistry as AgentRegistry
    from hyprial.identity.impl.agents.home.native_imports import NativeImportRecord as NativeImportRecord
    from hyprial.identity.impl.agents.home.native_imports.launch import capture_native_auth_launch as capture_native_auth_launch
    from hyprial.identity.impl.agents.runtime.context import (
        AgentRuntimeContext as AgentRuntimeContext, AgentRuntimeNotice as AgentRuntimeNotice, AgentRuntimeError as AgentRuntimeError,
    )
    from hyprial.identity.impl.agents.actor.ports import AgentRuntimeLaunchLeaseCompleted as AgentRuntimeLaunchLeaseCompleted
    from hyprial.identity.impl.agents.migration._bindings import AgentRuntimeMigrationBindings as AgentRuntimeMigrationBindings
    from hyprial.identity.impl.agents.actor.ports import AgentSecretGrantCompleted as AgentSecretGrantCompleted
    from hyprial.identity.impl.agents.runtime.context import AgentToolProfile as AgentToolProfile
    from hyprial.identity.impl.agents.runtime.tool_profile import GitAuthorUnresolvedError as GitAuthorUnresolvedError
    from hyprial.identity.impl.org_directory import AllowAllPolicy as AllowAllPolicy
    from hyprial.identity.impl.org_directory import BOOTSTRAP_STEP_KINDS as BOOTSTRAP_STEP_KINDS
    from hyprial.identity.impl.agents.actor.ports import (
        BindAgentCommand as BindAgentCommand, BlockAgentCommand as BlockAgentCommand,
    )
    from hyprial.identity.impl.agents.state.worker_state import BlockingFailureAuthority as BlockingFailureAuthority
    from hyprial.identity.impl.org_directory import BootstrapStep as BootstrapStep
    from hyprial.identity.impl.agents.runtime.enforcement import CAPABILITY_ENTRY_POINTS as CAPABILITY_ENTRY_POINTS
    from hyprial.identity.impl.agents.runtime.visibility import CallerIdentity as CallerIdentity
    from hyprial.identity.impl.agents.runtime.enforcement import CapabilityRecord as CapabilityRecord
    from hyprial.identity.impl.org_directory import CasbinOrgPolicy as CasbinOrgPolicy
    from hyprial.identity.impl.agents.home.environment import ChildEnvironmentLaunch as ChildEnvironmentLaunch
    from hyprial.identity.impl.agents.actor.ports import (
        CleanupRevokedAgentHomeCommand as CleanupRevokedAgentHomeCommand, ClearRestoreDispositionCommand as ClearRestoreDispositionCommand,
    )
    from hyprial.identity.impl.agents.home.config import ConfigProjectionReceipt as ConfigProjectionReceipt
    from hyprial.identity.impl.agents.actor.ports import (
        ConfirmAgentLifecycleReceiptCommand as ConfirmAgentLifecycleReceiptCommand, CreateAgentCommand as CreateAgentCommand,
        CreateTransferHostedAgentCommand as CreateTransferHostedAgentCommand,
    )
    from hyprial.identity.impl.device import (
        DEVICE_KEY_RELPATH as DEVICE_KEY_RELPATH, DEVICE_RECORD_RELPATH as DEVICE_RECORD_RELPATH,
    )
    from hyprial.identity.impl.org_directory import (
        DIRECTORY_DIR as DIRECTORY_DIR, PEOPLE_DIR as PEOPLE_DIR,
    )
    from hyprial.identity.impl.agents.actor.ports import DestroyAgentCommand as DestroyAgentCommand
    from hyprial.identity.impl.device import (
        DeviceRecord as DeviceRecord, MachineBinding as MachineBinding, consume_rotation_event as consume_rotation_event,
    )
    from hyprial.identity.impl.org_directory import (
        DirectoryDevice as DirectoryDevice, DirectoryStore as DirectoryStore, directory_device_path as directory_device_path, directory_owner_principal as
        directory_owner_principal, ProtectedDocIdTooLongError as ProtectedDocIdTooLongError, parse_protected_directory_node_id as parse_protected_directory_node_id, parse_protected_directory_doc_id as
        parse_protected_directory_doc_id, protected_directory_node_id as protected_directory_node_id, protected_directory_doc_id as protected_directory_doc_id, protected_directory_author_allowed as
        protected_directory_author_allowed,
    )
    from hyprial.identity.impl.agents.runtime.enforcement import (
        EnforcementError as EnforcementError, GRANTABLE_CAPABILITIES as GRANTABLE_CAPABILITIES,
    )
    from hyprial.identity.impl.agents.actor.ports import (
        GrantAgentCapabilityCommand as GrantAgentCapabilityCommand, DefaultClaudeCredentialCommand as DefaultClaudeCredentialCommand, DefaultClaudeCredentialCompleted as DefaultClaudeCredentialCompleted,
        GrantAgentSecretCommand as GrantAgentSecretCommand,
    )
    from hyprial.identity.impl.agents.runtime.visibility import GrantRecord as GrantRecord
    from hyprial.identity.impl.agents.registry._base import HandoverNotice as HandoverNotice
    from hyprial.identity.impl.agents.home.provisioner import HomePayloadFile as HomePayloadFile
    from hyprial.identity.impl.agents.home.effects import HomeRuntimePreparer as HomeRuntimePreparer
    from hyprial.identity.impl.identity_transaction import EXPECTED_IDENTITY_GENERATION_ENV as EXPECTED_IDENTITY_GENERATION_ENV
    from hyprial.identity.impl.org_directory import (
        INVITES_DIR as INVITES_DIR, INVITE_SCHEME as INVITE_SCHEME,
    )
    from hyprial.identity.impl.identity_transaction import (
        IdentityTransactionBusy as IdentityTransactionBusy, IdentityTransactionLock as IdentityTransactionLock,
    )
    from hyprial.identity.impl.onboarding import (
        AuthProfile as AuthProfile, CommitPermit as CommitPermit,
        DIFFERENT_ACCOUNT_REASON as DIFFERENT_ACCOUNT_REASON,
        HOME_ACCOUNT_MIGRATION_GUIDE as HOME_ACCOUNT_MIGRATION_GUIDE,
        HomeBinding as HomeBinding,
    )
    from hyprial.identity.impl.onboarding.ports import CREDENTIAL_ARTIFACT_LABEL as CREDENTIAL_ARTIFACT_LABEL
    from hyprial.identity.impl.onboarding import (
        HttpTransport as HttpTransport, IdentityAdmission as IdentityAdmission, IdentityBegin as IdentityBegin, IdentityCredentialPort as IdentityCredentialPort, IdentityCredentialService as
        IdentityCredentialService, IdentityError as IdentityError, IdentityPublicState as IdentityPublicState, IdentityRecordError as IdentityRecordError, IdentitySnapshot as IdentitySnapshot,
        IdentityWorkerError as IdentityWorkerError, IdentityWorkerProtocolError as IdentityWorkerProtocolError, IdentityWorkerUnavailable as IdentityWorkerUnavailable,
        LEGACY_BUSINESS_DATABASES as LEGACY_BUSINESS_DATABASES,
        LEGACY_BUSINESS_TEXT_FILES as LEGACY_BUSINESS_TEXT_FILES,
        LEGACY_DEVICE_STATE_FILES as LEGACY_DEVICE_STATE_FILES,
        LEGACY_UNBOUND_REASON as LEGACY_UNBOUND_REASON,
        LEGACY_USER_KEYS_DIR as LEGACY_USER_KEYS_DIR,
        OnboardingSubject as OnboardingSubject,
    )
    from hyprial.identity.impl.onboarding.logout import (
        LOGIN_ARTIFACT_LABELS as LOGIN_ARTIFACT_LABELS, LOGIN_SETTINGS_FIELDS as LOGIN_SETTINGS_FIELDS, LogoutArtifacts as LogoutArtifacts, LogoutCredentialUnreadable as LogoutCredentialUnreadable,
        LogoutError as LogoutError, LogoutPartialRemoval as LogoutPartialRemoval, LogoutPermissionDenied as LogoutPermissionDenied, LogoutSettingsUnreadable as LogoutSettingsUnreadable,
    )
    from hyprial.identity.impl.onboarding import (
        PUBLIC_STATE_FIELDS as PUBLIC_STATE_FIELDS, WORKER_COMMANDS as WORKER_COMMANDS,
        home_account_conflict_message as home_account_conflict_message,
        legacy_business_data_present as legacy_business_data_present,
        normalize_issuer as normalize_issuer,
        read_home_binding as read_home_binding,
    )
    from hyprial.identity.impl.onboarding.logout import (
        inspect_logout_artifacts as inspect_logout_artifacts, remove_logout_artifacts as remove_logout_artifacts,
    )
    from hyprial.identity.impl.onboarding.credentials.credential_store import (
        CREDENTIAL_VERSION as CREDENTIAL_VERSION, LOGIN_FILENAME as LOGIN_FILENAME, SECRETS_DIRNAME as SECRETS_DIRNAME, IdentityCredential as IdentityCredential, credential_path as
        identity_credential_path, read_login_credential as read_identity_credential, read_login_generation as read_login_generation, write_login_credential as write_identity_credential, reserve_identity_generation as reserve_identity_generation,
    )
    from hyprial.identity.impl.onboarding.identity_record import (
        IDENTITY_RESIDUE_FIELDS as IDENTITY_RESIDUE_FIELDS, node_owner_or_none as node_owner_or_none, read_identity_admission as read_identity_admission, read_identity_snapshot as read_identity_snapshot,
        read_onboarding_identity as read_onboarding_identity, read_onboarding_subject as read_onboarding_subject,
        read_settings_identity as read_settings_identity, read_settings_identity_metadata as read_settings_identity_metadata,
        read_settings_username as read_settings_username,
        resolve_node_owner as resolve_node_owner, resolve_node_user_id as resolve_node_user_id, settings_path as settings_path,
        write_settings_identity as write_settings_identity, write_settings_owner as write_settings_owner,
    )
    from hyprial.identity.impl.agents.registry._base import InvalidAgentNameError as InvalidAgentNameError
    from hyprial.identity.impl.org_directory import (
        InviteError as InviteError, InviteLink as InviteLink, LEAVES_DIR as LEAVES_DIR,
    )
    from hyprial.identity.impl.agents.migration.entry import (
        MigrationAuthorizationWindow as MigrationAuthorizationWindow, MigrationPreflightManifest as MigrationPreflightManifest,
    )
    from hyprial.identity.impl.org_directory import (
        ORG_META_DOC as ORG_META_DOC, ORG_SPACE_PREFIX as ORG_SPACE_PREFIX, OrgPolicy as OrgPolicy,
    )
    from hyprial.identity.impl.pac_errors import (
        PAC_EDGE_EXISTS as PAC_EDGE_EXISTS, PAC_EDGE_INVALID as PAC_EDGE_INVALID, PAC_EXPANSION_ALREADY_BOUND as PAC_EXPANSION_ALREADY_BOUND, PAC_EXPANSION_INVALID as PAC_EXPANSION_INVALID,
        PAC_EXPANSION_POLICY_UNAVAILABLE as PAC_EXPANSION_POLICY_UNAVAILABLE, PAC_EXPANSION_SYSTEM_NODE as PAC_EXPANSION_SYSTEM_NODE, PAC_EVENTS_RESYNC as PAC_EVENTS_RESYNC, PAC_EVENT_ENVELOPE_INVALID as
        PAC_EVENT_ENVELOPE_INVALID, PAC_EVENT_TYPE_UNKNOWN as PAC_EVENT_TYPE_UNKNOWN, PAC_FLAG_ALREADY_SET as PAC_FLAG_ALREADY_SET, PAC_FLAG_NOT_OWNER as PAC_FLAG_NOT_OWNER, PAC_FLAG_NOT_SET as
        PAC_FLAG_NOT_SET, PAC_GRAPH_CLOSED as PAC_GRAPH_CLOSED, PAC_GRAPH_EXISTS as PAC_GRAPH_EXISTS, PAC_GRAPH_FORWARD_CYCLE as PAC_GRAPH_FORWARD_CYCLE, PAC_GRAPH_FROZEN as PAC_GRAPH_FROZEN,
        PAC_GRAPH_NOT_ACTIVE as PAC_GRAPH_NOT_ACTIVE, PAC_GRAPH_NOT_FOUND as PAC_GRAPH_NOT_FOUND, PAC_GRAPH_NOT_OWNER as PAC_GRAPH_NOT_OWNER, PAC_GRAPH_VERSION_CONFLICT as PAC_GRAPH_VERSION_CONFLICT,
        PAC_MIGRATION_SOURCE_UNREADABLE as PAC_MIGRATION_SOURCE_UNREADABLE, PAC_NODE_EXISTS as PAC_NODE_EXISTS, PAC_NODE_NOT_FOUND as PAC_NODE_NOT_FOUND, PAC_NODE_SHAPE_INVALID as PAC_NODE_SHAPE_INVALID,
        PAC_NOTIFY_DELIVERY_FAILED as PAC_NOTIFY_DELIVERY_FAILED, PAC_OPERATION_KEY_CONFLICT as PAC_OPERATION_KEY_CONFLICT, PAC_OWNER_AMBIGUOUS as PAC_OWNER_AMBIGUOUS, PAC_OWNER_UNKNOWN as
        PAC_OWNER_UNKNOWN, PAC_OWNER_UNRESOLVED as PAC_OWNER_UNRESOLVED, PAC_PRINCIPAL_SHAPE_INVALID as PAC_PRINCIPAL_SHAPE_INVALID, PAC_PRINCIPAL_UNVERIFIED as PAC_PRINCIPAL_UNVERIFIED,
        PAC_RESOLUTION_UNAVAILABLE as PAC_RESOLUTION_UNAVAILABLE, PAC_WORKTREE_CLEANUP_FAILED as PAC_WORKTREE_CLEANUP_FAILED, PAC_WORKTREE_IDENTITY_MISMATCH as PAC_WORKTREE_IDENTITY_MISMATCH,
        PAC_WORKTREE_PREPARE_FAILED as PAC_WORKTREE_PREPARE_FAILED, PacError as PacError,
    )
    from hyprial.identity.impl.agents.runtime.enforcement import Principal as Principal
    from hyprial.identity.impl.org_directory import (
        RESOURCE_DIRECTORY_ALL as RESOURCE_DIRECTORY_ALL, RESOURCE_ORG_MEMBERS as RESOURCE_ORG_MEMBERS, RESOURCE_ORG_SELF as RESOURCE_ORG_SELF, ROLE_ADMIN as ROLE_ADMIN, ROLE_MEMBER as ROLE_MEMBER,
        ROLE_OWNER as ROLE_OWNER,
    )
    from hyprial.identity.impl.agents.state.liveness import (
        RUNTIME_HEADLESS as RUNTIME_HEADLESS, RUNTIME_INTERACTIVE as RUNTIME_INTERACTIVE,
    )
    from hyprial.identity.impl.agents.actor.ports import (
        RecordAgentActivityCommand as RecordAgentActivityCommand, RecordAgentActivityIfBindingCommand as RecordAgentActivityIfBindingCommand, RecordAgentSessionRefCommand as RecordAgentSessionRefCommand,
        ReleaseAgentCommand as ReleaseAgentCommand, ReleaseAgentDestroyReservationCommand as ReleaseAgentDestroyReservationCommand, ReleaseAgentRuntimeLaunchCommand as ReleaseAgentRuntimeLaunchCommand,
        ReserveAgentDestroyCommand as ReserveAgentDestroyCommand,
    )
    from hyprial.identity.impl.agents.state.worker_state import (
        RestorePolicyAuthority as RestorePolicyAuthority, RestorePolicyError as RestorePolicyError, is_inactive as is_inactive,
    )
    from hyprial.identity.impl.agents.state.restore_migration import migrate_legacy_agent_keep_list as migrate_legacy_agent_keep_list
    from hyprial.identity.impl.agents.actor.ports import (
        RetireAgentLifecycleReceiptCommand as RetireAgentLifecycleReceiptCommand, RetireAgentSessionRefsCommand as RetireAgentSessionRefsCommand, RollbackRetiredAgentSessionRefsCommand as
        RollbackRetiredAgentSessionRefsCommand, RevokeAgentCapabilityCommand as RevokeAgentCapabilityCommand, RevokeAgentSecretCommand as RevokeAgentSecretCommand,
    )
    from hyprial.identity.impl.users.runtime_capabilities import (
        RuntimeCapability as RuntimeCapability, RuntimeCapabilityError as RuntimeCapabilityError, RuntimeCapabilityStore as RuntimeCapabilityStore,
    )
    from hyprial.identity.impl.agents.home.effects import RuntimeHomePreparers as RuntimeHomePreparers
    from hyprial.identity.impl.agents.runtime.visibility import (
        SEE_ACTORS as SEE_ACTORS, SEND_TO as SEND_TO,
    )
    from hyprial.identity.impl.agents.runtime.secrets import (
        SecretResolver as SecretResolver, SecretResolutionError as SecretResolutionError, SecretSource as SecretSource,
    )
    from hyprial.identity.impl.agents.secret_store import (
        AgentSecretStore as AgentSecretStore, FingerprintUnavailable as FingerprintUnavailable, SecretEvent as SecretEvent, StoredSecret as StoredSecret, agent_secret_store_exists as
        agent_secret_store_exists,
    )
    from hyprial.identity.impl.agents.runtime.secrets import secret_environment_name_is_allowed as secret_environment_name_is_allowed
    from hyprial.identity.impl.agents.runtime.secrets import delete_user_provider as delete_user_provider
    from hyprial.identity.impl.agents.runtime.secrets import read_user_provider_value as read_user_provider_value
    from hyprial.identity.impl.agents.secret_store import secret_fingerprint as secret_fingerprint
    from hyprial.identity.impl.agents.actor.ports import (
        SetRestoreDispositionCommand as SetRestoreDispositionCommand, SettleAgentDestroyCommand as SettleAgentDestroyCommand,
    )
    from hyprial.identity.impl.agents.actor.ports import SetHarnessArgsCommand as SetHarnessArgsCommand
    from hyprial.identity.impl.agents.runtime.context import SharedCredentialBinding as SharedCredentialBinding
    from hyprial.identity.impl.users.store._base import USER_NOT_FOUND as USER_NOT_FOUND
    from hyprial.identity.impl.agents.actor.ports import (
        UnblockAgentCommand as UnblockAgentCommand, BindAgentConfigCommand as BindAgentConfigCommand,
    )
    from hyprial.identity.impl.agents.registry.config_binding import agent_config_digest as agent_config_digest
    from hyprial.identity.impl.agents.actor.ports import UpdateAgentCommand as UpdateAgentCommand
    from hyprial.identity.impl.users.home import UserHomeError as UserHomeError
    from hyprial.identity.impl.users.store._store import UserStore as UserStore
    from hyprial.identity.impl.users.store._base import UserStoreError as UserStoreError
    from hyprial.identity.impl.agents.runtime.visibility import VisibilityError as VisibilityError
    from hyprial.identity.impl.agents.state.worker_proxy import (
        WORKER_PROXY_FIELDS as WORKER_PROXY_FIELDS, WORKER_PROXY_SETTINGS_KEY as WORKER_PROXY_SETTINGS_KEY,
    )
    from hyprial.identity.impl.pac_errors import (
        WORKFLOW_REMOTE_OWNER_UNSUPPORTED as WORKFLOW_REMOTE_OWNER_UNSUPPORTED, WORKFLOW_WORKER_RECEIPT_MISMATCH as WORKFLOW_WORKER_RECEIPT_MISMATCH,
    )
    from hyprial.identity.impl.agents.state.worker_proxy import WorkerProxyError as WorkerProxyError
    from hyprial.identity.impl.device import address_path as address_path
    from hyprial.identity.impl.agents.runtime.context import agent_home_mode as agent_home_mode
    from hyprial.identity.impl.agents.state.worker_proxy import ambient_proxy_absent as ambient_proxy_absent
    from hyprial.identity.impl.agents.home.environment import apply_runtime_environment_profile as apply_runtime_environment_profile
    from hyprial.identity.impl.agents.home.readiness import (
        ReadinessError as ReadinessError, ToolDeclaration as ToolDeclaration, assess_agent_readiness as assess_agent_readiness, parse_tool_declaration as parse_tool_declaration,
    )
    from hyprial.identity.impl.agents.runtime.context import resolve_agent_runtime_context as resolve_agent_runtime_context
    from hyprial.identity.impl.agents.runtime.tool_profile import resolve_agent_tool_profile as resolve_agent_tool_profile
    from hyprial.identity.impl.agents.home.environment import resolve_agent_secrets as resolve_agent_secrets
    from hyprial.identity.impl.agents.home.native_credential import (
        NativeCredentialError as NativeCredentialError, NATIVE_CREDENTIAL_FILES as NATIVE_CREDENTIAL_FILES, install_native_credential as install_native_credential, install_native_credential_set as
        install_native_credential_set, inspect_native_credentials as inspect_native_credentials, remove_native_credentials as remove_native_credentials, native_secrets_root as native_secrets_root, prepare_legacy_claude_native_root as
        prepare_legacy_claude_native_root,
    )
    from hyprial.identity.impl.org_directory import bootstrap_yaml as bootstrap_yaml
    from hyprial.identity.impl.agents.runtime.enforcement import (
        check_org_context as check_org_context, check_shared_path as check_shared_path,
    )
    from hyprial.identity.impl.agents.home.environment import compose_worker_child_launch as compose_worker_child_launch
    from hyprial.identity.impl.org_directory import (
        decode_invite as decode_invite, default_bootstrap as default_bootstrap, default_role_grants_document as default_role_grants_document,
    )
    from hyprial.identity.impl.agents.home.environment import derived_proxy_environment as derived_proxy_environment
    from hyprial.identity.impl.agents.state.worker_state import desired_generation as desired_generation
    from hyprial.identity.impl.device import (
        device_key_path as device_key_path, device_record_path as device_record_path, machine_binding_path as machine_binding_path, machine_fingerprint as machine_fingerprint, mark_first_published as
        mark_first_published,
    )
    from hyprial.identity.impl.org_directory import encode_invite as encode_invite
    from hyprial.identity.impl.agents.runtime.enforcement import explain as explain
    from hyprial.identity.impl.agents.runtime.visibility import explain_target as explain_target
    from hyprial.identity.impl.identity_slug import identity_slug as identity_slug
    from hyprial.identity.impl.org_directory import (
        invite_deep_link as invite_deep_link, invite_https_url as invite_https_url, is_org_acl_space as is_org_acl_space, is_org_directory_space as is_org_directory_space,
    )
    from hyprial.identity.impl.agents.state.worker_proxy import launch_worker_proxy_route as launch_worker_proxy_route
    from hyprial.identity.impl.agents.migration.entry import load_packaged_support_matrix as load_packaged_support_matrix
    from hyprial.identity.impl.agents.runtime.visibility import may_send as may_send
    from hyprial.identity.impl.agents.registry._base import (
        normalize_capabilities as normalize_capabilities, normalize_harness_args as normalize_harness_args,
    )
    from hyprial.identity.impl.agents.runtime.capabilities import option_value as option_value
    from hyprial.identity.impl.org_directory import (
        org_from_space_name as org_from_space_name, org_space_name as org_space_name, parse_bootstrap_yaml as parse_bootstrap_yaml,
    )
    from hyprial.identity.impl.agents.runtime.context import preflight_codex_credential as preflight_codex_credential
    from hyprial.identity.impl.org_directory import role_grants_from_document as role_grants_from_document
    from hyprial.identity.impl.principal import (
        parse_principal as parse_principal, principal_kind as principal_kind, principal_matches_local_actor as principal_matches_local_actor,
    )
    from hyprial.identity.impl.device import (
        read_device_record as read_device_record, read_machine_binding as read_machine_binding,
    )
    from hyprial.identity.impl.agents.state.worker_proxy import read_worker_proxy as read_worker_proxy
    from hyprial.identity.impl.agents.runtime.capabilities import runtime_capabilities as runtime_capabilities
    from hyprial.identity.impl.agents.runtime.context import shared_credential_status as shared_credential_status
    from hyprial.identity.impl.agents.home.provisioner import snapshot_home_payload as snapshot_home_payload
    from hyprial.identity.impl.agents.runtime.context import validate_shared_credential_binding as validate_shared_credential_binding
    from hyprial.identity.impl.worker_session import (
        WorkerSessionBinding as WorkerSessionBinding, worker_session_binding as worker_session_binding,
    )
    from hyprial.identity.impl.agents.runtime.context import validate_shared_credential_environment as validate_shared_credential_environment
    from hyprial.identity.impl.agents.home.config import verify_native_projection as verify_native_projection
    from hyprial.identity.impl.agents.runtime.visibility import visible_targets as visible_targets
    from hyprial.identity.impl.agents.home.environment import whitelist_replacement_environment as whitelist_replacement_environment
    from hyprial.identity.impl.device import (
        write_device_record as write_device_record, write_machine_binding as write_machine_binding,
    )
    from hyprial.identity.impl.agents.state.worker_proxy import write_worker_proxy_field as write_worker_proxy_field
    from hyprial.identity.impl.agents.addressing import (
        id_form_minting as id_form_minting, name_taken_remedy as name_taken_remedy,
        official_agent_uri as official_agent_uri, resolve_local_actor as resolve_local_actor,
    )
    from hyprial.identity.impl.agents.actor.ports import RenameAgentCommand as RenameAgentCommand
    from hyprial.identity.impl.agents.registry._base import agent_id_for_entity_token as agent_id_for_entity_token

__all__ = [
    'ModelConnection',
    'ACTION_INVITE', 'ACTION_LEAVE', 'ACTION_READ', 'ACTION_REMOVE', 'ACTION_WRITE', 'ADDRESS_RELPATH', 'AGENT_HEARTBEAT_TTL_SECONDS', 'AcquireAgentRuntimeLaunchCommand', 'Agent', 'AgentActor',
    'AgentAlreadyRunning', 'AgentBinding', 'AgentCapabilityGrantCompleted', 'AgentCommand', 'AgentConfig', 'AgentConfigCasMismatch', 'AgentConfigError', 'AgentDestroyReservationCompleted',
    'AgentDestroySettled', 'AgentError', 'AgentEvent', 'AgentHomeAccessError', 'AgentHomeAccessPending', 'AgentHomeError', 'AgentIncarnationMismatch', 'AgentLifecycleReceiptCompleted',
    'AgentMigrationCoordinator', 'AgentHarnessArgsUpdated', 'AgentMutationCompleted', 'AgentProjection', 'AgentRegistry', 'NativeImportRecord', 'AgentRuntimeContext', 'AgentRuntimeError', 'AgentRuntimeLaunchLeaseCompleted',
    'AgentRuntimeMigrationBindings', 'AgentRuntimeNotice', 'AgentSecretGrantCompleted', 'AgentSecretStore', 'AgentToolProfile', 'AllowAllPolicy', 'AuthProfile',
    'BOOTSTRAP_STEP_KINDS', 'BindAgentCommand', 'BindAgentConfigCommand', 'BlockAgentCommand', 'BlockingFailureAuthority', 'BootstrapStep',
    'CAPABILITY_ENTRY_POINTS', 'CREDENTIAL_ARTIFACT_LABEL', 'CREDENTIAL_VERSION', 'CallerIdentity', 'CapabilityRecord', 'CasbinOrgPolicy', 'ChildEnvironmentLaunch', 'CleanupRevokedAgentHomeCommand',
    'ClearRestoreDispositionCommand', 'CommitPermit', 'ConfigProjectionReceipt', 'ConfirmAgentLifecycleReceiptCommand', 'CreateAgentCommand',
    'CreateTransferHostedAgentCommand', 'DEVICE_KEY_RELPATH', 'DEVICE_RECORD_RELPATH', 'DIFFERENT_ACCOUNT_REASON', 'DIRECTORY_DIR', 'DefaultClaudeCredentialCommand', 'DefaultClaudeCredentialCompleted', 'DestroyAgentCommand',
    'DeviceRecord', 'DirectoryDevice', 'DirectoryStore', 'EXPECTED_IDENTITY_GENERATION_ENV', 'EnforcementError', 'FingerprintUnavailable', 'GRANTABLE_CAPABILITIES', 'GitAuthorUnresolvedError',
    'GrantAgentCapabilityCommand', 'GrantAgentSecretCommand', 'GrantRecord', 'HOME_ACCOUNT_MIGRATION_GUIDE', 'HOSTED_BY_VALUES', 'HandoverNotice', 'HomeBinding', 'HomeOperationConflict', 'HomeOperationUnknown', 'HomePayloadFile',
    'HomeRuntimePreparer', 'HttpTransport', 'IDENTITY_RESIDUE_FIELDS', 'INVITES_DIR', 'INVITE_SCHEME', 'IdentityAdmission', 'IdentityBegin', 'IdentityCredential', 'IdentityCredentialPort',
    'IdentityCredentialService', 'IdentityError', 'IdentityPublicState', 'IdentityRecordError', 'IdentitySnapshot', 'IdentityTransactionBusy', 'IdentityTransactionLock', 'IdentityWorkerError',
    'IdentityWorkerProtocolError', 'IdentityWorkerUnavailable', 'OnboardingSubject', 'InvalidAgentNameError', 'InviteError', 'InviteLink', 'LEAVES_DIR', 'LEGACY_BUSINESS_DATABASES', 'LEGACY_BUSINESS_TEXT_FILES', 'LEGACY_DEVICE_STATE_FILES', 'LEGACY_UNBOUND_REASON', 'LEGACY_USER_KEYS_DIR', 'LOGIN_ARTIFACT_LABELS', 'LOGIN_FILENAME', 'LOGIN_SETTINGS_FIELDS',
    'LogoutArtifacts', 'LogoutCredentialUnreadable', 'LogoutError', 'LogoutPartialRemoval', 'LogoutPermissionDenied', 'LogoutSettingsUnreadable', 'MachineBinding',
    'MigrationAuthorizationWindow', 'MigrationPreflightManifest', 'NATIVE_CREDENTIAL_FILES', 'NativeCredentialError', 'ORG_META_DOC', 'ORG_SPACE_PREFIX', 'OrgPolicy',
    'PAC_EDGE_EXISTS', 'PAC_EDGE_INVALID', 'PAC_EVENTS_RESYNC', 'PAC_EVENT_ENVELOPE_INVALID', 'PAC_EVENT_TYPE_UNKNOWN', 'PAC_EXPANSION_ALREADY_BOUND', 'PAC_EXPANSION_INVALID',
    'PAC_EXPANSION_POLICY_UNAVAILABLE', 'PAC_EXPANSION_SYSTEM_NODE', 'PAC_FLAG_ALREADY_SET', 'PAC_FLAG_NOT_OWNER', 'PAC_FLAG_NOT_SET', 'PAC_GRAPH_CLOSED', 'PAC_GRAPH_EXISTS',
    'PAC_GRAPH_FORWARD_CYCLE', 'PAC_GRAPH_FROZEN', 'PAC_GRAPH_NOT_ACTIVE', 'PAC_GRAPH_NOT_FOUND', 'PAC_GRAPH_NOT_OWNER', 'PAC_GRAPH_VERSION_CONFLICT', 'PAC_MIGRATION_SOURCE_UNREADABLE',
    'PAC_NODE_EXISTS', 'PAC_NODE_NOT_FOUND', 'PAC_NODE_SHAPE_INVALID', 'PAC_NOTIFY_DELIVERY_FAILED', 'PAC_OPERATION_KEY_CONFLICT', 'PAC_OWNER_AMBIGUOUS', 'PAC_OWNER_UNKNOWN', 'PAC_OWNER_UNRESOLVED',
    'PAC_PRINCIPAL_SHAPE_INVALID', 'PAC_PRINCIPAL_UNVERIFIED', 'PAC_RESOLUTION_UNAVAILABLE', 'PAC_WORKTREE_CLEANUP_FAILED', 'PAC_WORKTREE_IDENTITY_MISMATCH', 'PAC_WORKTREE_PREPARE_FAILED',
    'PEOPLE_DIR', 'PUBLIC_STATE_FIELDS', 'PacError', 'Principal', 'ProtectedDocIdTooLongError',
    'RESOURCE_DIRECTORY_ALL', 'RESOURCE_ORG_MEMBERS', 'RESOURCE_ORG_SELF', 'ROLE_ADMIN', 'ROLE_MEMBER', 'ROLE_OWNER', 'RUNTIME_HEADLESS', 'RUNTIME_INTERACTIVE', 'ReadinessError',
    'RecordAgentActivityCommand', 'RecordAgentActivityIfBindingCommand', 'RecordAgentSessionRefCommand', 'ReleaseAgentCommand', 'ReleaseAgentDestroyReservationCommand',
    'ReleaseAgentRuntimeLaunchCommand', 'RenameAgentCommand', 'ReserveAgentDestroyCommand', 'RestorePolicyAuthority', 'RestorePolicyError', 'RetireAgentLifecycleReceiptCommand',
    'RetireAgentSessionRefsCommand', 'RevokeAgentCapabilityCommand', 'RevokeAgentSecretCommand', 'RollbackRetiredAgentSessionRefsCommand', 'RuntimeCapability', 'RuntimeCapabilityError', 'RuntimeCapabilityStore', 'RuntimeHomePreparers',
    'SECRETS_DIRNAME', 'SEE_ACTORS', 'SEND_TO', 'SecretEvent', 'SecretResolutionError', 'SecretResolver', 'SecretSource', 'SetHarnessArgsCommand', 'SetRestoreDispositionCommand', 'SettleAgentDestroyCommand',
    'SharedCredentialBinding', 'StoredSecret', 'ToolDeclaration', 'USER_NOT_FOUND', 'UnblockAgentCommand', 'UpdateAgentCommand', 'UserHomeError',
    'UserStore', 'UserStoreError', 'VisibilityError', 'WORKER_COMMANDS', 'WORKER_PROXY_FIELDS', 'WORKER_PROXY_SETTINGS_KEY',
    'WORKFLOW_REMOTE_OWNER_UNSUPPORTED', 'WORKFLOW_WORKER_RECEIPT_MISMATCH', 'WorkerProxyError', 'WorkerSessionBinding', 'address_path', 'agent_config_digest', 'agent_home_mode',
    'agent_id_for_entity_token', 'agent_secret_store_exists', 'ambient_proxy_absent', 'apply_runtime_environment_profile', 'assess_agent_readiness',
    'bootstrap_yaml', 'check_org_context', 'check_shared_path', 'compose_worker_child_launch', 'consume_rotation_event',
    'decode_invite', 'default_bootstrap', 'default_role_grants_document', 'delete_user_provider', 'derived_proxy_environment', 'desired_generation', 'device_key_path', 'device_record_path',
    'directory_device_path', 'directory_owner_principal', 'encode_invite', 'explain', 'explain_target', 'id_form_minting', 'identity_credential_path', 'identity_slug', 'inspect_logout_artifacts',
    'install_native_credential', 'install_native_credential_set', 'inspect_native_credentials', 'invite_deep_link', 'invite_https_url', 'is_inactive', 'is_org_acl_space', 'is_org_directory_space', 'JoinIdentityError', 'JoinTokenLedger', 'launch_worker_proxy_route',
    'load_packaged_support_matrix', 'machine_binding_path', 'machine_fingerprint', 'mark_first_published', 'may_send', 'migrate_legacy_agent_keep_list', 'name_taken_remedy', 'native_secrets_root', 'node_owner_or_none',
    'home_account_conflict_message', 'legacy_business_data_present', 'normalize_capabilities', 'normalize_harness_args', 'normalize_issuer', 'official_agent_uri', 'option_value', 'org_from_space_name', 'org_space_name', 'parse_bootstrap_yaml', 'parse_principal', 'parse_protected_directory_doc_id',
    'parse_protected_directory_node_id', 'parse_tool_declaration', 'preflight_codex_credential', 'prepare_legacy_claude_native_root', 'principal_kind', 'principal_matches_local_actor',
    'protected_directory_author_allowed', 'protected_directory_doc_id', 'protected_directory_node_id', 'read_device_record', 'read_user_provider_value', 'read_home_binding', 'read_identity_admission', 'read_identity_credential',
    'read_identity_snapshot', 'read_login_generation', 'read_onboarding_identity', 'read_onboarding_subject', 'read_machine_binding', 'read_settings_identity', 'read_settings_identity_metadata', 'read_settings_username', 'read_worker_proxy', 'remove_logout_artifacts', 'remove_native_credentials',
    'reserve_identity_generation', 'resolve_agent_runtime_context', 'capture_native_auth_launch', 'resolve_agent_secrets', 'resolve_agent_tool_profile', 'resolve_local_actor', 'resolve_node_owner', 'resolve_node_user_id', 'role_grants_from_document', 'runtime_capabilities',
    'secret_environment_name_is_allowed', 'secret_fingerprint', 'settings_path', 'shared_credential_status', 'snapshot_home_payload', 'validate_oidc_join', 'validate_shared_credential_binding',
    'validate_shared_credential_environment', 'verify_native_projection', 'visible_targets', 'whitelist_replacement_environment', 'worker_session_binding', 'write_device_record',
    'write_identity_credential', 'write_machine_binding', 'write_settings_identity', 'write_settings_owner', 'write_worker_proxy_field',
    'AgentIdentitySnapshot', 'AuthorizationMechanism', 'BINDING_URI_NAMESPACE', 'BINDING_URI_PREFIX', 'CLAIM_FIELDS', 'ClaimReceiptV1', 'ClientTlsMaterial', 'DEVICE_CERTIFICATE_ROUTE', 'DeviceAuthorityConfig', 'DeviceTransportClient', 'ENVELOPE_VERSION', 'EncryptionKey', 'InviteSelection', 'InviteView', 'MAX_REQUEST_BYTES', 'MAX_RESPONSE_BYTES', 'ManagementAuthorizationRefV1', 'OUTCOME_NOT_SENT', 'OUTCOME_REJECTED', 'OUTCOME_UNKNOWN', 'OrgMembership', 'OrgRef', 'OrganizationAuthorizationLeaseV1', 'PROFILE', 'ProviderKeys', 'RETIRE_BINDING_ROUTE', 'SealedEnvelope', 'StagedTransportCredential', 'TailnetAuthorityBinding', 'TailnetAuthorityPin', 'TailnetCredentialAssociation', 'TailnetCredentialContext', 'TailnetCredentialError', 'TailnetCredentialOperation', 'TailnetCredentialWorkflow', 'TailnetCredentials', 'TailnetMember', 'TailnetMembership', 'TransportClientError', 'TransportCredential', 'TransportCredentialError', 'UserKey', 'binding_claims', 'binding_digest', 'binding_uri', 'canonical_binding_bytes', 'commit_transport_credential', 'invite_identifier', 'invite_link', 'invite_view', 'invite_views', 'load_client_tls_material', 'protected_section_for_path', 'read_transport_credential', 'retire_transport_credential', 'select_invites', 'stage_transport_credential', 'transport_credential_status', 'validate_enrollment_response', 'verify_certificate_binding'
]
