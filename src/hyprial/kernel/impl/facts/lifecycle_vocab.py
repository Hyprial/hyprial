"""Lifecycle mutation pure vocabulary (kernel-owned, 依据任务书 §2.2).

四个纯数据/协议类自 daemon/impl/lifecycle_receipts.py 下沉；执行/持久化/补偿权威
仍属 daemon。字段与语义逐字保持（129 条 authority 豁免记录不受影响）。
"""

from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class MutationProvenance:
    """Receipt committed atomically with a domain mutation.

    ``created_by_operation`` is the compensation authority.  ``changed`` is
    only an observation of this delivery attempt and must never be promoted to
    ownership by the process manager.
    """

    created_by_operation: bool
    changed: bool
    resource_token: str

    def __post_init__(self) -> None:
        if not self.resource_token.strip():
            raise ValueError("resource_token must not be blank")

@dataclass(frozen=True, slots=True)
class LifecycleMutationRequest:
    """Domain command plus the token fence required for compensation."""

    correlation_id: str
    attempt_token: str
    operation_id: str
    expected_resource_token: str | None
    payload: object

    def __post_init__(self) -> None:
        if not self.correlation_id.strip() or not self.attempt_token.strip():
            raise ValueError("lifecycle mutation identity must not be blank")
        if not self.operation_id.strip():
            raise ValueError("operation_id must not be blank")
        if (
            self.expected_resource_token is not None
            and not self.expected_resource_token.strip()
        ):
            raise ValueError("expected_resource_token must not be blank")

@dataclass(frozen=True, slots=True)
class LifecycleMutationCompleted:
    correlation_id: str
    attempt_token: str
    generation: int
    version: int
    domain: str
    provenance: MutationProvenance
    payload: object
    replayed: bool = False

@dataclass(frozen=True, slots=True)
class DomainEffectClaim:
    """A durable domain receipt's attestation, for the U0c startup backfill.

    A lifecycle receipt is committed by the domain actor in the SAME
    transaction as its mutation, so its presence proves the effect ran
    even when the process manager died before journalling the completion.
    At startup the daemon turns every surviving receipt into one of these
    claims and backfills the journal (``backfill_domain_attested_effects``),
    so the restart's compensation -- which reads the journal, not the
    receipts -- can undo exactly what the dead generation actually did.
    """

    operation_id: str
    attempt_token: str
    changed: bool
    created_by_operation: bool
    resource_token: str
