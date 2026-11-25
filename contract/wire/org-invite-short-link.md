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

## Invite pickup: `org.invites.list` / `org.invites.accept` (0.5.5.3, O2c)

Login no longer accepts every account invite (D6 deleted). The pickup is
declared in `contract/daemon-request/openapi.json` and the examples are frozen
in `contract/fixtures/org-membership/invites.json`.

Ownership boundary: the **shell** reads the signed-in account
(`hyprialInvites`) because the access token lives in `secrets/login.json` and
the daemon must never read that file (T11). The **daemon** decides what this
device still owes an answer and performs the join through the org network
service `org.join` already uses. The account snapshot is a parameter; the
credential never crosses the boundary.

```jsonc
// org.invites.list  (read; http_exposed=true, projection only)
params  { "invites": [ { "payload": "<page-recorded payload>", "acceptedAt": "<iso8601|null>" } ] }
result  { "ok": true,
          "pending": [ { "inviteId", "org", "inviter", "spaceId", "acceptedAt" } ],
          "settled": <int>, "duplicates": <int>, "warnings": [ <string> ] }

// org.invites.accept  (write; http_exposed=false, retry=idempotency-key, key=inviteId)
params  { "inviteId": "inv-…", "link": "v2.<payload>" }
result  { "ok": true, "inviteId", "org", "spaceId",
          "joined": <bool>, "alreadyMember": <bool>, "steps": [ { "kind", "ok", "detail" } ] }
```

Rules the daemon enforces:

- `inviteId` is `inv-` plus the first 20 hex digits of SHA-256 over the
  complete payload JSON (sorted keys, compact separators). It includes the
  bearer, expiry, authority and workflow. URL wrapping does not change it.
  A replacement bearer cannot reuse another confirmation (`INVALID_ARGUMENT`).
  It binds the confirmation; it is not authorization evidence.
- `org.list` is not completion evidence. Before reporting `alreadyMember` or
  settling an invite, read back the exact directory and ACL space IDs, local
  membership in both, the directory-to-ACL binding (including ownership), the
  valid ACL policy, and this device's directory row. A directory-only join is
  not membership completion, even when an org listing says `active`.
  Membership read-back and resume require the access granted by the invitation
  producer's shared policy: directory `rw`, ACL `ro`. Higher access (ACL `rw`
  or space ownership) satisfies the grant; missing membership or directory `ro`
  does not. Verification never promotes an ACL member to `rw`.
- An existing verified membership answers `joined=false, alreadyMember=true`.
  An incomplete acceptance checkpoint keeps the invite pending until every
  bootstrap step completes. Pending partial rows include `status="partial"`
  and `missingStep`. Left orgs remain settled without rejoining; only explicit
  `hyprial org join` can undo a deliberate leave.
- Concurrent accepts of the same `inviteId` on one home share a single flight
  across IPC worker threads and return the same result or failure. A waiter
  has a 120-second bound; expiry returns `no-holder-online` with partial
  `accept-in-flight` data, never starts a competing join.
- A private local `org-invites/<sha256(inviteId)>.json` checkpoint records
  `started`, Tailnet redemption/announcement, each bootstrap effect, separate
  directory/ACL commits and binding, then `done` with the result. It contains
  no bearer and is not a membership authority. File and directory are synced
  before proceeding. Retry reads back committed effects and resumes missing
  steps. Tailnet redemption reuses its existing durable remote operation ID.
  Completed results are replayed only after live read-back; missing membership
  after completion is a partial failure, not permission to resurrect it.
- Failed acceptance preserves the original error code. Its error data adds
  `status="partial"`, `missingStep`, and `inviteId`; it never becomes a success
  or removes the pending invite. Login/pending failed rows retain the safe
  status and missing step. The invite's expiry check still applies to retries.
- Two account rows naming one space are one candidate; the other is counted
  in `duplicates`. An unfinished acceptance takes priority over another invite
  for that space, regardless of account row order; a verified duplicate cannot
  hide the unfinished operation.
- A row the account recorded without a payload is reported to the operator and
  is not sent to the daemon; an unreadable payload stays a candidate with
  `org/inviter/spaceId = "unknown"` and fails on its own.

Never in a result or an event: the payload, the access token, the refresh
token. `org.invite.pending` carries `count` and `orgs` only.

Declining is a device decision made in the shell: **a decline is not a
withdrawal**. Nothing writes the account property, so `declined` only means
"this device said no in this run".

`org.join` is a MethodSpec bound to the existing join service, not a legacy
router branch. Its parameters and result remain `{link}` and
`{org, spaceId, steps}`. It is mutating, local/bound-agent only, not HTTP
exposed, and does not permit automatic retries. Shipped OrgFS lower-kebab
and sidecar uppercase error codes keep their spelling. OrgFS details remain
a map of primitive values or primitive arrays; unknown codes still fail the
registry check. `org.invites.accept` uses the same error vocabulary.

Login (including first-time daemon initialization) never requests bulk
acceptance. TTY prompts go to stderr; JSON/non-TTY pickup only reports.
Only `org pending --accept-all` accepts in bulk. Pickup failures and interrupts
after the identity commit return a separate `pending` result, not a failed or
rolled-back login. Transport exception text is not echoed in that result.
If an org is left between listing and accepting, accept refuses it; explicit
`org join` is required to return.
