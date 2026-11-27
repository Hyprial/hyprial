# Service grants contract

This document freezes what two devices on the same build exchange for service
grants: the grant document in OrgFS, the protected-path rule that makes its
author meaningful, the sealed credential inside it, the limits every reader
applies, and the log events and reason codes operators read. The design and its
reasons are in
[docs/design/service-grant-2026-11-27.md](../../docs/design/service-grant-2026-11-27.md).
The IPC view of the three methods is generated into
[contract/daemon-request/openapi.json](../daemon-request/openapi.json) and the
CLI flags into [contract/cli/surface.json](../cli/surface.json); this document
does not repeat them. Device row encryption keys are specified in
[contract/org-network](../org-network/README.md), the sidecar's `expose`
admission in [contract/sidecar-tailcat](../sidecar-tailcat/README.md).

There is no compatibility path for other document versions or other builds:
a reader refuses anything that does not match this contract exactly.

## Grant document (schemaVersion 1)

Path, in the org's directory space:

```text
directory/service-grants/<owner>/<grantId>.json
```

`<owner>` is the grantor's bare owner name, the same value as the owner segment
of `directory/devices/<owner>/...`; never a `user:<owner>` principal. The file
name is the document's `grantId` plus `.json`.

Every object is closed: a document with a missing or an extra key, at any
level, is invalid and skipped alone (other documents are still read).

