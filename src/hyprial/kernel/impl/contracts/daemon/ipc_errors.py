"""Daemon IPC error type and code registry (docs/reference/proto.md §3.7; closes §9 item 4).

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
- ``tests/test_ipc_error_code_registry.py`` enforces both directions: the
  registry matches a frozen membership fixture, and no file under
  ``src/hyprial`` spells one of these codes as a string literal outside this
  module.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


class DaemonRequestError(RuntimeError):
    """One daemon IPC refusal carrying its stable wire code and optional data."""

    def __init__(self, code: str, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


# -- generic request/framing --------------------------------------------
DAEMON_ERROR = "DAEMON_ERROR"
INVALID_REQUEST = "INVALID_REQUEST"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
INVALID_PARAMS = "INVALID_PARAMS"
METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
VERSION_MISMATCH = "VERSION_MISMATCH"
IPC_REQUEST_TOO_LARGE = "IPC_REQUEST_TOO_LARGE"
DAEMON_NOT_READY = "DAEMON_NOT_READY"
# Minted by the daemon's restore gate (application.py handle()): while the
# post-accept restore is still running, every method outside the light set
# (ping, shutdown) is refused immediately instead of queueing.  The CLI
# branches on it to print "restoring" rather than a timeout.
DAEMON_RESTORING = "DAEMON_RESTORING"

# -- client-side IPC transport (minted by the CLI/MCP clients, same
# ``.code`` channel and user surface as daemon-sent codes) ---------------
DAEMON_UNAVAILABLE = "DAEMON_UNAVAILABLE"
IPC_TIMEOUT = "IPC_TIMEOUT"
# Minted by the CLI's IPC transport when the daemon peer closes the
# connection before answering (``_peer_gone``).  Joined the registry with
# PR #332 F4②: it rides the CLI's ``--json`` failure object into the e2e
# runner's machine-readable transient judgment, which is exactly the
# membership line above -- a code another process branches on is protocol.
DAEMON_DISCONNECTED = "DAEMON_DISCONNECTED"
DAEMON_START_FAILED = "DAEMON_START_FAILED"
# A harness start was accepted by the lifecycle manager but the harness
# refused or died during its own start precondition.  Distinct from a daemon
# start failure: the CLI command failed, the daemon itself is healthy.
HARNESS_START_FAILED = "HARNESS_START_FAILED"
DAEMON_START_TIMEOUT = "DAEMON_START_TIMEOUT"
# Minted by the daemon into its startup log; the CLI parses the JSON and
# branches on it to explain a refused start (docs/reference/proto.md §3.7 exemplar).
HYPRIAL_HOME_IN_USE = "HYPRIAL_HOME_IN_USE"
# Minted by the daemon's startup owner-migration custody gate (owner_migration)
# into the same startup-log JSON channel; the CLI launcher passes the code
# through and the login S3 start phase branches on it to report the named
# switch outcome instead of a generic startup failure (#513 x #493
# cross-acceptance).  Same membership line as HYPRIAL_HOME_IN_USE above.
OWNER_MIGRATION_CUSTODY_CONFLICT = "OWNER_MIGRATION_CUSTODY_CONFLICT"
OWNER_MIGRATION_CUSTODY_UNREADABLE = "OWNER_MIGRATION_CUSTODY_UNREADABLE"
OWNER_MIGRATION_HOSTED_CONFLICT = "OWNER_MIGRATION_HOSTED_CONFLICT"
# Minted by the local install/upgrade migration boundary.  It reaches the
# machine-readable CLI error surface and therefore belongs in this registry
# even though no daemon sends it.
HYPRIAL_HOME_MIGRATION_FAILED = "HYPRIAL_HOME_MIGRATION_FAILED"

# -- addressing / delivery ----------------------------------------------
AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
TARGET_IS_NODE = "TARGET_IS_NODE"
TARGET_UNRESOLVED = "TARGET_UNRESOLVED"
SENDER_UNRESOLVED = "SENDER_UNRESOLVED"
# Minted by the daemon send boundary when a *verified* caller's send-to
# ledger does not name the target (AT09 / card 358).  Distinct from
# UNSUPPORTED_TARGET (a malformed address) and TARGET_IS_NODE (a node is not
# a delivery target): here the address is fine and the caller may not use it.
TARGET_NOT_AUTHORIZED = "TARGET_NOT_AUTHORIZED"
SENDER_NOT_AUTHENTICATED = "SENDER_NOT_AUTHENTICATED"
# Minted at a real capability entry point when a verified caller's
# capability ledger does not name the resource (AT10 / card 359).
# CHANNEL_NOT_AUTHORIZED covers outbound adapter routes (message.send to
# route:<adapter>:<route>); ORG_CONTEXT_NOT_AUTHORIZED covers reads of the
# org shared folder that the accepted-document grant does not cover.
CHANNEL_NOT_AUTHORIZED = "CHANNEL_NOT_AUTHORIZED"
ORG_CONTEXT_NOT_AUTHORIZED = "ORG_CONTEXT_NOT_AUTHORIZED"
MESSAGE_REPLY_UNAVAILABLE = "MESSAGE_REPLY_UNAVAILABLE"
MESSAGE_REPLY_RESOURCES_UNSUPPORTED = "MESSAGE_REPLY_RESOURCES_UNSUPPORTED"
# -- send links (docs/design/design-send-links-2026-10-14.md §4, §7, §9) --
# A send without ``summary``: refused at the send boundary, nothing queued.
SUMMARY_REQUIRED = "SUMMARY_REQUIRED"
# Hold reasons.  A held send is ``ok: true, queued: true``: the daemon keeps
# the message in the sender's outbox and releases it itself.  None of these
# three is a failure and none may surface as ``ok: false``.
LINK_PEER_FULL = "LINK_PEER_FULL"
LINK_SELF_FULL = "LINK_SELF_FULL"
LINK_WINDOW_FULL = "LINK_WINDOW_FULL"
# The sender's one-sided reference is written; the peer has not referenced
# back yet (§4.2).  Also a hold, released when the link becomes effective.
LINK_MINTING = "LINK_MINTING"
# Refusals.  LINK_UNKNOWN: the receiving daemon's local orgfs copy holds no
# link file for the sender at all (the sender never joined the org space;
# this never self-heals, so it is refused at ingress and recorded as
# ``refused``).  LINK_RECLAIMED: a reply to a request whose link has since
# been reclaimed; the residue is drained, not delivered (R11).
LINK_UNKNOWN = "LINK_UNKNOWN"
LINK_RECLAIMED = "LINK_RECLAIMED"
# message.read_body: the caller may not read this body (not its inbox row,
# already pruned, or fenced out).  Distinct from MESSAGE_ACK_UNAVAILABLE.
MESSAGE_BODY_UNAVAILABLE = "MESSAGE_BODY_UNAVAILABLE"
# Minted by the inbox ack path (row missing / already consumed / TTL-pruned
# all fold into this one result code) and returned through message.ack's
# IPC result field.  Entered the registry when the codex interactive
# carrier began branching on it to classify a settlement target as already
# settled elsewhere.
MESSAGE_ACK_UNAVAILABLE = "MESSAGE_ACK_UNAVAILABLE"
# Minted by ``message.pending.batch`` when its rendering exceeds the inline
# budget and no private actor state root is available for the one spill file.
# Deliberately distinct from a size limit: the batch is refused rather than
# written to a shared directory or sent unbounded inline.
INBOX_BATCH_SPILL_UNAVAILABLE = "INBOX_BATCH_SPILL_UNAVAILABLE"
USER_DELIVERY_UNAVAILABLE = "USER_DELIVERY_UNAVAILABLE"
USER_DELIVERY_FAILED = "USER_DELIVERY_FAILED"
ROUTE_ADAPTER_UNCONFIGURED = "ROUTE_ADAPTER_UNCONFIGURED"
ROUTE_SEND_FAILED = "ROUTE_SEND_FAILED"
ROUTE_RESOURCES_UNSUPPORTED = "ROUTE_RESOURCES_UNSUPPORTED"
ROUTE_RESOURCE_REPLY_UNSUPPORTED = "ROUTE_RESOURCE_REPLY_UNSUPPORTED"
# The daemon refused to read a caller-named local file (path_authz): outside
# the caller's workspace/cwd, inside the hyprial home, or swapped mid-read.
ATTACHMENT_PATH_REFUSED = "ATTACHMENT_PATH_REFUSED"
# Minted in squire addressing (code= kwargs), travel cross-node over zenoh
# and pass through the daemon IPC error channel (docs/reference/proto.md §2.2 ⑤).
TARGET_SQUIRE_UNCONFIGURED = "TARGET_SQUIRE_UNCONFIGURED"
TARGET_SQUIRE_ADAPTER_UNAVAILABLE = "TARGET_SQUIRE_ADAPTER_UNAVAILABLE"
# Receiver never answered within the receipt window: transient (retryable),
# deliberately distinct from UNCONFIGURED/ADAPTER_UNAVAILABLE (permanent).
USER_DELIVERY_TIMEOUT = "USER_DELIVERY_TIMEOUT"
SUBMIT_OUTCOME_UNKNOWN = "SUBMIT_OUTCOME_UNKNOWN"


class SubmitOutcomeUnknownError(DaemonRequestError):
    """A submitted command has no confirmed outcome; never proof of rejection.

    Deliberately outside the generic transient registry: retry requires the
    original target, payload and idempotency key, not an arbitrary new call.
    """

    def __init__(self, message: str, data: Any | None = None) -> None:
        super().__init__(SUBMIT_OUTCOME_UNKNOWN, message, data)

# -- sessions / channel leases ------------------------------------------
SESSION_SUPERSEDED = "SESSION_SUPERSEDED"
STALE_SESSION = "STALE_SESSION"
INVALID_SESSION_SOURCE = "INVALID_SESSION_SOURCE"
INVALID_CHANNEL_LEASE = "INVALID_CHANNEL_LEASE"
CHANNEL_LIVENESS_UNAVAILABLE = "CHANNEL_LIVENESS_UNAVAILABLE"
STALE_DAEMON_GENERATION = "STALE_DAEMON_GENERATION"
# The stdio MCP channel process was started from a different hyprial build/schema
# than the daemon it talks to.  Reported instead of a generic tool failure so the
# operator gets a restart instruction rather than guessing at SUMMARY_REQUIRED.
MCP_CHANNEL_STALE = "MCP_CHANNEL_STALE"

# -- agents / adapters ---------------------------------------------------
AGENT_EXISTS = "AGENT_EXISTS"
AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
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
ADAPTER_NOT_FOUND = "ADAPTER_NOT_FOUND"
ADAPTER_RELOAD_FAILED = "ADAPTER_RELOAD_FAILED"
USE_ADAPTER_COMMAND = "USE_ADAPTER_COMMAND"

# -- dispatch ------------------------------------------------------------
DISPATCH_NO_CAPABLE_HARNESS = "DISPATCH_NO_CAPABLE_HARNESS"
DISPATCH_ROLE_MISMATCH = "DISPATCH_ROLE_MISMATCH"

# -- harness turn settlement --------------------------------------------
# A model stopped for length without producing assistant text.  The harness
# tries pi's own compaction recovery once before minting this code;
# delivery settlement treats it as permanent so redelivery cannot repeat the
# same deterministic full-context turn three times.
CONTEXT_EXHAUSTED = "CONTEXT_EXHAUSTED"

# -- lifecycle -------------------------------------------------------------
# Minted by the daemon's lifecycle boundary (application.py
# ``_run_lifecycle_operation``): the manager's consumer thread is dead or
# absent, so admitting an operation would queue work nobody drives.
LIFECYCLE_MANAGER_UNAVAILABLE = "LIFECYCLE_MANAGER_UNAVAILABLE"
# The daemon-side wait for an operation to settle elapsed.  Named for what
# happened instead of overloading DAEMON_START_FAILED (2026-09-14: a dead
# lifecycle thread surfaced as "start failed" on every `hyprial agent stop`).
LIFECYCLE_OPERATION_UNSETTLED = "LIFECYCLE_OPERATION_UNSETTLED"

# -- services ------------------------------------------------------------
SERVICE_INVALID_TARGET = "SERVICE_INVALID_TARGET"
SERVICE_PORT_IN_USE = "SERVICE_PORT_IN_USE"
SERVICE_NAME_CONFLICT = "SERVICE_NAME_CONFLICT"
SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
SERVICE_NOT_REGISTERED = "SERVICE_NOT_REGISTERED"
SERVICE_SIDECAR_UNSUPPORTED = "SERVICE_SIDECAR_UNSUPPORTED"
SERVICE_REGISTRY_INVALID = "SERVICE_REGISTRY_INVALID"
SERVICE_DEVICE_UNAVAILABLE = "SERVICE_DEVICE_UNAVAILABLE"
SERVICE_NOT_AUTHORIZED = "SERVICE_NOT_AUTHORIZED"
# Minted by the forwarding sidecar I/O owner when a membership command issued
# from an IPC request path (org invite/join/network) outlives its bound; the
# CLI surfaces it as the reply code instead of an uncoded DAEMON_ERROR after
# the roundtrip budget (0.5.5.1 hotfix, 2026-10-28).
MEMBERSHIP_SIDECAR_TIMEOUT = "MEMBERSHIP_SIDECAR_TIMEOUT"
# A join/execute bootstrap step waits (bounded by the request path) for the
# forwarding reconcile it requested; the cadence did not finish one in time,
# or the run it waited on failed without a code of its own.
FORWARDING_RECONCILE_TIMEOUT = "FORWARDING_RECONCILE_TIMEOUT"
FORWARDING_RECONCILE_FAILED = "FORWARDING_RECONCILE_FAILED"
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
ORGFS_CONTENT_PENDING = "content-pending"
ORGFS_INVALID_SPACE_DIRECTORY = "ORGFS_INVALID_SPACE_DIRECTORY"
ORGFS_INVALID_URI = "invalid-uri"
ORGFS_CROSS_SPACE_URI = "cross-space-uri"
# Every external ACL-space mutation is refused at the shared orgfs.* bridge;
# only daemon-internal org lifecycle code may write the space.
ORGFS_ACL_SPACE_EXTERNAL_MUTATION = "ORGFS_ACL_SPACE_EXTERNAL_MUTATION"
# The owner-node ACL entry gate: a content write whose caller is neither the
# owner's terminal, the owner's user-proxy, nor an explicitly granted agent.
ORG_ACL_WRITE_DENIED = "ORG_ACL_WRITE_DENIED"
# org.acl.grant/revoke/ls called by anything but the owner's terminal or the
# owner's user-proxy (harness kind as this daemon registered it at launch).
ORG_ACL_GRANT_FORBIDDEN = "ORG_ACL_GRANT_FORBIDDEN"
# An ACL grant may only name an agent this daemon runs for its own owner.
ORG_ACL_GRANT_NOT_OWNED = "ORG_ACL_GRANT_NOT_OWNED"

# -- retired agent.task v1 facade (kept: the registry is grow-only) ----
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

# -- atomic identity config binding and operator rebind recovery --
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

# -- transfer ------------------------------------------------------------
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
# Minted by lifecycle.start when a start asks to resume a session
# (``sessionRef``) whose transcript the harness cannot find.  Refused BEFORE
# anything starts: pi given an unknown --session-id silently begins a fresh
# session under that very id, so an after-the-fact id comparison cannot tell.
RESUME_SESSION_NOT_FOUND = "RESUME_SESSION_NOT_FOUND"
# Minted by the daemon's transfer.receive container path (missing docker,
# missing image) and passed through its IPC error channel; the CLI-side
# orchestrator raises the same code for its own preflight so the operator
# sees one stable surface.
TRANSFER_PREREQUISITE = "TRANSFER_PREREQUISITE"
# Minted by transfer.receive when the credential staging/bundle is absent
# or incomplete (daemon IPC error channel).
TRANSFER_CREDENTIALS = "TRANSFER_CREDENTIALS"
# Minted by the container pipeline (docker run/volume/load failures),
# passed through transfer.receive's IPC error channel.
TRANSFER_CONTAINER = "TRANSFER_CONTAINER"
# Minted by transfer.land (AT07): the bundle was refused, the landing was
# undone, or the target cannot speak for the bundle's owner.  A name that is
# already taken keeps TRANSFER_CONFLICT instead -- that answer is about the
# name, not about the landing.
TRANSFER_LANDING_FAILED = "TRANSFER_LANDING_FAILED"
# Minted by transfer.land (AT07 item 2): the bundle declares a credential
# envelope the target cannot honour -- the envelope is absent or unreadable, or
# its policy would fall back to the host key without the host owner's grant.
# The target refuses before anything is written; it never opens the envelope
# and never touches the host's own credentials behind the operator's back.
CREDENTIAL_ENVELOPE_MISSING = "CREDENTIAL_ENVELOPE_MISSING"
CREDENTIAL_ENVELOPE_INVALID = "CREDENTIAL_ENVELOPE_INVALID"
CREDENTIAL_HOST_KEY_REFUSED = "CREDENTIAL_HOST_KEY_REFUSED"
# Minted by transfer.receive when the mapping-time sandbox smoke exercises
# the target's own harness sandbox entry point and it fails to enforce
# (design §4): the landing is refused before the session is resumed, so the
# source keeps the worker and no half-mapped target is left behind.
SANDBOX_SMOKE_FAILED = "SANDBOX_SMOKE_FAILED"

#: Wire code -> registered transient class (PR #332 F4②).  Populated
#: exclusively by ``TransientDaemonError.__init_subclass__`` -- the ONE
#: registration point; nothing outside this module adds, removes or
#: re-points an entry.
_TRANSIENT_CODE_CLASSES: dict[str, type["TransientDaemonError"]] = {}


class TransientDaemonError(DaemonRequestError):
    """A daemon-IPC failure whose cause is expected to pass on its own.

    PR #332 F4② (Allen 2026-09-03): ONE exception hierarchy, ONE
    registration point, BOTH ends.  The daemon mints a subclass and its
    ``.code`` is what ``_response`` serialises onto the wire; every client
    (CLI / MCP agent-channel / Lark adapter worker / e2e runner) turns an
    envelope code back into THE SAME class through
    :func:`transient_error_from_code`, so "is this failure transient?" is
    answered exactly once in the codebase -- ``except TransientDaemonError``
    (or one of the registered subclasses).  Re-introducing a string
    comparison against one of these codes is a second judgment system and
    turns ``tests/test_ipc_transient_judgment.py`` red.

    ``DaemonRequestError`` base keeps every existing ``except
    DaemonRequestError`` / ``except RuntimeError`` handler working.
    """

    #: The registered wire code; each subclass declares exactly one.
    CODE: str

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        code = cls.__dict__.get("CODE")
        if not isinstance(code, str) or not code:
            raise TypeError(f"{cls.__name__} must declare its CODE string")
        registered = _TRANSIENT_CODE_CLASSES.setdefault(code, cls)
        if registered is not cls:
            raise TypeError(
                f"transient code {code} is already registered to "
                f"{registered.__name__}; one code, one class"
            )

    def __init__(self, message: str, data: Any | None = None) -> None:
        super().__init__(self.CODE, message, data)


class DaemonRestoringError(TransientDaemonError):
    """The restore gate refused a heavy method at dispatch (pre-side-effect).

    Safe to wait out: the refusal happens BEFORE any effect, so a refused
    mutation never executed (F4①).
    """

    CODE = DAEMON_RESTORING


class DaemonUnavailableError(TransientDaemonError):
    """The daemon socket could not be connected (down / mid-restart)."""

    CODE = DAEMON_UNAVAILABLE


class DaemonDisconnectedError(TransientDaemonError):
    """The daemon peer closed the IPC connection before answering."""

    CODE = DAEMON_DISCONNECTED


class IpcTimeoutError(TransientDaemonError):
    """The daemon did not answer within the request timeout."""

    CODE = IPC_TIMEOUT


#: The frozen code -> class view of the transient registry.
TRANSIENT_CODE_CLASSES: Mapping[str, type[TransientDaemonError]] = MappingProxyType(
    _TRANSIENT_CODE_CLASSES
)


def transient_error_from_code(
    code: str, message: str, data: Any | None = None
) -> TransientDaemonError | None:
    """Deserialize a wire ``error.code`` into its registered class (F4②).

    Returns the registered instance, or ``None`` for every code that is not
    a registered transient code -- including codes a version-skewed newer
    peer minted that this build has never heard of.  ``None`` is the
    "fallback" half of the ruling: the caller routes it down its existing
    permanent-error class (``CliError`` / ``DaemonRequestRejected`` /
    ``DaemonRequestError`` / the runner's hard failure), which carries the
    unknown code verbatim; the lookup itself never raises, so an unknown
    code degrades to a permanent-looking refusal instead of crashing the
    client.
    """

    registered = _TRANSIENT_CODE_CLASSES.get(code)
    if registered is None:
        return None
    return registered(message, data)


# Provider credential seeding (`agent provider login`).  These cross the IPC
# boundary as ``error.code`` and are what the operator branches on when a
# harness credential cannot be installed.  The first line is matched by a
# term-lint rule, so rewording it makes that lint fire.
PROVIDER_UNSUPPORTED = "PROVIDER_UNSUPPORTED"
PROVIDER_SOURCE_MISSING = "PROVIDER_SOURCE_MISSING"
PROVIDER_SOURCE_AMBIGUOUS = "PROVIDER_SOURCE_AMBIGUOUS"
PROVIDER_UNSAFE_PATH = "PROVIDER_UNSAFE_PATH"
PROVIDER_CREDENTIAL_EXISTS = "PROVIDER_CREDENTIAL_EXISTS"


#: Every registered proto-facing code.  Membership is frozen by test; a
#: change here must be deliberate and reviewed as a protocol change.
ALL_CODES: frozenset[str] = frozenset(
    value
    for name, value in globals().items()
    if name.isupper() and isinstance(value, str)
)
