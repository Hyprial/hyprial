# Decision record
# - 2026-11-25 (C2): the frozen interface of design §3.6 and §3.8, verbatim
#   where the design spells it out.  ``Admission.admit`` takes the concrete
#   ``StagedDoc`` defined in ``staged.py``.
# - ``SafetyRecord`` is what the admission hands to ``RecordVerifier``: the
#   design names the port ``verify(record)`` but not the record's shape.
"""Admission outcomes, the coordinator's read view and the verifier port."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Mapping, Protocol

if TYPE_CHECKING:
    from hyprial.daemon.impl.orgfs.membership.staged import StagedDoc

Verdict = Literal["verified", "unsigned", "invalid", "unavailable"]
SIGNATURE_FIELD = "sig"
SIGNATURE_PURPOSE = "orgfs.commit-as-user"


@dataclass(frozen=True, slots=True)
class Accepted:
    """The update may be applied to the authoritative instance and committed."""


@dataclass(frozen=True, slots=True)
class Pending:
    """The update cannot be judged yet; it is neither applied nor stored."""

    reason: Literal["dependency", "unknown-doc"]


@dataclass(frozen=True, slots=True)
class Rejected:
    """The update is refused with an ``OrgFsError`` code."""

    code: str
    detail: Mapping[str, object] = field(default_factory=dict)


AdmissionResult = Accepted | Pending | Rejected


class AdmissionView(Protocol):
    """A consistent read-only view taken on the space's serial executor."""

    space_id: str
    space_name: str
    owner: str

    def member_mode(self, user: str) -> str | None: ...

    def tree_row(self, node_id: str) -> Mapping[str, object] | None: ...

    def node_for_doc(self, doc_id: str) -> Mapping[str, object] | None: ...

    def path_of(self, node_id: str) -> str | None: ...


class Admission(Protocol):
    """Result-state admission of one remote update (design §3.6)."""

    def admit(
        self,
        doc_id: str,
        update: bytes,
        expected_vector: bytes,
        staged: "StagedDoc",
        view: AdmissionView,
    ) -> AdmissionResult: ...


@dataclass(frozen=True, slots=True)
class SafetyRecord:
    """A single-author safety record presented to the verifier (§3.8).

    ``kind`` is ``"member"`` for a member row (``key`` = user) or
    ``"document"`` for a protected content document (``key`` = doc id).
    ``payload`` is the row or the parsed JSON body; a signature, when
    present, is its ``sig`` field.
    """

    space_id: str
    kind: Literal["member", "document"]
    key: str
    payload: Mapping[str, object]
    content_sha256: str


class RecordVerifier(Protocol):
    """Signature verification port for safety records (§3.8)."""

    def verify(self, record: SafetyRecord) -> Verdict: ...


class UnavailableRecordVerifier:
    """Today's verifier: binding issuance (U2) is not live, so always ``unavailable``."""

    def verify(self, record: SafetyRecord) -> Verdict:
        return "unavailable"
