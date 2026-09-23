"""Daemon IPC error type and code registry (docs/proto.md §3.7; closes §9 item 4).

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
DAEMON_START_TIMEOUT = "DAEMON_START_TIMEOUT"
# Minted by the daemon into its startup log; the CLI parses the JSON and
# branches on it to explain a refused start (docs/proto.md §3.7 exemplar).
HYPRIAL_HOME_IN_USE = "HYPRIAL_HOME_IN_USE"
# Minted by the daemon's startup owner-migration custody gate (owner_migration)
# into the same startup-log JSON channel; the CLI launcher passes the code
# through and the login S3 start phase branches on it to report the named
# switch outcome instead of a generic startup failure (#513 x #493
# cross-acceptance).  Same membership line as HYPRIAL_HOME_IN_USE above.
OWNER_MIGRATION_CUSTODY_CONFLICT = "OWNER_MIGRATION_CUSTODY_CONFLICT"
OWNER_MIGRATION_CUSTODY_UNREADABLE = "OWNER_MIGRATION_CUSTODY_UNREADABLE"
# Minted by the local install/upgrade migration boundary.  It reaches the
# machine-readable CLI error surface and therefore belongs in this registry
# even though no daemon sends it.
HYPRIAL_HOME_MIGRATION_FAILED = "HYPRIAL_HOME_MIGRATION_FAILED"

# -- addressing / delivery ----------------------------------------------
AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
TARGET_IS_NODE = "TARGET_IS_NODE"
SENDER_UNRESOLVED = "SENDER_UNRESOLVED"
MESSAGE_REPLY_UNAVAILABLE = "MESSAGE_REPLY_UNAVAILABLE"
MESSAGE_REPLY_RESOURCES_UNSUPPORTED = "MESSAGE_REPLY_RESOURCES_UNSUPPORTED"
# Minted by the inbox ack path (row missing / already consumed / TTL-pruned
# all fold into this one result code) and returned through message.ack's
# IPC result field.  Entered the registry when the codex interactive
# carrier began branching on it to classify a settlement target as already
# settled elsewhere.
MESSAGE_ACK_UNAVAILABLE = "MESSAGE_ACK_UNAVAILABLE"
USER_DELIVERY_UNAVAILABLE = "USER_DELIVERY_UNAVAILABLE"
USER_DELIVERY_FAILED = "USER_DELIVERY_FAILED"
ROUTE_ADAPTER_UNCONFIGURED = "ROUTE_ADAPTER_UNCONFIGURED"
ROUTE_SEND_FAILED = "ROUTE_SEND_FAILED"
ROUTE_RESOURCES_UNSUPPORTED = "ROUTE_RESOURCES_UNSUPPORTED"
ROUTE_RESOURCE_REPLY_UNSUPPORTED = "ROUTE_RESOURCE_REPLY_UNSUPPORTED"
# Minted in squire addressing (code= kwargs), travel cross-node over zenoh
# and pass through the daemon IPC error channel (docs/proto.md §2.2 ⑤).
TARGET_SQUIRE_UNCONFIGURED = "TARGET_SQUIRE_UNCONFIGURED"
TARGET_SQUIRE_ADAPTER_UNAVAILABLE = "TARGET_SQUIRE_ADAPTER_UNAVAILABLE"
# Receiver never answered within the receipt window: transient (retryable),
# deliberately distinct from UNCONFIGURED/ADAPTER_UNAVAILABLE (permanent).
USER_DELIVERY_TIMEOUT = "USER_DELIVERY_TIMEOUT"

# -- sessions / channel leases ------------------------------------------
SESSION_SUPERSEDED = "SESSION_SUPERSEDED"
STALE_SESSION = "STALE_SESSION"
INVALID_SESSION_SOURCE = "INVALID_SESSION_SOURCE"
INVALID_CHANNEL_LEASE = "INVALID_CHANNEL_LEASE"
CHANNEL_LIVENESS_UNAVAILABLE = "CHANNEL_LIVENESS_UNAVAILABLE"
STALE_DAEMON_GENERATION = "STALE_DAEMON_GENERATION"

# -- agents / adapters ---------------------------------------------------
AGENT_EXISTS = "AGENT_EXISTS"
AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
ADAPTER_NOT_FOUND = "ADAPTER_NOT_FOUND"
ADAPTER_RELOAD_FAILED = "ADAPTER_RELOAD_FAILED"
USE_ADAPTER_COMMAND = "USE_ADAPTER_COMMAND"

# -- dispatch ------------------------------------------------------------
DISPATCH_NO_CAPABLE_HARNESS = "DISPATCH_NO_CAPABLE_HARNESS"
DISPATCH_ROLE_MISMATCH = "DISPATCH_ROLE_MISMATCH"

# -- lifecycle -------------------------------------------------------------
# Minted by the daemon's lifecycle boundary (application.py
# ``_run_lifecycle_operation``): the manager's consumer thread is dead or
# absent, so admitting an operation would queue work nobody drives.
LIFECYCLE_MANAGER_UNAVAILABLE = "LIFECYCLE_MANAGER_UNAVAILABLE"
# The daemon-side wait for an operation to settle elapsed.  Named for what
# happened instead of overloading DAEMON_START_FAILED (2026-09-14: a dead
# lifecycle thread surfaced as "start failed" on every `hyprial down`).
LIFECYCLE_OPERATION_UNSETTLED = "LIFECYCLE_OPERATION_UNSETTLED"

# -- services ------------------------------------------------------------
ROUTINE_UNAVAILABLE = "ROUTINE_UNAVAILABLE"
WORKFLOW_UNAVAILABLE = "WORKFLOW_UNAVAILABLE"
WORKFLOW_DELIVERY_REFUSED = "WORKFLOW_DELIVERY_REFUSED"
EXTERNAL_REF_CONFLICT = "EXTERNAL_REF_CONFLICT"
WORKFLOW_EXTERNAL_REF_CORRUPT = "WORKFLOW_EXTERNAL_REF_CORRUPT"
AUTOUPDATE_NOTIFICATION_UNAVAILABLE = "AUTOUPDATE_NOTIFICATION_UNAVAILABLE"
ORG_SOURCE_UNREACHABLE = "ORG_SOURCE_UNREACHABLE"

# -- agent.task v1 ------------------------------------------------------
UNSUPPORTED = "UNSUPPORTED"
PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
IDENTITY_OVERRIDE_FORBIDDEN = "IDENTITY_OVERRIDE_FORBIDDEN"
CALLER_NOT_AUTHORIZED = "CALLER_NOT_AUTHORIZED"
SERVICE_BINDING_NOT_FOUND = "SERVICE_BINDING_NOT_FOUND"
RUN_NOT_FOUND = "RUN_NOT_FOUND"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
RESULT_NOT_READY = "RESULT_NOT_READY"
RESULT_REF_CONFLICT = "RESULT_REF_CONFLICT"
RUN_NOT_CANCELLABLE = "RUN_NOT_CANCELLABLE"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
PROTOCOL_ERROR = "PROTOCOL_ERROR"

# -- transfer ------------------------------------------------------------
TRANSFER_WORKER_NOT_FOUND = "TRANSFER_WORKER_NOT_FOUND"
TRANSFER_AMBIGUOUS = "TRANSFER_AMBIGUOUS"
TRANSFER_UNSUPPORTED_HARNESS = "TRANSFER_UNSUPPORTED_HARNESS"
TRANSFER_CONFLICT = "TRANSFER_CONFLICT"
TRANSFER_PIN_CONFLICT = "TRANSFER_PIN_CONFLICT"
TRANSFER_START_FAILED = "TRANSFER_START_FAILED"
STRICT_RESUME_FAILED = "STRICT_RESUME_FAILED"
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


#: Every registered proto-facing code.  Membership is frozen by test; a
#: change here must be deliberate and reviewed as a protocol change.
ALL_CODES: frozenset[str] = frozenset(
    value
    for name, value in globals().items()
    if name.isupper() and isinstance(value, str)
)
