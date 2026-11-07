# Corrupted organization context (redacted sample, 2026-10-16 incident)

This fixture reproduces the escaped-brace defect that stopped a production
daemon: one department entry was written as `\{dept: ...}` (a residual
backslash before the flow-mapping brace).  Names and duties are redacted; the
structure is unchanged.

```yaml
meta:
  version: 13
  issued_at: 2026-10-16
  publisher: alice
lines:
  - {line: alpha, owner: bob}
members: []
routing: []
departments:
  note: short-term form
  entries:
    - \{dept: dept-a, supervisor: alice, head: bob}
    - {dept: dept-b, head: carol}
norms:
  autonomy: [x]
  escalation: y
  etiquette: z
residents: []
```