| Field | Type | Rule |
| --- | --- | --- |
| `schemaVersion` | integer | exactly `1` |
| `grantId` | string | `sg.` followed by 26 characters of `[a-z2-7]`; equals the file name |
| `service` | object or null | see below; null only in a revoked tombstone |
| `service.deviceId` | string | identifier (see below); the grantor's own exposing device |
| `service.port` | integer | `1..65535`, never a boolean |
| `service.name` | string or null | `[a-z0-9][a-z0-9_-]{0,62}` (the service-connect service name rule); required when `credential` is not null |
| `service.protocol` | string | `tcp` or `http` (catalog protocol, byte-for-byte TCP); `tcp` when the grantor did not choose |
| `service.note` | string or null | at most 512 characters; no control (Unicode `Cc`) or surrogate (`Cs`) characters |
| `recipient` | object, `"@members"`, or null | null only in a revoked tombstone |
| `recipient.owner` | string | bare owner identifier, no `:` |
| `recipient.deviceId` | string | identifier |
| `grantedAtMs` | integer or null | non-negative Unix milliseconds of the first grant; unchanged by in-place updates; null only in a revoked tombstone |
| `updatedAtMs` | integer or null | non-negative Unix milliseconds of the last in-place update; null when the document was never updated in place |
| `expiresAtMs` | integer or null | non-negative Unix milliseconds; null means no expiry; later than the grantor's clock when written |
| `revokedAtMs` | integer or null | non-negative Unix milliseconds; null while not revoked |
| `credential` | object or null | one sealed envelope (see [Sealed credential](#sealed-credential)); always null when `recipient` is `"@members"` and when `revokedAtMs` is set |

Identifiers (`service.deviceId`, `recipient.owner`, `recipient.deviceId`, and
the `<owner>` path segment) are non-empty strings of at most 256 characters,
equal to their trimmed form, not `.` or `..`, not starting with `@` (selector
syntax), without `/`, `\` or `:`, and without control characters
(U+0000–U+001F, U+007F) or UTF-16 surrogates. The org is named by its
directory-space name, `group-<name>`.

Meaning:

- **Effective**: `revokedAtMs` is null and `expiresAtMs` is null or greater than
  the reader's clock (`expiresAtMs <= now` is expired). Each device judges
  expiry with its own clock.
- **Revoked tombstone**: `revokedAtMs` set and `credential` null. When the
  grantor's own document could not be parsed at revocation, the tombstone keeps
  each field that still validates; `service`, `recipient` and `grantedAtMs` may
  then be null. A tombstone is never effective and is listed as revoked.
- **One effective grant per (exposing device, port, recipient)**: granting
  again while one is effective rewrites that document in place (same
  `grantId`; new `credential`, `expiresAtMs`, `service.name`,
  `service.protocol`, `service.note`; `grantedAtMs` kept; `updatedAtMs` set).
  Otherwise a new `grantId` is minted.
- **`@members`**: every member device of this org, as defined by the exposing
  side (see the design's Exposing side section); a recipient treats it as
  addressed to itself. A document with `"@members"` and a non-null
  `credential` is invalid.

## Protected section `service-grants`

- The section name `service-grants` is one of the protected directory sections.
  Its document ids and tree nodes are built the same way as those of `devices`.
- **Author equals path subject.** Every replica accepts a create or update of
  `directory/service-grants/<owner>/...` (tree node or document) only when the
  update's author is `user:<owner>`. The rule depends only on the document id
  and the author, not on the content.
- **The parent is owner-created.** `directory/service-grants` (two path
  segments) belongs to the space owner and is in the owner-created directory
  list next to `directory`, `directory/devices`, `directory/people`,
  `directory/invites` and `directory/leaves`. `hyprial org create` creates it
  for a new org; for an existing org the space owner's daemon creates it on its
  next device-row publish. Members cannot create it. Until it exists,
  `service.grant` fails with `SERVICE_UNAVAILABLE` and the message
  `org service-grants directory is not initialized yet; it is created the next
  time the org owner's upgraded daemon publishes its device row`.
- The rule assumes the update's author is genuine; authors are not signed.
- A replica on a build that does not know the section refuses its documents,
  and refuses a non-owner member's `directory/service-grants/<owner>` tree node.

## Sealed credential

Profile name `service-grant`. The envelope is closed JSON with exactly three
keys and no signature:

```json
{
  "header": {"v": 1, "alg": "A256GCM", "wrap": "X25519-HKDF-SHA256-A256GCM",
             "org": "group-acme", "grantId": "sg.abcdefghijklmnopqrstuvwxyz",
             "service": {"owner": "alice", "deviceId": "device-a", "port": 8447},
             "recipient": {"owner": "bob", "deviceId": "device-b"},
             "encryptionKeyId": "x25519:<32 lowercase hex>"},
  "recipients": [{"recipientKeyId": "x25519:<32 lowercase hex>", "epk": "<base64>",
                  "nonce": "<base64>", "wrappedDek": "<base64>"}],
  "payload": {"nonce": "<base64>", "ciphertext": "<base64>"}
}
```

Header (exact key set `v, alg, wrap, org, grantId, service, recipient,
encryptionKeyId`):

| Key | Value |
| --- | --- |
| `v` | integer `1` |
| `alg` | `A256GCM` |
| `wrap` | `X25519-HKDF-SHA256-A256GCM` |
| `org` | the org's directory-space name (`group-<name>`), as passed to `service.grant` |
| `grantId` | the document's `grantId` |
| `service` | exactly `{owner, deviceId, port}`: grantor bare owner from the document path, `service.deviceId`, `service.port` |
| `recipient` | exactly `{owner, deviceId}`: the document's recipient |
| `encryptionKeyId` | the recipient device row's `encryptionKeyId`, derived from the public key sealed to |

`recipients` holds exactly one row. `recipientKeyId` equals the header's
`encryptionKeyId`. Encodings are standard base64 with padding that round-trips
exactly: `epk` 32 bytes, both `nonce`s 12 bytes, `wrappedDek` 48 bytes,
`ciphertext` 17 to 16400 bytes (a 1 to 16384 byte secret plus the 16-byte tag).

Canonical header bytes: bounded canonical JSON of the whole header, keys sorted
at every level, no whitespace, UTF-8, integers only. The fixed test vector uses
org `org-a`, service `alice`/`device-a`/`8447` and recipient `bob`/`device-b`;
its canonical header is (key id elided):

```text
{"alg":"A256GCM","encryptionKeyId":"x25519:…","grantId":"sg.abcdefghijklmnopqrstuvwxyz","org":"org-a","recipient":{"deviceId":"device-b","owner":"bob"},"service":{"deviceId":"device-a","owner":"alice","port":8447},"v":1,"wrap":"X25519-HKDF-SHA256-A256GCM"}
```

Construction (`H` = canonical header bytes, `kid` = recipient key id as ASCII,
`R` = recipient public key):

1. Draw a 32-byte data key `dek` and a 12-byte payload nonce.
   `ciphertext = AES-256-GCM(dek, nonce, secret, aad = H)`.
2. Draw an ephemeral X25519 key; `epk` is its public key.
   `shared = X25519(ephemeral, R)`; an all-zero shared secret is refused.
3. `kek = HKDF-SHA256(ikm = shared, salt = epk || R, info =
   "hyprial/service-grant/wrap/v1" || 0x00 || H || 0x00 || kid, length = 32)`.
4. Draw a 12-byte wrap nonce;
   `wrappedDek = AES-256-GCM(kek, wrap nonce, dek, aad = H || 0x00 || kid)`.

Entropy is drawn in the order data key, payload nonce, ephemeral key, wrap
nonce. The fixed vector that pins this construction independently of the
implementation is `test_seal_matches_independent_fixed_vector` in
`tests/identity/sealed_envelope/test_service_grant_envelope.py` (synthetic keys
only).

Binding and refusals:

- Sealing refuses (`INVALID_ARGUMENT`) a recipient key that is not 32 bytes, is
  all-zero or low-order, or whose id does not match; an empty or over-16384-byte
  secret; and any header field outside the rules above.
- Opening rebuilds the expected header from the document being read (org,
  `grantId`, grantor from the path, `service.deviceId`, `service.port`,
  recipient, and the opener's own key id) and refuses unless the envelope's
  header is exactly that. Moving a ciphertext into another document, to
  another recipient, port, org or key fails.
- Every opening failure is one value-free `PAYLOAD_INVALID`. No refusal carries
  key material, plaintext or parser text.
- The provider-key profile shares the primitive but has its own wrap domain and
  a signature; an envelope of one profile never opens as the other.

## Limits

| Limit | Value | Applies to | When exceeded |
| --- | --- | --- | --- |
| Documents per grantor directory | 256 | every reader, per org | that grantor is unknown: the recipient keeps its previous answer for it; the exposing side admits nothing for that org (step `grants-limited`); `service.grants` names it in `limitedGrantors` |
| Listed bytes per grantor directory | 512 KiB | every reader, per org | same |
| Grantor directories per org | 1024 | every reader | the whole org is unknown for that pass |
| Periodic scan budget | 2.0 s per org per pass | recipient reconcile | the whole org is unknown for that pass |
| Startup check budget | 0.2 s in total | recipient startup check | the answer is "do not start" |
| New grant ids per grantor per org | refused at 240 documents, after cleanup | `service.grant` | `SERVICE_UNAVAILABLE` with a message naming the count and the way out; in-place updates are not limited |
| Cleanup age | 7 days after `revokedAtMs` or `expiresAtMs` | the grantor's own documents, on each grant or revoke | — |
| Admission keys per port | 4096 | exposing side push | the port keeps its previous set |
| `service.note` | 512 characters | document | invalid document |
| Sealed secret | 1..16384 bytes | envelope | refused when sealing |

"Unknown" is never "no grant": an unknown answer neither admits nor withdraws.

## Log events

All events are emitted through the daemon log; none carries a secret,
ciphertext, plaintext, or a parser or directory error text. Component is
`service` unless stated.

| Event | Level | Fields | Emitted by |
| --- | --- | --- | --- |
| `service.grant.created` | info | `org, grantId, grantor, serviceDeviceId, port, recipient, hasCredential, expiresAtMs` | grantor, new `grantId` |
| `service.grant.updated` | info | same as `created` | grantor, in-place update |
| `service.grant.revoked` | info | `org, grantId, grantor, revokedAtMs, tombstone` | grantor |
| `service.grant.cleaned` | info | `org, grantor, grantIds` | grantor, after a grant or revoke deleted long-dead documents |
| `service.grant.cleanup_failed` | warn | `org, grantor, grantId, errorCode` (`grantId` null when the grantor's documents could not be listed) | grantor; the grant or revoke itself still succeeded |
| `service.grant.received` | info | `org, grantId, grantor, entryId` | recipient, key stored or replaced |
| `service.grant.rejected` | warn | `org, grantId, grantor, reason` | recipient, once per grant and reason |
| `service.grant.withdrawn` | info | `org, grantId, grantor, reason` | recipient, once when a grant's key entry is removed or the grant stops being offered to service-connect |
| `service.grant.scan_limited` | warn | `org, phase, reason`, plus `grantor` for a per-grantor limit | recipient; `periodic`: when a limit starts or its reason changes; `startup`: once per check, so up to once every 60 s while service-connect is not running |
| `service.grant.recheck_failed` | warn | `errorType` | recipient, an activation recheck failed |
| `service.grant.key_unavailable` | warn | `org, port, grantId, recipient, reason` | exposing side; once per `(org, grantId, reason)` until it changes or recovers |
| `service.grant.directory_unavailable` | warn | `org` (empty for `orgs`), `step` | exposing side; once per `(org, step)` until it recovers |
| `service.grant.roster_unreadable` | warn | `code` | exposing side |
| `service.grant.admission_limit` | warn | `port, count, limit` | exposing side |
| `service.grant.admission_unavailable` | warn | `errorType` | exposing side, unexpected derivation failure |
| `service.grant.admission_trigger_failed` | warn | `method, reason`, plus `errorType` when `reason` is `failed` | exposing side |
| `service.grant.admission_tick_deferred` | warn | `errorType` | exposing side |
| `service.grant.unexpose_revoke_failed` | warn | `port, org, code` (`org` is `*` when the whole step failed) | exposing side |
| `network.exposure.admission_rejected` | error | `port, code` | exposing side, push not confirmed by the sidecar |
| `org.directory_roots.failed` | warn, component `org` | `org, errorType, code` | org owner, creating the parent directory |

`recipient` in `created`/`updated` is `@members` or `<owner>/<deviceId>`;
in `key_unavailable` it is an object, `{owner, deviceId}`, or
`{"recipient": "@members"}` for a `@members` grant.

A `withdrawn` grant that only became unreadable is not reported. A key entry
withdrawn after a restart is reported then; a mapping that came down while the
daemon was stopped is not.

## Reason codes

`service.grant.rejected.reason`:

| Code | Meaning |
| --- | --- |
| `unnamed` | the grant has no `service.name`, so it cannot become a service entry |
| `service-device-unknown` | `service.deviceId` has no usable device row under the grantor |
| `service-device-invalid` | the device row or port fails service-connect validation |
| `device-conflict` | the same `deviceId` maps to different keys, addresses or generations across sources |
| `entry-invalid` | the derived catalog entry fails validation (for example the note) |
| `name-conflict` | both `<name>` and `<name>-<grantor>` are taken |
| `credential-unopenable` | the ciphertext is not sealed to this device and this grant |
| `key-unavailable` | this device has no usable encryption key right now |
| `entry-exists` | an entry `grant.<id>` exists that this daemon did not write |
| `entry-modified` | the `KEY` value of an entry this daemon wrote was changed by the user |

`service.grant.withdrawn.reason`:

| Code | Meaning |
| --- | --- |
| `revoked` | the grantor revoked the grant |
| `expired` | the grant expired by this device's clock |
| `credential-cleared` | the grant is still offered but no longer carries a key |
| `grantor-device-gone` | the grantor's device row for `service.deviceId` is gone |
| `left-org` | this device left the org |
| `not-org-member` | this device is no longer a member of the org |
| `org-deleted` | the org was deleted |
| `grant-removed` | the grant document is gone |
| any `service.grant.rejected` code | the grant now fails that check (for example `device-conflict`) |

`service.grant.scan_limited`: `phase` is `startup` or `periodic`; `reason` is
`documents`, `bytes` (per grantor, with `grantor`), `grantors` or `deadline`
(whole org).

`service.grant.key_unavailable.reason`:

| Code | Meaning |
| --- | --- |
| `member-not-in-roster` | the recipient is not an active member of the org's tailnet roster |
| `member-without-local-key` | the member has no `clientPublicByServer` entry for this device's server |
| `roster-unavailable` | the roster, or this tailnet in it, cannot be read or is not ready |
| `tailnet-unresolved` | the org's tailnet binding cannot be resolved |
| `key-malformed` | the roster key is not `nodekey:` plus 64 lowercase hex digits |
| `not-org-member` | sub-org on an inherited tailnet: the recipient is in the parent's roster but not a device of this org |
| `org-devices-unavailable` | sub-org on an inherited tailnet: this org's own device directory cannot be read |

`service.grant.directory_unavailable.step`: `orgs` (the device's orgs cannot be
listed), `grants` (an org's grant directory cannot be read, including an
ambiguous directory space), `grants-limited` (this device's own grants in the
org are over a reader bound, so they count as unreadable), `tailnet` (grants
read, tailnet binding not resolved), `devices` (a sub-org on an inherited
tailnet: its own device directory cannot be read). Every step closes the org's
grants on this device.

`service.grant.admission_trigger_failed`: `method` is `service.grant`,
`service.revoke`, `network.expose` or `sidecar-installed`; `reason` is `failed`
or `unscheduled`.

## Files on the recipient

| Path (under `HYPRIAL_HOME`) | Mode | Content |
| --- | --- | --- |
| `secrets/providers/grant.<26 base32>.json` | 0600 | user-provider entry with field `KEY` |
| `state/service-grants/consumer-ledger.json` | 0600 | which entries and connections this daemon derived from which grant, with digests only (never the key) |

The entry name prefix `grant.` and the field name `KEY` are fixed.
