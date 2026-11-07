"""Registry guard for proto-facing IPC error codes (docs/reference/proto.md §3.7).

Three locks, in the spirit of the E2E-010 frozen fixture:

1. Registry membership is frozen to the exact historical strings.  Removing
   or renaming a code turns this red on purpose -- old nodes keep sending
   the old strings, so the registry may only grow.
2. No file under ``src/hyprial`` spells a registered code as a string literal
   outside the registry module.  Writing ``DaemonRequestError("SOME_CODE")``
   (or comparing ``error.code == "SOME_CODE"``) instead of importing the
   constant is exactly the drift §3.7 documents; the AST guard catches it
   at test time.
3. A payload from an old peer carrying the raw historical string still
   matches the constant a new receiver branches on.
"""

from __future__ import annotations

import ast
from pathlib import Path

from hyprial.kernel import ipc_errors

SRC = Path(__file__).resolve().parents[4] / 'src/hyprial'
REGISTRY_FILE = SRC / "kernel" / "impl" / "contracts" / "daemon" / "ipc_errors.py"

# The complete proto-facing code set. New codes normally append here with the
# module; the frozen identity §5 contract explicitly retired its never-shipped
# UNION_ID_UNKNOWN draft before the resolver landed.
FROZEN_CODES = frozenset(
    {
        "ADAPTER_NOT_FOUND",
        "ADAPTER_RELOAD_FAILED",
        "AGENT_CONFIG_CAS_MISMATCH",
        "AGENT_CONFIG_REBIND_CLEANUP_FAILED",
        "AGENT_CONFIG_REBIND_IN_PROGRESS",
        "AGENT_CONFIG_REBIND_INVALID_MAPPING",
        "AGENT_CONFIG_REBIND_INVALID_SOURCE",
        "AGENT_CONFIG_REBIND_MODE",
        "AGENT_CONFIG_REBIND_NOT_QUIESCENT",
        "AGENT_CONFIG_REBIND_NO_BINDING",
        "AGENT_CONFIG_REBIND_OWNERSHIP",
        "AGENT_CONFIG_REBIND_RECEIPT_INVALID",
        "AGENT_CONFIG_REBIND_SECRET_CONTENT",
        "AGENT_CONFIG_REBIND_SOURCE_DRIFT",
        "AGENT_CONFIG_REBIND_SYMLINK",
        "AGENT_CONFIG_REBIND_TARGET_DRIFT",
        "AGENT_CREATE_COMMIT_FAILED",
        "AGENT_CREATE_ROLLBACK_PENDING",
        "AGENT_DEFAULT_ROUTINE_FAILED",
        "AGENT_DEFAULT_ROUTINE_PENDING",
        "AGENT_EXISTS",
        "AGENT_INCARNATION_MISMATCH",
        "AGENT_MIGRATION_BUSY",
        "AGENT_NOT_FOUND",
        "AGENT_SECRET_RESOLUTION_FAILED",
        "AMBIGUOUS_TARGET",
        "ATTACHMENT_PATH_REFUSED",
        "AUTOUPDATE_NOTIFICATION_UNAVAILABLE",
        "CALLER_NOT_AUTHORIZED",
        "CHANNEL_LIVENESS_UNAVAILABLE",
        "CHANNEL_NOT_AUTHORIZED",
        "CODEX_RESUME_AGENT_RESTORE_FAILED",
        "CODEX_CREDENTIAL_MISSING",
        "CONTEXT_EXHAUSTED",
        "CREDENTIAL_ENVELOPE_INVALID",
        "CREDENTIAL_ENVELOPE_MISSING",
        "CREDENTIAL_HOST_KEY_REFUSED",
        "DAEMON_DISCONNECTED",
        "DAEMON_ERROR",
        "DAEMON_NOT_READY",
        "DAEMON_RESTORING",
        "DAEMON_START_FAILED",
        "DAEMON_START_TIMEOUT",
        "DAEMON_UNAVAILABLE",
        "DISPATCH_NO_CAPABLE_HARNESS",
        "DISPATCH_ROLE_MISMATCH",
        "EXTERNAL_REF_CONFLICT",
        "FORWARDING_RECONCILE_FAILED",
        "FORWARDING_RECONCILE_TIMEOUT",
        "HARNESS_RUNTIME_OBSERVATION_UNAUTHORIZED",
        "HARNESS_START_FAILED",
        "HYPRIAL_HOME_IN_USE",
        "HYPRIAL_HOME_MIGRATION_FAILED",
        "IDENTITY_CONFLICT",
        "IDENTITY_OVERRIDE_FORBIDDEN",
        "IDENTITY_SOURCE_UNAVAILABLE",
        "IDENTITY_UNBOUND",
        "INTERACTIVE_CARRIER_STOP_FAILED",
        "INVALID_ARGUMENT",
        "INVALID_CHANNEL_LEASE",
        "INVALID_PARAMS",
        "INVALID_REQUEST",
        "INVALID_SESSION_SOURCE",
        "IPC_REQUEST_TOO_LARGE",
        "IPC_TIMEOUT",
        "LIFECYCLE_INCARNATION_UNSUPPORTED",
        "LIFECYCLE_MANAGER_UNAVAILABLE",
        "LIFECYCLE_OPERATION_UNSETTLED",
        "LINK_MINTING",
        "LINK_PEER_FULL",
        "LINK_RECLAIMED",
        "LINK_SELF_FULL",
        "LINK_UNKNOWN",
        "LINK_WINDOW_FULL",
        "MCP_CHANNEL_STALE",
        "INBOX_BATCH_SPILL_UNAVAILABLE",
        "MESSAGE_ACK_UNAVAILABLE",
        "MESSAGE_BODY_UNAVAILABLE",
        "MESSAGE_REPLY_RESOURCES_UNSUPPORTED",
        "MESSAGE_REPLY_UNAVAILABLE",
        "MEMBERSHIP_SIDECAR_TIMEOUT",
        "METHOD_NOT_FOUND",
        "ORGFS_ACL_SPACE_EXTERNAL_MUTATION",
        # #2069: startup refuses a non-UUID directory under orgfs/spaces/ instead
        # of loading a compaction backup as a second space.  The code names the
        # offending path and carries the same grow-only contract as the rest.
        "ORGFS_INVALID_SPACE_DIRECTORY",
        "ORG_ACL_GRANT_FORBIDDEN",
        "ORG_ACL_GRANT_NOT_OWNED",
        "ORG_ACL_WRITE_DENIED",
        "ORG_CONTEXT_NOT_AUTHORIZED",
        "ORG_SOURCE_UNREACHABLE",
        "ORG_SPACE_NOT_JOINED",
        "OWNER_MIGRATION_CUSTODY_CONFLICT",
        "OWNER_MIGRATION_CUSTODY_UNREADABLE",
        "OWNER_MIGRATION_HOSTED_CONFLICT",
        "PAYLOAD_TOO_LARGE",
        "PREVIEW_EXPIRED",
        "PREVIEW_STALE",
        "PREVIEW_UNKNOWN",
        "OPERATION_IN_PROGRESS",
        "PROTOCOL_ERROR",
        "RESULT_NOT_READY",
        "RESULT_REF_CONFLICT",
        "RESUME_AGENT_RESTORE_FAILED",
        "RESUME_OUTCOME_UNKNOWN",
        "RESUME_SESSION_NOT_FOUND",
        "ROUTE_ADAPTER_UNCONFIGURED",
        "ROUTE_RESOURCES_UNSUPPORTED",
        "ROUTE_RESOURCE_REPLY_UNSUPPORTED",
        "ROUTE_SEND_FAILED",
        "ROUTINE_BINDING_IMMUTABLE",
        "ROUTINE_LAST_BINDING",
        "ROUTINE_NAME_IMMUTABLE",
        "ROUTINE_UNAVAILABLE",
        "RUN_NOT_CANCELLABLE",
        "RUN_NOT_FOUND",
        "SANDBOX_SMOKE_FAILED",
        "SENDER_NOT_AUTHENTICATED",
        "SENDER_UNRESOLVED",
        "SERVICE_BINDING_NOT_FOUND",
        "SERVICE_DEVICE_UNAVAILABLE",
        "SERVICE_INVALID_TARGET",
        "SERVICE_NAME_CONFLICT",
        "SERVICE_NOT_AUTHORIZED",
        "SERVICE_NOT_REGISTERED",
        "SERVICE_PORT_IN_USE",
        "SERVICE_REGISTRY_INVALID",
        "SERVICE_SIDECAR_UNSUPPORTED",
        "SERVICE_UNAVAILABLE",
        "SESSION_SUPERSEDED",
        "STALE_DAEMON_GENERATION",
        "STALE_SESSION",
        "STRICT_RESUME_FAILED",
        "SUBMIT_OUTCOME_UNKNOWN",
        "SUMMARY_REQUIRED",
        "TARGET_IS_NODE",
        "TARGET_NOT_AUTHORIZED",
        "TARGET_NOT_FOUND",
        "TARGET_SQUIRE_ADAPTER_UNAVAILABLE",
        "TARGET_SQUIRE_UNCONFIGURED",
        "TARGET_UNRESOLVED",
        "TRANSFER_AMBIGUOUS",
        "TRANSFER_CONFLICT",
        "TRANSFER_CONTAINER",
        "TRANSFER_CREDENTIALS",
        "TRANSFER_CUSTODY_UNAVAILABLE",
        "TRANSFER_LANDING_FAILED",
        "TRANSFER_PIN_CONFLICT",
        "TRANSFER_PREREQUISITE",
        "TRANSFER_STARTS_HELD",
        "TRANSFER_START_FAILED",
        "TRANSFER_UNSUPPORTED_HARNESS",
        "TRANSFER_WORKER_NOT_FOUND",
        "TRANSPORT_ERROR",
        "UNSUPPORTED",
        "UNSUPPORTED_TARGET",
        "USER_DELIVERY_FAILED",
        "USER_DELIVERY_TIMEOUT",
        "USER_DELIVERY_UNAVAILABLE",
        "USE_ADAPTER_COMMAND",
        "VERSION_MISMATCH",
        "WORKFLOW_DELIVERY_REFUSED",
        "WORKFLOW_EXTERNAL_REF_CORRUPT",
        "WORKFLOW_UNAVAILABLE",
        "PROVIDER_UNSUPPORTED",
        "PROVIDER_SOURCE_MISSING",
        "PROVIDER_SOURCE_AMBIGUOUS",
        "PROVIDER_UNSAFE_PATH",
        "PROVIDER_CREDENTIAL_EXISTS",
        "content-pending",
        "cross-space-uri",
        "invalid-uri",
    }
)


