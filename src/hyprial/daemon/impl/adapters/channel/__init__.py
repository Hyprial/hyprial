"""Daemon-owned shared chat channel authority."""

from __future__ import annotations

import sqlite3

from hyprial.daemon.impl.adapters.contracts import IngressObservation, NativeAlias

from .controls import (
    ControlAuthority,
    ControlDenied,
    ControlReceipt,
    accept_control_response,
    present_control_intent,
    verify_control_callback_ref,
)
from .delivery import (
    AuthorizationDenied,
    DeliveryAttempt,
    DeliveryConflict,
    DeliveryStore,
    PartSubmission,
    ServiceAuthorizationVerifier,
    attempt_authorized_send,
)
from .echo_media import (
    AttachmentDenied,
    BoundedAttachmentAuthority,
    BoundedByteStream,
    EchoAuthority,
    FrozenEchoDecision,
    classify_echo,
)
from .lifecycle import ChannelLifecycle, LifecycleConflict
from .planning import (
    AdmissionDecision,
    AdmissionDenied,
    FrozenTargetSet,
    admit_with_authorization,
    freeze_sync_targets,
    plan_proactive_mail,
    resolve_sync_targets,
    resume_sync_targets,
)
from .store import (
    CanonicalCommit,
    ChannelConflict,
    ChannelStore,
    ChannelStoreFault,
    IngressCapture,
    QuarantineResolution,
)


def _alias(value: str) -> NativeAlias:
    kind, identifier = value.split(":", 1)
    return NativeAlias(
        instance_id="fixture-instance",
        instance_generation=1,
        account_scope="fixture-account",
        provider_resource="messages",
        semantic_kind="create",
        alias_kind=kind,  # type: ignore[arg-type]
        normalized_id=identifier,
        normalized_revision="s:0",
    )


def _observation(identifier: str, aliases: tuple[NativeAlias, ...]) -> IngressObservation:
    return IngressObservation(
        observation_id=identifier,
        aliases=aliases,
        source_kind="live",
        raw_safe_ref=f"safe:{identifier}",
        observation_digest=f"digest:{identifier}",
        received_at_ms=1,
    )


def capture_aliases_atomically(value: dict[str, object]) -> dict[str, object]:
    """C0 compatibility probe routed through the real SQLite transaction."""

    aliases = tuple(_alias(str(item)) for item in value["aliases"])  # type: ignore[union-attr]
    conflict_value = value.get("conflict")
    connection = sqlite3.connect(":memory:")
    store = ChannelStore(connection)
    ingress_alias = _alias(str(value["ingress"]))
    first = store.capture(_observation("fixture-first", (ingress_alias,)), b"first")
    if conflict_value is not None:
        conflict_alias = _alias(str(conflict_value))
        if store.alias_owner(conflict_alias) is None:
            store.capture(_observation("fixture-conflict", (conflict_alias,)), b"other")
    result = store.capture(_observation("fixture-combined", aliases), b"combined")
    captured = [
        item.normalized_id
        for item in aliases
        if store.alias_owner(item) == result.ingress_id and result.state != "quarantined"
    ]
    output = {
        "admissions": store.canonicalize_count(first.ingress_id),
        "owner": "ingress",
        "captured": captured,
        "quarantined": [
            f"{item.alias_kind}:{item.normalized_id}" for item in aliases
        ] if result.state == "quarantined" else [],
    }
    connection.close()
    return output


__all__ = [
    "AdmissionDecision",
    "AdmissionDenied",
    "AttachmentDenied",
    "AuthorizationDenied",
    "BoundedAttachmentAuthority",
    "BoundedByteStream",
    "CanonicalCommit",
    "ChannelConflict",
    "ChannelLifecycle",
    "ChannelStore",
    "ChannelStoreFault",
    "ControlAuthority",
    "ControlDenied",
    "ControlReceipt",
    "DeliveryAttempt",
    "DeliveryConflict",
    "DeliveryStore",
    "EchoAuthority",
    "FrozenEchoDecision",
    "FrozenTargetSet",
    "IngressCapture",
    "LifecycleConflict",
    "PartSubmission",
    "QuarantineResolution",
    "ServiceAuthorizationVerifier",
    "accept_control_response",
    "admit_with_authorization",
    "attempt_authorized_send",
    "capture_aliases_atomically",
    "classify_echo",
    "freeze_sync_targets",
    "plan_proactive_mail",
    "present_control_intent",
    "resolve_sync_targets",
    "resume_sync_targets",
    "verify_control_callback_ref",
]
