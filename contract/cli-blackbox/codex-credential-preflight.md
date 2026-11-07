# Interactive Codex credential preflight contract

Before launching either interactive Codex process, an agent-home P2 native
OpenAI launch validates the effective `auth.json`. The daemon's agent-home
authority performs this check before runtime-home materialization and the Codex
native preparer, so an unusable canonical path or configured shared target
cannot be replaced by a generic home-access failure. A refusal uses exit status
1 and this JSON envelope:

```json
{
  "ok": false,
  "code": "CODEX_CREDENTIAL_MISSING",
  "error": "human-readable diagnostic",
  "data": {
    "reason": "missing",
    "path": "/absolute/effective/auth.json",
    "remediation": {
      "kind": "command",
      "command": "hyprial agent provider login codex --actor worker"
    }
  }
}
```

`data.reason` and remediation are case-specific:

| reason | remediation |
| --- | --- |
| `missing` | `kind=command`; provider login without `--force` |
| `empty` | `kind=command`; provider login with `--force` |
| `permissions` | `kind=command`; `chmod 600` for `data.path` |
| `not-regular` | `kind=manual`; names the path and does not recommend provider login |
| `shared-target` | `kind=manual`; names the external target and `config.sharedCredentials.codex`, and does not recommend provider login |
| `inaccessible` | `kind=manual`; names the path and requires access repair |

A command remediation must repair that classified state so the next preflight
passes, assuming the operator's native Codex login source exists. A manual
remediation must state that no hyprial CLI command can safely repair the state.
After a shared target becomes a non-empty 0600 regular file, the designated
`auth.json` link is accepted by the Codex native inventory and launch advances.
The diagnostic never reads or emits credential content.
