# `hyprial org invite` short-link field

The org invite JSON result keeps every existing daemon field and adds one
shell-owned field:

```json
{
  "ok": true,
  "link": "https://invite.example/join#v1...",
  "deepLink": "hyprial://join#v1...",
  "command": "hyprial org join '...'",
  "invite": "hyprial-invite:v1:...",
  "expiresAt": "2026-10-04T00:00:00Z",
  "short": "https://hyprial.ai/j/<id>"
}
```

`short` is `null` when the short link could not be minted.  The long `link`
and all other fields are always present; the command still exits 0.

The shell posts the exact long-URL UTF-8 bytes as the plain-text body to
`https://hyprial.ai/j` with the current Casdoor access token
(`Authorization: Bearer ...`).  The service owns the TTL.  Success is only
HTTP 201, and the response's `targetSha256` must equal
`sha256(long-url-bytes)`; the response `short` is accepted only then.
Redirects are never followed.  The whole operation has a 3-second budget.

## Degradation reason codes

On stderr (one line, and `short: null`), the shell may report:

| Code | Meaning |
|---|---|
| `SHORT_LINK_NOT_LOGGED_IN` | no usable login credential |
| `SHORT_LINK_TIMEOUT` | total 3s budget expired (credential/discovery/refresh/POST/read) |
| `SHORT_LINK_TOO_LARGE` | `/j` response body exceeded 4 KiB |
| `SHORT_LINK_TARGET_MISMATCH` | 201 `targetSha256` missing or not the long URL digest |
| `SHORT_LINK_REDIRECT_REFUSED` | `/j` returned 3xx or the final URL changed |
| `SHORT_LINK_FAILED (<Type>)` | any other contained failure; only the class name is exposed |

A short link is itself a credential: the human output keeps the
"私发，勿贴群" warning and never logs the access token or the raw transport
exception.
