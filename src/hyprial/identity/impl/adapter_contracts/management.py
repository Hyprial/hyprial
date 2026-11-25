"""Immutable management authorization reference for staged adapter libraries."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class ManagementAuthorizationRefV1:
    authorization_id: str
    operation_id: str
    generation: int
    nonce: str
    authorization_digest: str
