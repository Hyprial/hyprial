# Decision (repository owner, 2026-11-19): the wire error vocabulary is not backend
# logic.  It lives in this root package, outside the five backend domains, so every
# layer imports it without an import-ratchet exception.  This module holds ONLY the
# code constants and ALL_CODES: no classes, no functions, no import of any other
# hyprial module.  Exception types and ``transient_error_from_code`` stay in
# ``hyprial.kernel.impl.contracts.daemon.ipc_errors`` for now and move under
# ``hyprial/error/`` in a later change, split by business area.
# Names, values and order were moved by text from that module; its attribute
# surface (``hyprial.kernel.ipc_errors.X``) is unchanged by re-export.
#
# Per-code decision notes carried over unchanged from the former home.  Each
# note preceded the constant it names; the constant is listed after the marker.
#
# >>> USER_KEY_ABSENT
# -- daemon-owned user signing keys -------------------------------------
#
# >>> DAEMON_ERROR
# -- generic request/framing --------------------------------------------
#
# >>> DAEMON_RESTORING
# Minted by the daemon's restore gate (application.py handle()): while the
# post-accept restore is still running, every method outside the light set
# (ping, shutdown) is refused immediately instead of queueing.  The CLI
# branches on it to print "restoring" rather than a timeout.
#
# >>> DAEMON_UNAVAILABLE
# -- client-side IPC transport (minted by the CLI/MCP clients, same
# ``.code`` channel and user surface as daemon-sent codes) ---------------
#
# >>> DAEMON_DISCONNECTED
# Minted by the kernel IPC client when the daemon peer closes the
# connection before answering (``_peer_gone``).  Joined the registry with
# PR #332 F4②: it rides the CLI's ``--json`` failure object into the e2e
# runner's machine-readable transient judgment, which is exactly the
# membership line above -- a code another process branches on is protocol.
#
# >>> HARNESS_START_FAILED
# A harness start was accepted by the lifecycle manager but the harness
# refused or died during its own start precondition.  Distinct from a daemon
# start failure: the CLI command failed, the daemon itself is healthy.
#
# >>> HYPRIAL_HOME_IN_USE
# Minted by the daemon into its startup log; the CLI parses the JSON and
# branches on it to explain a refused start (docs/reference/proto.md §3.7 exemplar).
#
# >>> HYPRIAL_HOME_MIGRATION_FAILED
# Minted by the local install/upgrade migration boundary.  It reaches the
# machine-readable CLI error surface and therefore belongs in this registry
# even though no daemon sends it.
#
# >>> AMBIGUOUS_TARGET
# -- addressing / delivery ----------------------------------------------
#
# >>> TARGET_NOT_AUTHORIZED
# Minted by the daemon send boundary when a *verified* caller's send-to
# ledger does not name the target (AT09 / card 358).  Distinct from
# UNSUPPORTED_TARGET (a malformed address) and TARGET_IS_NODE (a node is not
# a delivery target): here the address is fine and the caller may not use it.
#
# >>> ORG_CONTEXT_NOT_AUTHORIZED
# Minted at a real capability entry point when a verified caller's
# capability ledger does not name the resource (AT10 / card 359): reads of
# the org shared folder that the accepted-document grant does not cover.
#
# >>> SUMMARY_REQUIRED
# -- send links (docs/design/design-send-links-2026-10-14.md §4, §7, §9) --
# A send without ``summary``: refused at the send boundary, nothing queued.
#
# >>> MESSAGE_BODY_UNAVAILABLE
# message.read_body: the caller may not read this body (not its inbox row,
# already pruned, or fenced out).  Distinct from MESSAGE_ACK_UNAVAILABLE.
#
# >>> MESSAGE_ACK_UNAVAILABLE
# Minted by the inbox ack path (row missing / already consumed / TTL-pruned
# all fold into this one result code) and returned through message.ack's
# IPC result field.  Entered the registry when the codex interactive
# carrier began branching on it to classify a settlement target as already
# settled elsewhere.
#
# >>> INBOX_BATCH_SPILL_UNAVAILABLE
# Minted by ``message.pending.batch`` when its rendering exceeds the inline
# budget and no private actor state root is available for the one spill file.
# Deliberately distinct from a size limit: the batch is refused rather than
# written to a shared directory or sent unbounded inline.
#
# >>> ATTACHMENT_PATH_REFUSED
# The daemon refused to read a caller-named local file (path_authz): outside
# the caller's workspace/cwd, inside the hyprial home, or swapped mid-read.
#
# >>> USER_DELIVERY_TIMEOUT
# Receiver never answered within the receipt window: transient (retryable),
# deliberately distinct from TARGET_UNRESOLVED and USER_DELIVERY_UNAVAILABLE.
#
# >>> PREVIEW_UNKNOWN
# -- preview → execute confirmation (design.md §7; DR-04 vocabulary) ---
# The preview instance ID is NOT a business ``moveId`` and NOT an execution
# operation.  These codes describe the generic confirmation record only.
#
# >>> SESSION_SUPERSEDED
# -- sessions / channel leases ------------------------------------------
#
# >>> MCP_CHANNEL_STALE
# The stdio MCP channel process was started from a different hyprial build/schema
# than the daemon it talks to.  Reported instead of a generic tool failure so the
# operator gets a restart instruction rather than guessing at SUMMARY_REQUIRED.
#
# >>> AGENT_EXISTS
# -- agents / adapters ---------------------------------------------------
#
# >>> DISPATCH_NO_CAPABLE_HARNESS
# -- dispatch ------------------------------------------------------------
#
# >>> CONTEXT_EXHAUSTED
# -- harness turn settlement --------------------------------------------
# A model stopped for length without producing assistant text.  The harness
# tries pi's own compaction recovery once before minting this code;
# delivery settlement treats it as permanent so redelivery cannot repeat the
# same deterministic full-context turn three times.
#
# >>> LIFECYCLE_MANAGER_UNAVAILABLE
# -- lifecycle -------------------------------------------------------------
# Minted by the daemon's lifecycle boundary (application.py
# ``_run_lifecycle_operation``): the manager's consumer thread is dead or
# absent, so admitting an operation would queue work nobody drives.
#
# >>> LIFECYCLE_OPERATION_UNSETTLED
# The daemon-side wait for an operation to settle elapsed.  Named for what
# happened instead of overloading DAEMON_START_FAILED (2026-09-14: a dead
# lifecycle thread surfaced as "start failed" on every `hyprial agent stop`).
#
# >>> LIFECYCLE_OPERATION_TIMEOUT
# The manager exceeded its own operation deadline and compensates the settled
# work; stored as the result's error_code and re-raised verbatim by the
# lifecycle boundary (lifecycle_manager/process.py).
#
# >>> PORT_CLOSING
# A lifecycle step never ran because its domain port refused dispatch:
# PORT_{ADMISSION} minted by lifecycle/atomic_lifecycle_ports.py, same
# spellings for the manager's own dispatch-time refusals (process.py).
#
# >>> LIFECYCLE_DOMAIN_FAILED
# A lifecycle step's domain dispatch failed without a code of its own
# (lifecycle/atomic_lifecycle_ports.py fallback).
#
# >>> LIFECYCLE_COMPENSATION_TIMEOUT
# A compensation step never settled inside its own deadline; the operation
# ends FAILED and the coordinator releases the operation's resources
# (lifecycle/lifecycle_coordinator.py).
#
# >>> DOMAIN_COMMAND_TIMEOUT
# The domain-side correlation wait did not settle inside its budget
# (composition/events.py CorrelatedDomainEvents.wait).
#
# >>> ROUTE_LEASE_STALE
# Route-domain lifecycle step failures (route_registration/io.py).
#
# >>> HARNESS_RUNTIME_CLOSING
# Harness-domain lifecycle step failures (harnesses/actor/*).
#
# >>> AGENT_HOME_ACCESS_FAILED
# Agent-domain lifecycle step failures (identity/impl/agents/*): the code the
# registry error carried when the agent owner rejected the lifecycle command.
#
# >>> ACTOR_STOPPED_UNDELIVERED
# The agent-registry actor retired (stop or quarantine) before a command it
# had accepted could begin; the undelivered sink stamps this code onto the
# rejection (kernel/impl/actor_runtime/guardian.py).
#
# >>> SERVICE_INVALID_TARGET
# -- services ------------------------------------------------------------
#
# >>> MEMBERSHIP_SIDECAR_TIMEOUT
# Minted by the forwarding sidecar I/O owner when a membership command issued
# from an IPC request path (org invite/join/network) outlives its bound; the
# CLI surfaces it as the reply code instead of an uncoded DAEMON_ERROR after
# the roundtrip budget (0.5.5.1 hotfix, 2026-10-28).
#
# >>> FORWARDING_RECONCILE_TIMEOUT
# A join/execute bootstrap step waits (bounded by the request path) for the
# forwarding reconcile it requested; the cadence did not finish one in time,
# or the run it waited on failed without a code of its own.
#
# >>> ORG_PATH_SHAPE_INVALID
# org.fetch candidate enumeration/read rejected a path shape after dispatch.
# Distinct from INVALID_ARGUMENT (shared IPC parameter shape): the request was
# well-formed, but the orgfs path it names is not a directory/readable node.
#
# >>> ORGFS_ACL_SPACE_EXTERNAL_MUTATION
# Every external ACL-space mutation is refused at the shared orgfs.* bridge;
# only daemon-internal org lifecycle code may write the space.
#
# >>> ORGFS_LAYOUT_UNSUPPORTED
# OrgFS minimal (design 2026-11-25 §2.7, phase D P0): a space directory in the
# old journal layout, or a damaged v3 directory, is refused per space with this
# code and a ``reason`` detail; the other spaces keep working.
#
# >>> ORG_ACL_WRITE_DENIED
# The owner-node ACL entry gate: a content write whose caller is neither the
# owner's terminal nor an explicitly granted agent.
#
# >>> ORG_ACL_GRANT_FORBIDDEN
# org.acl.grant/revoke/ls called by anything but the owner's terminal.
#
# >>> ORG_ACL_GRANT_NOT_OWNED
# An ACL grant may only name an agent this daemon runs for its own owner.
#
# >>> UNSUPPORTED
# -- retired agent.task v1 facade (kept: the registry is grow-only) ----
#
# >>> AGENT_INCARNATION_MISMATCH
# -- atomic identity config binding and operator rebind recovery --
#
# >>> TRANSFER_WORKER_NOT_FOUND
# -- transfer ------------------------------------------------------------
#
# >>> RESUME_SESSION_NOT_FOUND
# Minted by lifecycle.start when a start asks to resume a session
# (``sessionRef``) whose transcript the harness cannot find.  Refused BEFORE
# anything starts: pi given an unknown --session-id silently begins a fresh
# session under that very id, so an after-the-fact id comparison cannot tell.
#
# >>> TRANSFER_PREREQUISITE
# Minted by the daemon's transfer.receive container path (missing docker,
# missing image) and passed through its IPC error channel; the CLI-side
# orchestrator raises the same code for its own preflight so the operator
# sees one stable surface.
#
# >>> TRANSFER_CREDENTIALS
# Minted by transfer.receive when the credential staging/bundle is absent
# or incomplete (daemon IPC error channel).
#
# >>> TRANSFER_CONTAINER
# Minted by the container pipeline (docker run/volume/load failures),
# passed through transfer.receive's IPC error channel.
#
# >>> TRANSFER_LANDING_FAILED
# Minted by transfer.land (AT07): the bundle was refused, the landing was
# undone, or the target cannot speak for the bundle's owner.  A name that is
# already taken keeps TRANSFER_CONFLICT instead -- that answer is about the
# name, not about the landing.
#
# >>> CREDENTIAL_ENVELOPE_MISSING
# Minted by transfer.land (AT07 item 2): the bundle declares a credential
# envelope the target cannot honour -- the envelope is absent or unreadable, or
# its policy would fall back to the host key without the host owner's grant.
# The target refuses before anything is written; it never opens the envelope
# and never touches the host's own credentials behind the operator's back.
#
# >>> SANDBOX_SMOKE_FAILED
# Minted by transfer.receive when the mapping-time sandbox smoke exercises
# the target's own harness sandbox entry point and it fails to enforce
# (design §4): the landing is refused before the session is resumed, so the
# source keeps the worker and no half-mapped target is left behind.
#
# >>> PROVIDER_UNSUPPORTED
# Provider credential seeding (`agent provider login`).  These cross the IPC
# boundary as ``error.code`` and are what the operator branches on when a
# harness credential cannot be installed.  The first line is matched by a
# term-lint rule, so rewording it makes that lint fire.
#
# >>> PROVIDER_GRANT_MISSING
# User-owned provider envelopes and grants (R1 v1).
#
# >>> AUTH_OPERATION_CONFLICT
# Onboarding operation codes, already declared in the published
# daemon-request OpenAPI as onboarding-local constants and registered here
# with unchanged spellings (owner 2026-11-21, L1: keep the exposed wire codes
# and make them registered ones).
#
# >>> ORG_CELL_UNBOUND
# OC7 D5 (owner approval 2026-11-23): a member operation on a legacy org whose
# org cell was never created; the owner rebuilds or deletes the org following
# docs/guides/migration-oc7-org-lifecycle.md. No org cell is backfilled.
#
# >>> ORG_ALREADY_DELETED
# OC7 org deletion (owner approval 2026-11-23): the org carries the OrgFS
# `deleted` marker or a committed deletion on this home, so create, invite,
# remove and a new deletion of that name are refused.
#
# >>> ALL_CODES
#: Every registered proto-facing code.  Membership is frozen by test; a
#: change here must be deliberate and reviewed as a protocol change.
"""Wire error-code registry (docs/reference/proto.md §3.7; closes §9 item 4).

One named constant per proto-facing error code -- every code that crosses a
process boundary through the daemon IPC error channel (``error.code`` in a
daemon response, a ``CliError`` surfaced to users/JSON, or an inner-service
code that passes through to the wire unchanged).  Sender and receiver import
the SAME constant, following the ``SESSION_SUPERSEDED_CODE`` precedent the
protocol document promotes from special case to rule: a code a caller
branches on must never be typed twice as a string literal.

Rules:

- Values are frozen once shipped.  Old nodes keep sending these exact
  strings, so a receiver that compares against a constant here is tolerant
  of every historical peer by construction.  Renaming a value is a protocol
  break; add a new code instead and keep recognising the old one.
- **Membership line**: a code enters this registry when its string leaves
  the minting process into another process's MACHINE-READABLE decision
  surface -- a daemon IPC ``error.code`` (minted or passed through), a
  zenoh cross-node payload, or a persisted JSON field that another process
  parses and branches on (e.g. the startup-log ``HYPRIAL_HOME_IN_USE`` the CLI
  matches against).  Client-side-only codes stay OUT: a ``CliError`` code
  that is only ever rendered to a human or emitted in ``--json`` output
  without any other process branching on it is display, not protocol
  (``INVALID_RESPONSE`` and friends).  External platform code spaces
  (Lark) and process-internal exception codes stay out for the same
  reason.  Border case, ruled here: ``UPGRADE_RESTART_FAILED`` /
  ``UPGRADE_NOTIFICATION_FAILED`` land in the autoupdate last-run JSON and
  are projected by the daemon's ``autoupdate.status``, but no process
  branches on the strings -- display-through, so they stay out; the first
  cross-process ``== "UPGRADE_..."`` branch anyone adds moves them in.
- ``tests/daemon/ipc/error/test_ipc_error_code_registry.py`` enforces both directions: the
  registry matches a frozen membership fixture, and no file under
  ``src/hyprial`` spells one of these codes as a string literal outside this
  module.
- ``USER_AGENT_IMMUTABLE`` (UA1b): the owner's user agent is the owner's
  identity on its machine: no harness, never renamed, destroyed, started or
  given work.  Every agent administration and scheduled-work admission aimed
  at it answers this code.
- ``CONVERSATION_NOT_FOUND`` and ``CONVERSATION_NOT_MEMBER`` (slice GC2):
  a group conversation (``g.`` id) that no readable chat space holds, and
  a caller that is not a current member of the group it names.  Both were
  approved by the repository owner on 2026-11-19.
"""

