from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.org.document import parse_document, serialize_document
from hyprial.daemon.impl.org.store import OrgContextStore, OrgVersionError


def _document(version: int, *, publisher: str = "alicesmith"):
    source = f"""# Organization v{version}

```yaml
meta:
  version: {version}
  issued_at: 2026-08-16
  publisher: {publisher}
lines: []
members: []
routing: []
norms:
  autonomy: [任务域内自治]
  escalation: 超域必须升级
  etiquette: 先引荐再委派
residents: []
```
"""
    return parse_document(source)


def test_store_adopts_atomically_and_records_local_acceptance(tmp_path: Path) -> None:
    store = OrgContextStore(tmp_path)
    adopted_at = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
    document = _document(1)

    record = store.adopt(document, adopted_at=adopted_at)

    assert store.accepted_path.read_text(encoding="utf-8") == serialize_document(
        document
    )
    assert store.accepted_path.stat().st_mode & 0o777 == 0o600
    assert record.version == 1
    assert record.publisher == "alicesmith"
    assert record.adopted_at == adopted_at
    assert store.load_accepted() == document


def test_store_rejects_non_monotonic_versions(tmp_path: Path) -> None:
    store = OrgContextStore(tmp_path)
    store.adopt(_document(2))

    with pytest.raises(OrgVersionError, match="newer than accepted version 2"):
        store.adopt(_document(2))
    with pytest.raises(OrgVersionError, match="newer than accepted version 2"):
        store.adopt(_document(1))


def test_store_tolerates_legacy_signed_documents(tmp_path: Path) -> None:
    """Pre-unsigning documents carry a meta.signature field; parsing must
    tolerate and ignore it (no data migration for a retiring field)."""

    store = OrgContextStore(tmp_path)
    source = """# Legacy

```yaml
meta:
  version: 1
  issued_at: 2026-08-16
  publisher: alicesmith
  signature: ed25519-v1:AAAA:BBBB
lines: []
members: []
routing: []
norms:
  autonomy: [任务域内自治]
  escalation: 超域必须升级
  etiquette: 先引荐再委派
residents: []
```
"""
    store.adopt(parse_document(source))
    assert store.load_accepted().meta.publisher == "alicesmith"


def test_failed_atomic_replace_preserves_previous_accepted_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = OrgContextStore(tmp_path)
    original = _document(1)
    store.adopt(original)
    real_replace = __import__("os").replace

    def fail_target(source: Path | str, target: Path | str) -> None:
        if Path(target) == store.accepted_path:
            raise OSError("injected replace failure")
        real_replace(source, target)

    monkeypatch.setattr('hyprial.daemon.impl.org.store.os.replace', fail_target)
    with pytest.raises(OSError, match="injected replace failure"):
        store.adopt(_document(2))

    assert store.load_accepted() == original
    assert not tuple(tmp_path.glob(".org-context.md.*.tmp"))


def test_failed_acceptance_metadata_write_does_not_commit_new_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = OrgContextStore(tmp_path)
    original = _document(1)
    original_record = store.adopt(original)
    real_replace = __import__("os").replace

    def fail_metadata(source: Path | str, target: Path | str) -> None:
        if Path(target).parent == store.adoptions_dir:
            raise OSError("injected metadata failure")
        real_replace(source, target)

    monkeypatch.setattr('hyprial.daemon.impl.org.store.os.replace', fail_metadata)
    with pytest.raises(OSError, match="injected metadata failure"):
        store.adopt(_document(2))

    assert store.load_accepted() == original
    assert store.acceptance_record() == original_record


def test_acceptance_record_hashes_raw_bytes_not_normalized_text(
    tmp_path: Path,
) -> None:
    """A CRLF body is valid Markdown; the audit digest is over the bytes on
    disk, never the newline-normalized text ``read_text`` would produce."""

    source = _document(1).source.replace(
        "# Organization v1\n", "# Organization v1\r\n", 1
    )
    store = OrgContextStore(tmp_path)
    document = parse_document(source)

    record = store.adopt(document)

    assert store.accepted_path.read_bytes() == source.encode("utf-8")
    assert record.document_sha256 == hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()
    assert store.acceptance_record() == record


def test_acceptance_record_matches_a_fully_crlf_file(tmp_path: Path) -> None:
    """A whole CRLF file's raw-byte digest is what its record must name."""

    raw = _document(1).source.replace("\n", "\r\n").encode("utf-8")
    store = OrgContextStore(tmp_path)
    store.ensure_layout()
    store.accepted_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    (store.adoptions_dir / f"{digest}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "publisher": "alicesmith",
                "adoptedAt": "2026-08-16T08:00:00Z",
                "documentSha256": digest,
            }
        ),
        encoding="utf-8",
    )

    record = store.acceptance_record()

    assert record is not None
    assert record.document_sha256 == digest
    assert record.version == 1
    assert record.publisher == "alicesmith"
    assert store.load_accepted() is not None


def test_status_warns_and_keeps_the_slot_when_the_record_is_missing(
    tmp_path: Path,
) -> None:
    store = OrgContextStore(tmp_path)
    store.ensure_layout()
    store.accepted_path.write_text(
        serialize_document(_document(1)), encoding="utf-8"
    )

    assert store.acceptance_record() is None

    status = store.status()

    assert status["slot"] == "accepted"
    assert "accepted" not in status
    # The parseable body still backs the displayable fields; only the adoption
    # time is unknown and must never be invented.
    assert status["meta"] == {
        "version": 1,
        "issuedAt": "2026-08-16",
        "publisher": "alicesmith",
    }
    assert status["warnings"][0]["code"] == (
        "ORG_ACCEPTANCE_METADATA_UNAVAILABLE"
    )
    assert "adoption" in status["warnings"][0]["message"]


def test_pending_area_stages_verified_versions_without_adopting(tmp_path: Path) -> None:
    store = OrgContextStore(tmp_path)
    store.ensure_layout()
    staged = store.stage(_document(4), source="future-mesh")

    assert staged.path.parent == tmp_path / "org" / "pending"
    assert staged.version == 4
    assert staged.publisher == "alicesmith"
    assert store.load_accepted() is None
    status = store.status()
    assert status["slot"] == "absent"
    assert status["pendingCount"] == 1
    assert status["pending"][0]["source"] == "future-mesh"


def test_failed_pending_metadata_write_does_not_expose_partial_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = OrgContextStore(tmp_path)
    real_replace = __import__("os").replace

    def fail_metadata(source: Path | str, target: Path | str) -> None:
        target_path = Path(target)
        if target_path.parent == store.pending_dir and target_path.suffix == ".json":
            raise OSError("injected pending metadata failure")
        real_replace(source, target)

    monkeypatch.setattr('hyprial.daemon.impl.org.store.os.replace', fail_metadata)
    with pytest.raises(OSError, match="injected pending metadata failure"):
        store.stage(_document(5), source="future-mesh")

    assert store.list_pending() == ()
    assert not tuple(store.pending_dir.glob("*.md"))
