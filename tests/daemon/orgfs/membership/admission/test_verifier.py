"""The RecordVerifier port is consulted on safety records (design §3.8)."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs.membership import (
    Accepted,
    Rejected,
    ResultStateAdmission,
    SafetyRecord,
    UnavailableRecordVerifier,
)
from tests.daemon.orgfs.membership.support import (
    OWNER,
    SPACE_ID,
    Replica,
    deliver,
)

ALICE = "user:alice"
DEVICE = "directory/devices/alice/laptop.json"


class _Recording:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.records: list[SafetyRecord] = []

    def verify(self, record: SafetyRecord) -> str:
        self.records.append(record)
        return self.verdict


@pytest.fixture
def origin() -> Replica:
    replica = Replica.create(members={ALICE: "rw"})
    replica.makedirs("directory/devices/alice", author=OWNER)
    return replica


def test_default_verifier_is_unavailable() -> None:
    record = SafetyRecord(SPACE_ID, "member", ALICE, {}, "0" * 64)
    assert UnavailableRecordVerifier().verify(record) == "unavailable"
    assert isinstance(ResultStateAdmission().verifier,
                      UnavailableRecordVerifier)


def test_member_row_change_is_presented_to_the_verifier(origin) -> None:
    verifier = _Recording("unavailable")
    admission = ResultStateAdmission(verifier=verifier)
    receiver = origin.clone(90)
    assert deliver(admission, receiver, origin.invite("user:carol", "ro", by=OWNER)) == Accepted()
    [record] = verifier.records
    assert record.kind == "member" and record.key == "user:carol"
    assert record.payload["addedBy"] == OWNER
    assert len(record.content_sha256) == 64


def test_protected_document_is_presented_with_its_signature_slot(origin) -> None:
    verifier = _Recording("unavailable")
    admission = ResultStateAdmission(verifier=verifier)
    receiver = origin.clone(90)
    tree, content = origin.write_text(DEVICE, '{"deviceId":"d","sig":{"alg":"x"}}', author=ALICE)
    assert deliver(admission, receiver, tree) == Accepted()
    assert deliver(admission, receiver, content) == Accepted()
    [record] = verifier.records
    assert record.kind == "document" and record.key == content.doc_id
    assert record.payload["sig"] == {"alg": "x"}


def test_ordinary_documents_are_not_presented(origin) -> None:
    verifier = _Recording("unavailable")
    admission = ResultStateAdmission(verifier=verifier)
    receiver = origin.clone(90)
    for frame in origin.write_text("plain.md", "x", author=ALICE):
        assert deliver(admission, receiver, frame) == Accepted()
    assert verifier.records == []


@pytest.mark.parametrize("verdict", ["unavailable", "unsigned", "invalid"])
def test_required_verification_rejects_anything_but_verified(origin, verdict) -> None:
    admission = ResultStateAdmission(
        verifier=_Recording(verdict), require_verified=True
    )
    receiver = origin.clone(90)
    frame = origin.invite("user:carol", "ro", by=OWNER)
    result = deliver(admission, receiver, frame)
    assert isinstance(result, Rejected) and result.code == "not-owner"
    assert result.detail["verdict"] == verdict
    verified = ResultStateAdmission(
        verifier=_Recording("verified"), require_verified=True
    )
    assert deliver(verified, receiver, frame) == Accepted()
