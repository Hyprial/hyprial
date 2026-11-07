# Corrupted organization context (redacted sample, 2026-10-16 incident)

This fixture reproduces the second defect from the same incident: a flow
mapping was wrapped across lines and its values carried bare colons, so the
YAML block no longer parses.  Names and duties are redacted; the structure is
unchanged.

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
  entries:
    - {dept: dept-b, head: carol, duty: coordinates; sub-departments:
      backend: infra
      frontend: apps}
norms:
  autonomy: [x]
  escalation: y
  etiquette: z
residents: []
```
