"""Delivery authority over the inbox engine (cell model phase A, slice S5).

Every module here is pure: it imports only the standard library, the kernel
and this package, so the inbox engine can import it without a cycle and
delivery never reaches OrgFS, cells, conversations or threads.  The faces
are ``ports`` (shapes), ``bodies`` (digests and residue), ``retention``
(grant ledger and sweep), ``system_residue`` (the SYSTEM code-default
residue), ``authority`` (bindings and hydrating reads) and ``wake`` (the
observe-mode human-wake gate).
"""

from hyprial.daemon.impl.delivery.authority import (
    INTERACTION_PAGE_MAX,
    DeliveryBodies,
)
from hyprial.daemon.impl.delivery.bodies import (
    RESIDUE_KEYS,
    classify_send_body,
    digest_text,
    payload_sha256,
    residue_payload,
    text_sha256,
)
from hyprial.daemon.impl.delivery.ports import (
    BODY_RETENTION_PARAM,
    BODY_SOURCES,
    DEFAULT_BODY_RETENTION_MS,
    INVALID_TEXT_DIGEST,
    MAX_BODY_RETENTION_MS,
    MIN_BODY_RETENTION_MS,
    SOURCE_INBOX,
    SOURCE_SENDER_COPY,
    WAKE_GATE_OBSERVE,
    BodyGrant,
    BodyRef,
    BodyResolver,
    EnvelopeLike,
    HumanWakeGate,
    InteractionPage,
    InteractionRow,
    clamp_body_retention_ms,
    grant_subject,
)
from hyprial.daemon.impl.delivery.retention import (
    SWEEP_BATCH,
    apply_body_grant,
    ensure_body_retention_schema,
    sweep_body_retention,
)
from hyprial.daemon.impl.delivery.system_residue import (
    SYSTEM_BODY_CLASS,
    sweep_system_residue,
)
from hyprial.daemon.impl.delivery.wake import WOULD_SUPPRESS_EVENT, ObserveWakeGate

__all__ = [
    "BODY_RETENTION_PARAM",
    "BODY_SOURCES",
    "DEFAULT_BODY_RETENTION_MS",
    "INTERACTION_PAGE_MAX",
    "INVALID_TEXT_DIGEST",
    "MAX_BODY_RETENTION_MS",
    "MIN_BODY_RETENTION_MS",
    "RESIDUE_KEYS",
    "SOURCE_INBOX",
    "SOURCE_SENDER_COPY",
    "SWEEP_BATCH",
    "SYSTEM_BODY_CLASS",
    "WAKE_GATE_OBSERVE",
    "WOULD_SUPPRESS_EVENT",
    "BodyGrant",
    "BodyRef",
    "BodyResolver",
    "DeliveryBodies",
    "EnvelopeLike",
    "HumanWakeGate",
    "InteractionPage",
    "InteractionRow",
    "ObserveWakeGate",
    "apply_body_grant",
    "classify_send_body",
    "clamp_body_retention_ms",
    "digest_text",
    "ensure_body_retention_schema",
    "grant_subject",
    "payload_sha256",
    "residue_payload",
    "sweep_body_retention",
    "sweep_system_residue",
    "text_sha256",
]