USER_KEY_ABSENT = "USER_KEY_ABSENT"
USER_KEY_UNBOUND = "USER_KEY_UNBOUND"
USER_KEY_BINDING_EXPIRED = "USER_KEY_BINDING_EXPIRED"
USER_KEY_QUARANTINED = "USER_KEY_QUARANTINED"
PURPOSE_UNKNOWN = "PURPOSE_UNKNOWN"
PAYLOAD_INVALID = "PAYLOAD_INVALID"
RATE_LIMITED = "RATE_LIMITED"
DAEMON_ERROR = "DAEMON_ERROR"
INVALID_REQUEST = "INVALID_REQUEST"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
INVALID_PARAMS = "INVALID_PARAMS"
METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
VERSION_MISMATCH = "VERSION_MISMATCH"
IPC_REQUEST_TOO_LARGE = "IPC_REQUEST_TOO_LARGE"
DAEMON_NOT_READY = "DAEMON_NOT_READY"
DAEMON_RESTORING = "DAEMON_RESTORING"
DAEMON_UNAVAILABLE = "DAEMON_UNAVAILABLE"
IPC_TIMEOUT = "IPC_TIMEOUT"
DAEMON_DISCONNECTED = "DAEMON_DISCONNECTED"
DAEMON_START_FAILED = "DAEMON_START_FAILED"
HARNESS_START_FAILED = "HARNESS_START_FAILED"
HARNESS_BINARY_MISSING = "HARNESS_BINARY_MISSING"
DAEMON_START_TIMEOUT = "DAEMON_START_TIMEOUT"
HYPRIAL_HOME_IN_USE = "HYPRIAL_HOME_IN_USE"
HYPRIAL_HOME_MIGRATION_FAILED = "HYPRIAL_HOME_MIGRATION_FAILED"
AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
INVALID_AGENT_NAME = "INVALID_AGENT_NAME"
UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
TARGET_IS_NODE = "TARGET_IS_NODE"
TARGET_UNRESOLVED = "TARGET_UNRESOLVED"
SENDER_UNRESOLVED = "SENDER_UNRESOLVED"
TARGET_NOT_AUTHORIZED = "TARGET_NOT_AUTHORIZED"
SENDER_NOT_AUTHENTICATED = "SENDER_NOT_AUTHENTICATED"
ORG_CONTEXT_NOT_AUTHORIZED = "ORG_CONTEXT_NOT_AUTHORIZED"
ORG_REQUIRED = "ORG_REQUIRED"
MESSAGE_REPLY_UNAVAILABLE = "MESSAGE_REPLY_UNAVAILABLE"
MESSAGE_REPLY_RESOURCES_UNSUPPORTED = "MESSAGE_REPLY_RESOURCES_UNSUPPORTED"
SUMMARY_REQUIRED = "SUMMARY_REQUIRED"
MESSAGE_BODY_UNAVAILABLE = "MESSAGE_BODY_UNAVAILABLE"
MESSAGE_ACK_UNAVAILABLE = "MESSAGE_ACK_UNAVAILABLE"
INBOX_BATCH_SPILL_UNAVAILABLE = "INBOX_BATCH_SPILL_UNAVAILABLE"
USER_DELIVERY_UNAVAILABLE = "USER_DELIVERY_UNAVAILABLE"
USER_DELIVERY_FAILED = "USER_DELIVERY_FAILED"
ATTACHMENT_PATH_REFUSED = "ATTACHMENT_PATH_REFUSED"
USER_DELIVERY_TIMEOUT = "USER_DELIVERY_TIMEOUT"
SUBMIT_OUTCOME_UNKNOWN = "SUBMIT_OUTCOME_UNKNOWN"
PREVIEW_UNKNOWN = "PREVIEW_UNKNOWN"
PREVIEW_EXPIRED = "PREVIEW_EXPIRED"
PREVIEW_STALE = "PREVIEW_STALE"
OPERATION_IN_PROGRESS = "OPERATION_IN_PROGRESS"
SESSION_SUPERSEDED = "SESSION_SUPERSEDED"
STALE_SESSION = "STALE_SESSION"
INVALID_SESSION_SOURCE = "INVALID_SESSION_SOURCE"
INVALID_CHANNEL_LEASE = "INVALID_CHANNEL_LEASE"
CHANNEL_LIVENESS_UNAVAILABLE = "CHANNEL_LIVENESS_UNAVAILABLE"
STALE_DAEMON_GENERATION = "STALE_DAEMON_GENERATION"
MCP_CHANNEL_STALE = "MCP_CHANNEL_STALE"
AGENT_EXISTS = "AGENT_EXISTS"
AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
INVALID_AGENT_NAME = "INVALID_AGENT_NAME"
USER_AGENT_IMMUTABLE = "USER_AGENT_IMMUTABLE"
CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"
CONVERSATION_NOT_MEMBER = "CONVERSATION_NOT_MEMBER"
AGENT_ADDRESS_RETIRED = "AGENT_ADDRESS_RETIRED"
AGENT_CREATE_COMMIT_FAILED = "AGENT_CREATE_COMMIT_FAILED"
AGENT_CREATE_ROLLBACK_PENDING = "AGENT_CREATE_ROLLBACK_PENDING"
CODEX_RESUME_AGENT_RESTORE_FAILED = "CODEX_RESUME_AGENT_RESTORE_FAILED"
CODEX_CREDENTIAL_MISSING = "CODEX_CREDENTIAL_MISSING"
RESUME_AGENT_RESTORE_FAILED = "RESUME_AGENT_RESTORE_FAILED"
RESUME_OUTCOME_UNKNOWN = "RESUME_OUTCOME_UNKNOWN"
AGENT_MIGRATION_BUSY = "AGENT_MIGRATION_BUSY"
AGENT_DEFAULT_ROUTINE_PENDING = "AGENT_DEFAULT_ROUTINE_PENDING"
AGENT_DEFAULT_ROUTINE_FAILED = "AGENT_DEFAULT_ROUTINE_FAILED"
AGENT_SECRET_RESOLUTION_FAILED = "AGENT_SECRET_RESOLUTION_FAILED"
HARNESS_ARGS_CONFLICT = "HARNESS_ARGS_CONFLICT"
HARNESS_ARGS_INVALID = "HARNESS_ARGS_INVALID"
HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED = (
    "HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED"
)
UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
ADAPTER_NOT_FOUND = "ADAPTER_NOT_FOUND"
ADAPTER_RELOAD_FAILED = "ADAPTER_RELOAD_FAILED"
USE_ADAPTER_COMMAND = "USE_ADAPTER_COMMAND"
DISPATCH_NO_CAPABLE_HARNESS = "DISPATCH_NO_CAPABLE_HARNESS"
DISPATCH_ROLE_MISMATCH = "DISPATCH_ROLE_MISMATCH"
CONTEXT_EXHAUSTED = "CONTEXT_EXHAUSTED"
LIFECYCLE_MANAGER_UNAVAILABLE = "LIFECYCLE_MANAGER_UNAVAILABLE"
LIFECYCLE_OPERATION_UNSETTLED = "LIFECYCLE_OPERATION_UNSETTLED"
LIFECYCLE_OPERATION_TIMEOUT = "LIFECYCLE_OPERATION_TIMEOUT"
PORT_CLOSING = "PORT_CLOSING"
PORT_OVERLOADED = "PORT_OVERLOADED"
LIFECYCLE_DOMAIN_FAILED = "LIFECYCLE_DOMAIN_FAILED"
LIFECYCLE_COMPENSATION_TIMEOUT = "LIFECYCLE_COMPENSATION_TIMEOUT"
DOMAIN_COMMAND_TIMEOUT = "DOMAIN_COMMAND_TIMEOUT"
ROUTE_LEASE_STALE = "ROUTE_LEASE_STALE"
ROUTE_IO_FAILED = "ROUTE_IO_FAILED"
ROUTE_CLOSED_BEFORE_COMPLETION = "ROUTE_CLOSED_BEFORE_COMPLETION"
HARNESS_RUNTIME_CLOSING = "HARNESS_RUNTIME_CLOSING"
HARNESS_LIFECYCLE_IN_PROGRESS = "HARNESS_LIFECYCLE_IN_PROGRESS"
LIFECYCLE_ATTEMPT_SETTLED = "LIFECYCLE_ATTEMPT_SETTLED"
HARNESS_LIFECYCLE_RECEIPT_MISMATCH = "HARNESS_LIFECYCLE_RECEIPT_MISMATCH"
HARNESS_LIFECYCLE_STORE_MISSING = "HARNESS_LIFECYCLE_STORE_MISSING"
HARNESS_IO_FAILED = "HARNESS_IO_FAILED"
STALE_HARNESS_IO_COMPLETION = "STALE_HARNESS_IO_COMPLETION"
HARNESS_START_TIMEOUT = "HARNESS_START_TIMEOUT"
HARNESS_COMPLETION_HANDOFF_FAILED = "HARNESS_COMPLETION_HANDOFF_FAILED"
AGENT_HOME_ACCESS_FAILED = "AGENT_HOME_ACCESS_FAILED"
AGENT_RUNTIME_CONTEXT_STALE = "AGENT_RUNTIME_CONTEXT_STALE"
AGENT_RUNTIME_LAUNCH_MISMATCH = "AGENT_RUNTIME_LAUNCH_MISMATCH"
AGENT_LAUNCH_IN_PROGRESS = "AGENT_LAUNCH_IN_PROGRESS"
AGENT_DESTROY_SETTLEMENT_UNKNOWN = "AGENT_DESTROY_SETTLEMENT_UNKNOWN"
AGENT_VERSION_CONFLICT = "AGENT_VERSION_CONFLICT"
INVALID_AGENT_NAME = "INVALID_AGENT_NAME"
PIN_CONFLICT = "PIN_CONFLICT"
PIN_STATE_CONFLICT = "PIN_STATE_CONFLICT"
ACTOR_STOPPED_UNDELIVERED = "ACTOR_STOPPED_UNDELIVERED"
ACTOR_RESTARTED_UNDELIVERED = "ACTOR_RESTARTED_UNDELIVERED"
ACTOR_QUARANTINED_UNDELIVERED = "ACTOR_QUARANTINED_UNDELIVERED"
SERVICE_INVALID_TARGET = "SERVICE_INVALID_TARGET"
SERVICE_PORT_IN_USE = "SERVICE_PORT_IN_USE"
SERVICE_NAME_CONFLICT = "SERVICE_NAME_CONFLICT"
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
SERVICE_NOT_REGISTERED = "SERVICE_NOT_REGISTERED"
SERVICE_SIDECAR_UNSUPPORTED = "SERVICE_SIDECAR_UNSUPPORTED"
SERVICE_REGISTRY_INVALID = "SERVICE_REGISTRY_INVALID"
SERVICE_DEVICE_UNAVAILABLE = "SERVICE_DEVICE_UNAVAILABLE"
SERVICE_NOT_AUTHORIZED = "SERVICE_NOT_AUTHORIZED"
MEMBERSHIP_SIDECAR_TIMEOUT = "MEMBERSHIP_SIDECAR_TIMEOUT"
FORWARDING_RECONCILE_TIMEOUT = "FORWARDING_RECONCILE_TIMEOUT"
FORWARDING_RECONCILE_FAILED = "FORWARDING_RECONCILE_FAILED"
ROUTINE_SCHEMA_ERROR = "ROUTINE_SCHEMA_ERROR"
ENFORCEMENT_ERROR = "ENFORCEMENT_ERROR"
ENFORCEMENT_SCOPE_INVALID = "ENFORCEMENT_SCOPE_INVALID"
VISIBILITY_ERROR = "VISIBILITY_ERROR"
VISIBILITY_SCOPE_INVALID = "VISIBILITY_SCOPE_INVALID"
ROUTINE_UNAVAILABLE = "ROUTINE_UNAVAILABLE"
ROUTINE_NAME_IMMUTABLE = "ROUTINE_NAME_IMMUTABLE"
ROUTINE_BINDING_IMMUTABLE = "ROUTINE_BINDING_IMMUTABLE"
ROUTINE_LAST_BINDING = "ROUTINE_LAST_BINDING"
WORKFLOW_UNAVAILABLE = "WORKFLOW_UNAVAILABLE"
WORKFLOW_DELIVERY_REFUSED = "WORKFLOW_DELIVERY_REFUSED"
EXTERNAL_REF_CONFLICT = "EXTERNAL_REF_CONFLICT"
WORKFLOW_EXTERNAL_REF_CORRUPT = "WORKFLOW_EXTERNAL_REF_CORRUPT"
AUTOUPDATE_NOTIFICATION_UNAVAILABLE = "AUTOUPDATE_NOTIFICATION_UNAVAILABLE"
ORG_SOURCE_UNREACHABLE = "ORG_SOURCE_UNREACHABLE"
ORG_SPACE_NOT_JOINED = "ORG_SPACE_NOT_JOINED"
ORG_PATH_SHAPE_INVALID = "ORG_PATH_SHAPE_INVALID"
ORGFS_CONTENT_PENDING = "content-pending"
ORGFS_INVALID_SPACE_DIRECTORY = "ORGFS_INVALID_SPACE_DIRECTORY"
ORGFS_LAYOUT_UNSUPPORTED = "ORGFS_LAYOUT_UNSUPPORTED"
ORGFS_INVALID_URI = "invalid-uri"
ORGFS_CROSS_SPACE_URI = "cross-space-uri"
ORGFS_ACL_SPACE_EXTERNAL_MUTATION = "ORGFS_ACL_SPACE_EXTERNAL_MUTATION"
ORG_ACL_WRITE_DENIED = "ORG_ACL_WRITE_DENIED"
ORG_ACL_GRANT_FORBIDDEN = "ORG_ACL_GRANT_FORBIDDEN"
ORG_ACL_GRANT_NOT_OWNED = "ORG_ACL_GRANT_NOT_OWNED"
UNSUPPORTED = "UNSUPPORTED"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
IDENTITY_OVERRIDE_FORBIDDEN = "IDENTITY_OVERRIDE_FORBIDDEN"
IDENTITY_UNBOUND = "IDENTITY_UNBOUND"
IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
IDENTITY_SOURCE_UNAVAILABLE = "IDENTITY_SOURCE_UNAVAILABLE"
CALLER_NOT_AUTHORIZED = "CALLER_NOT_AUTHORIZED"
SERVICE_BINDING_NOT_FOUND = "SERVICE_BINDING_NOT_FOUND"
RUN_NOT_FOUND = "RUN_NOT_FOUND"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
RESULT_NOT_READY = "RESULT_NOT_READY"
RESULT_REF_CONFLICT = "RESULT_REF_CONFLICT"
RUN_NOT_CANCELLABLE = "RUN_NOT_CANCELLABLE"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
PROTOCOL_ERROR = "PROTOCOL_ERROR"
AGENT_INCARNATION_MISMATCH = "AGENT_INCARNATION_MISMATCH"
AGENT_CONFIG_CAS_MISMATCH = "AGENT_CONFIG_CAS_MISMATCH"
AGENT_CONFIG_REBIND_INVALID_SOURCE = "AGENT_CONFIG_REBIND_INVALID_SOURCE"
AGENT_CONFIG_REBIND_INVALID_MAPPING = "AGENT_CONFIG_REBIND_INVALID_MAPPING"
AGENT_CONFIG_REBIND_SYMLINK = "AGENT_CONFIG_REBIND_SYMLINK"
AGENT_CONFIG_REBIND_OWNERSHIP = "AGENT_CONFIG_REBIND_OWNERSHIP"
AGENT_CONFIG_REBIND_MODE = "AGENT_CONFIG_REBIND_MODE"
AGENT_CONFIG_REBIND_SECRET_CONTENT = "AGENT_CONFIG_REBIND_SECRET_CONTENT"
AGENT_CONFIG_REBIND_IN_PROGRESS = "AGENT_CONFIG_REBIND_IN_PROGRESS"
AGENT_CONFIG_REBIND_NO_BINDING = "AGENT_CONFIG_REBIND_NO_BINDING"
AGENT_CONFIG_REBIND_SOURCE_DRIFT = "AGENT_CONFIG_REBIND_SOURCE_DRIFT"
AGENT_CONFIG_REBIND_RECEIPT_INVALID = "AGENT_CONFIG_REBIND_RECEIPT_INVALID"
AGENT_CONFIG_REBIND_TARGET_DRIFT = "AGENT_CONFIG_REBIND_TARGET_DRIFT"
AGENT_CONFIG_REBIND_NOT_QUIESCENT = "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
AGENT_CONFIG_REBIND_CLEANUP_FAILED = "AGENT_CONFIG_REBIND_CLEANUP_FAILED"
LIFECYCLE_INCARNATION_UNSUPPORTED = "LIFECYCLE_INCARNATION_UNSUPPORTED"
HARNESS_RUNTIME_OBSERVATION_UNAUTHORIZED = "HARNESS_RUNTIME_OBSERVATION_UNAUTHORIZED"
TRANSFER_WORKER_NOT_FOUND = "TRANSFER_WORKER_NOT_FOUND"
TRANSFER_AMBIGUOUS = "TRANSFER_AMBIGUOUS"
TRANSFER_UNSUPPORTED_HARNESS = "TRANSFER_UNSUPPORTED_HARNESS"
TRANSFER_CONFLICT = "TRANSFER_CONFLICT"
TRANSFER_STARTS_HELD = "TRANSFER_STARTS_HELD"
TRANSFER_CUSTODY_UNAVAILABLE = "TRANSFER_CUSTODY_UNAVAILABLE"
TRANSFER_PIN_CONFLICT = "TRANSFER_PIN_CONFLICT"
TRANSFER_START_FAILED = "TRANSFER_START_FAILED"
STRICT_RESUME_FAILED = "STRICT_RESUME_FAILED"
INTERACTIVE_CARRIER_STOP_FAILED = "INTERACTIVE_CARRIER_STOP_FAILED"
RESUME_SESSION_NOT_FOUND = "RESUME_SESSION_NOT_FOUND"
TRANSFER_PREREQUISITE = "TRANSFER_PREREQUISITE"
TRANSFER_CREDENTIALS = "TRANSFER_CREDENTIALS"
TRANSFER_CONTAINER = "TRANSFER_CONTAINER"
TRANSFER_LANDING_FAILED = "TRANSFER_LANDING_FAILED"
CREDENTIAL_ENVELOPE_MISSING = "CREDENTIAL_ENVELOPE_MISSING"
CREDENTIAL_ENVELOPE_INVALID = "CREDENTIAL_ENVELOPE_INVALID"
CREDENTIAL_HOST_KEY_REFUSED = "CREDENTIAL_HOST_KEY_REFUSED"
SANDBOX_SMOKE_FAILED = "SANDBOX_SMOKE_FAILED"
PROVIDER_UNSUPPORTED = "PROVIDER_UNSUPPORTED"
PROVIDER_SOURCE_MISSING = "PROVIDER_SOURCE_MISSING"
PROVIDER_SOURCE_AMBIGUOUS = "PROVIDER_SOURCE_AMBIGUOUS"
PROVIDER_UNSAFE_PATH = "PROVIDER_UNSAFE_PATH"
PROVIDER_CREDENTIAL_EXISTS = "PROVIDER_CREDENTIAL_EXISTS"
PROVIDER_GRANT_MISSING = "PROVIDER_GRANT_MISSING"
PROVIDER_GRANT_REVOKED = "PROVIDER_GRANT_REVOKED"
PROVIDER_KEY_NOT_FOUND = "PROVIDER_KEY_NOT_FOUND"
PROVIDER_KEY_RECIPIENT_PENDING = "PROVIDER_KEY_RECIPIENT_PENDING"
PROVIDER_KEY_UNSEAL_FAILED = "PROVIDER_KEY_UNSEAL_FAILED"
PROVIDER_KEY_SIGNATURE_INVALID = "PROVIDER_KEY_SIGNATURE_INVALID"
PROVIDER_STORE_UNAVAILABLE = "PROVIDER_STORE_UNAVAILABLE"
PROVIDER_STORE_CONFLICT = "PROVIDER_STORE_CONFLICT"
AUTH_OPERATION_CONFLICT = "AUTH_OPERATION_CONFLICT"
AUTH_OPERATION_NOT_FOUND = "AUTH_OPERATION_NOT_FOUND"
DEVICE_ENSURE_FAILED = "DEVICE_ENSURE_FAILED"
REGISTRATION_UNAVAILABLE = "REGISTRATION_UNAVAILABLE"
ORG_CELL_UNBOUND = "ORG_CELL_UNBOUND"
ORG_ALREADY_DELETED = "ORG_ALREADY_DELETED"


MODEL_CONFIGURATION_INVALID = "MODEL_CONFIGURATION_INVALID"
MODEL_CONFIGURATION_CONFLICT = "MODEL_CONFIGURATION_CONFLICT"
MODEL_CONFIGURATION_INCOMPLETE = "MODEL_CONFIGURATION_INCOMPLETE"
MODEL_OPERATION_CAPACITY_EXCEEDED = "MODEL_OPERATION_CAPACITY_EXCEEDED"
MODEL_SESSION_BUSY = "MODEL_SESSION_BUSY"
MODEL_COMBINATION_UNSUPPORTED = "MODEL_COMBINATION_UNSUPPORTED"
MODEL_AUTH_UNAVAILABLE = "MODEL_AUTH_UNAVAILABLE"
MODEL_RESUME_MISMATCH = "MODEL_RESUME_MISMATCH"
MODEL_UPSTREAM_ERROR = "MODEL_UPSTREAM_ERROR"


ALL_CODES: frozenset[str] = frozenset(
    value
    for name, value in globals().items()
    if name.isupper() and isinstance(value, str)
)
