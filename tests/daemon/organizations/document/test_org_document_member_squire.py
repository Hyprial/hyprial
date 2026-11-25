# Decision record (S3a red tests R5; no inline comments by repository rule):
# - The member key squire leaves the org document schema.  A member that
#   carries squire with a value the old schema refused (a number) validates
#   after the removal because the key is no longer read; a member with a
#   string squire keeps validating and keeps its source bytes, so an old
#   document still opens and serializes unchanged.
"""The org document member record has no squire field."""

from __future__ import annotations

from hyprial.daemon.impl.org.document import (
    parse_document,
    serialize_document,
    validate_document,
)

TEMPLATE = """# Org context

Body.

```yaml
meta:
  version: 3
  issued_at: 2026-10-16
  publisher: alice
lines:
  - {line: alpha, owner: bob}
members:
  - {username: bob, display: Bob, squire: SQUIRE}
routing: []
norms:
  autonomy: [x]
  escalation: y
  etiquette: z
residents: []
```
"""


def test_a_non_string_squire_is_not_validated() -> None:
    result = validate_document(TEMPLATE.replace("SQUIRE", "42"))

    assert result.ok is True, result.issue


def test_a_string_squire_still_opens_and_serializes_unchanged() -> None:
    source = TEMPLATE.replace("SQUIRE", "agent:bob:node:squire")

    assert validate_document(source).ok is True
    assert serialize_document(parse_document(source)) == source
