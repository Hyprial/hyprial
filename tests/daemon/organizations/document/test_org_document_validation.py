"""One validation entry point for the structured part of org-context.md.

The corrupt fixtures are redacted copies of the 2026-10-16 production incident
(an escaped brace before a flow mapping, and a flow mapping wrapped across
lines with bare colons).  Each test pins the exact file line and column the
validator reports, so an operator's error message names the byte to fix.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from hyprial.daemon.impl.org.document import (
    OrgDocument,
    OrgDocumentError,
    OrgMeta,
    parse_document,
    validate_document,
)
from hyprial.daemon.impl.org.store import OrgContextStore, OrgStoreError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ESCAPED_BRACE = FIXTURES / "org-context-corrupt-escaped-brace.md"
MULTILINE_FLOW = FIXTURES / "org-context-corrupt-multiline-flow.md"

VALID = """# Org context

Body.

```yaml
meta:
  version: 3
  issued_at: 2026-10-16
  publisher: alice
lines:
  - {line: alpha, owner: bob}
members: []
routing: []
norms:
  autonomy: [x]
  escalation: y
  etiquette: z
residents: []
```
"""


def test_validate_reports_escaped_brace_with_file_line_and_column() -> None:
    source = ESCAPED_BRACE.read_text(encoding="utf-8")

    result = validate_document(source)

    assert result.ok is False
    assert result.document is None
    assert result.issue is not None
    assert result.issue.line == 20
    assert result.issue.column == 33
    assert "mapping values are not allowed here" in result.issue.reason
    # The reported line is the line an editor must fix.
    assert "\\{dept:" in source.splitlines()[result.issue.line - 1]


def test_validate_reports_multiline_flow_mapping_with_file_line_and_column() -> None:
    source = MULTILINE_FLOW.read_text(encoding="utf-8")

    result = validate_document(source)

    assert result.ok is False
    assert result.issue is not None
    assert result.issue.line == 19
    assert result.issue.column == 69
    assert "expected" in result.issue.reason
    assert "got ':'" in result.issue.reason


def test_validate_accepts_a_well_formed_document() -> None:
    result = validate_document(VALID)

    assert result.ok is True
    assert result.issue is None
    assert result.document is not None
    assert result.document.meta.version == 3
    assert result.document.meta.publisher == "alice"


def test_validate_returns_an_issue_instead_of_raising_for_non_text() -> None:
    result = validate_document(None)  # type: ignore[arg-type]

    assert result.ok is False
    assert result.issue is not None
    assert result.issue.reason


def test_validate_reports_an_impossible_yaml_date_with_file_location() -> None:
    """An impossible YAML date is a located failure, never a raw ValueError."""

    source = VALID.replace("2026-10-16", "2026-99-99")

    result = validate_document(source)

    assert result.ok is False
    assert result.document is None
    assert result.issue is not None
    assert result.issue.line == 8
    assert result.issue.column == 14
    assert "month" in result.issue.reason
    assert "issued_at" in source.splitlines()[result.issue.line - 1]


def test_parse_document_names_the_line_for_an_impossible_yaml_date() -> None:
    source = VALID.replace("2026-10-16", "2026-99-99")

    with pytest.raises(OrgDocumentError, match=r"line 8, column 14"):
        parse_document(source)


def test_parse_document_names_the_line_on_corrupt_input() -> None:
    source = ESCAPED_BRACE.read_text(encoding="utf-8")

    with pytest.raises(OrgDocumentError, match=r"line 20, column 33"):
        parse_document(source)


def test_store_refuses_to_write_a_document_with_corrupt_bytes(tmp_path: Path) -> None:
    # A caller can build an OrgDocument around corrupt source bytes without
    # parse_document; the write path must re-validate and refuse before writing.
    source = ESCAPED_BRACE.read_text(encoding="utf-8")
    document = OrgDocument(
        source=source,
        data={"meta": {"version": 1}},
        meta=OrgMeta(version=1, issued_at=date(2026, 10, 16), publisher="alice"),
        markdown="body",
    )
    store = OrgContextStore(tmp_path)

    with pytest.raises(OrgStoreError, match=r"line 20"):
        store.adopt(document)
    with pytest.raises(OrgStoreError, match=r"line 20"):
        store.stage(document, source="node-a")

    assert not (tmp_path / "org-context.md").exists()