def test_registry_membership_is_frozen() -> None:
    assert ipc_errors.ALL_CODES == FROZEN_CODES
    # Legacy OrgFS wire values retain their shipped lowercase spelling.
    orgfs_names = {
        "content-pending": "ORGFS_CONTENT_PENDING",
        "invalid-uri": "ORGFS_INVALID_URI",
        "cross-space-uri": "ORGFS_CROSS_SPACE_URI",
    }
    for code in FROZEN_CODES:
        name = orgfs_names.get(code, code)
        assert getattr(ipc_errors, name) == code


def test_no_literal_proto_code_outside_the_registry() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path == REGISTRY_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in ipc_errors.ALL_CODES
            ):
                offenders.append(
                    f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: "
                    f'"{node.value}" must be ipc_errors.{node.value}'
                )
    assert not offenders, (
        "proto-facing error codes must come from hyprial.contracts.ipc_errors, "
        "never a string literal:\n" + "\n".join(offenders)
    )


def test_codes_from_old_peers_are_recognised() -> None:
    # An old daemon serialises the raw string; a new receiver compares the
    # payload against the imported constant and must match byte-for-byte.
    old_payload = {"code": "SESSION_SUPERSEDED", "message": "superseded"}
    assert old_payload["code"] == ipc_errors.SESSION_SUPERSEDED
    from hyprial.daemon.impl.mcp.api import SESSION_SUPERSEDED_CODE

    assert old_payload["code"] == SESSION_SUPERSEDED_CODE
    for code in FROZEN_CODES:
        # Wire bytes from any historical peer are members of the registry.
        assert code in ipc_errors.ALL_CODES
