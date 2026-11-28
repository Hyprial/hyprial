# Service grants: sharing one exposed service with chosen org devices

- Date: 2026-11-27
- Status: accepted design, implemented on the service grant branch. Not yet
  verified on two real machines with real sidecars (see
  [Not verified on two real machines](#not-verified-on-two-real-machines)).
- Operator guides: [upgrade an existing org](../guides/migration-service-grants-upgrade.md),
  [recipient guide](../guides/service-grants-recipient.md),
  [default-deny exposures](../guides/migration-service-exposure-default-deny.md).
- Wire contract: [contract/service-grants](../../contract/service-grants/README.md).
- Terms follow [GLOSSARY.md](../../GLOSSARY.md) (`service grant`, and how it
  differs from `agent service secret grant`).

Code and tests cite this document by section name. The five sections they cite
are [Data](#data), [Trust](#trust), [Exposing side](#exposing-side),
[Sealing](#sealing) and [Recipient side](#recipient-side); keep those headings
stable.

## Problem

Alice runs a service on her device, for example a model gateway on port 8447.
She wants Bob's device, in the same org, to use it: only Bob's device, with the
ability to take it back at any time. When the service needs an API key, the key
travels with the grant.

Before this design, an exposed port admitted every active member of the
sidecar's tailnet, and a recipient had to receive two hand-placed records (a
catalog entry and `service-access.json`) to connect. Neither step said who may
use the service, and there was no channel for the key.

## Decisions

| # | Decision | Consequence |
| --- | --- | --- |
| D1 | A grant is Alice's policy over her own service, written in a document under her own name in the org's OrgFS directory space. It does not go into an org cell and is not copied by the org owner. | Granting and revoking never wait for the org owner. |
| D2 | Alice edits her own documents to grant, renew, change the key or revoke; the change takes effect on her next derivation. The org owner's lever is removing a member or device from the org; there is no per-grant veto. | No owner workflow; removal from the org is the override. |
| D3 | A recipient device publishes its own encryption public key on its directory device row; Alice reads it from there. It is not copied into the roster. | A device without the key can be granted access but cannot receive a key. |
| D4 | The key is sealed to the recipient device's public key and stored inside the grant document. The envelope carries no signature and is not delivered over Zenoh. | The author of the grant is established by OrgFS, not by the envelope (see [Trust](#trust)). |
| D5 | On the recipient, the opened key is stored as an ordinary user-provider entry. No new secret store and no new `SecretSource` value. | The recipient binds it to an agent with the existing `agent secret grant`. |
| D6 | The service address is not delivered. The recipient builds its mapping from the grant document plus the grantor's directory device row. | Address or key changes on the grantor's device follow automatically. |
| D7 | An exposure with no effective grant admits nobody. Keeping "every member may connect" takes one explicit `@members` grant per org. | Upgrading closes existing exposures until grants are written (see the upgrade guide). |
| D8 | Renewing a grant or changing its key updates the existing document in place: same `grantId`, same recipient entry name. | Agent bindings on the recipient survive renewal. |
| D9 | Revoked and expired documents are deleted automatically 7 days later; each grantor holds at most 256 documents per org and is refused new grants at 240. | The grant directory cannot grow without bound. |
| D10 | `@members` admits only devices that are members of that org, expanded to an explicit key set. | An `@members` grant in one org never opens the port to another org that shares the sidecar. |

The rule that cell documents are the single source of member policy covers the
org's policy over its members. A grant is a member's policy over their own
service, so it is outside that rule.

## Data

### Device row: encryption public key

A directory device row (`directory/devices/<owner>/<deviceId>.json`) may carry
the paired optional keys `encryptionKeyId` and `encryptionPublicKey`: both or
neither. `encryptionPublicKey` is the 32 raw bytes of the device's X25519
public key (key kind `user`) in standard base64; `encryptionKeyId` is
`x25519:` followed by the first 32 lowercase hex digits of the SHA-256 of those
bytes. A row with only one of them, a malformed value, an id that is not the
digest of the key, or a low-order key is invalid as a whole. The row's key set
stays closed. The full rule is in
[contract/org-network](../../contract/org-network/README.md).

A daemon creates and publishes the key once it is serving with a committed
account (`owner`, `identityIssuer`, `userId`) and a device record that has a
`deviceUid` and belongs to that account. A device without these publishes a
row without the pair, which remains valid. The private key never leaves the
device (`<state>/identity/encryption-keys/<slug>/key.json`).

### Grant document

Path: `directory/service-grants/<owner>/<grantId>.json` in the org's directory
space, where `<owner>` is the grantor's bare owner name (never `user:<owner>`)
and `grantId` is `sg.` followed by 26 lowercase base32 characters (`a-z`, `2-7`).
The document is closed JSON at every object level:

```json
{
  "schemaVersion": 1,
  "grantId": "sg.abcdefghijklmnopqrstuvwxyz",
  "service": {"deviceId": "device-a", "port": 8447, "name": "magpie",
              "protocol": "http", "note": null},
  "recipient": {"owner": "bob", "deviceId": "device-b"},
  "grantedAtMs": 1764200000000,
  "updatedAtMs": null,
  "expiresAtMs": null,
  "revokedAtMs": null,
  "credential": null
}
```

- `service.deviceId` is always the grantor's own device: a grant can only be
  written for a port that this device currently exposes.
- `recipient` is one device `{owner, deviceId}` or the string `"@members"`.
  An `@members` grant never carries a `credential`; a reader drops such a
  document as invalid.
- `credential` is null or one sealed envelope (see [Sealing](#sealing)).
- `service.name` is required whenever `credential` is present: a recipient can
  only store a key against a named service entry. A grant without either only
  admits; the recipient gets no service entry from it.
- `grantedAtMs` is the time of the first grant and does not change on an
  in-place update; `updatedAtMs` is the time of the last in-place update, null
  until there is one.
- Effective means `revokedAtMs` is null and `expiresAtMs` is null or later than
  the reader's clock. There is exactly one implementation of this check, shared
  by the listing, the exposing side and the recipient side.

Field formats are in the [contract](../../contract/service-grants/README.md).

### Lifecycle of a document

- **Grant.** When the grantor has no effective grant for the same
  (exposing device, port, recipient), `service.grant` mints a new `grantId`.
- **Renew or change the key (in place).** When such an effective grant exists,
  `service.grant` rewrites that document: `credential`, `expiresAtMs`,
  `service.name`, `service.protocol` and `service.note` take the new values,
  `grantedAtMs` is kept and `updatedAtMs` is set. The `grantId` does not change,
  so the recipient's key entry name does not change either. The method returns
  `{grant, updated: true}` and logs `service.grant.updated`; a new grant returns
  `updated: false` and logs `service.grant.created`. Running the same command
  without `--credential` clears the key from that grant.
- **Revoke.** `service.revoke` sets `revokedAtMs` and clears `credential`; the
  document stays, so a recipient can tell "revoked" from "not replicated yet". A
  document that can no longer be parsed is replaced by a revoked tombstone that
  keeps whichever fields still validate; in a tombstone `service`, `recipient`
  and `grantedAtMs` may be null.
- **Unexpose.** A successful `network.unexpose` revokes every grant this device
  issued for that port, in every org.
- **Cleanup.** Whenever the grantor writes a grant or revokes one, documents of
  that grantor in that org that have been revoked, or expired, for more than
  7 days are deleted. A failed deletion does not fail the write.
- **Limits.** One grantor holds at most 256 documents in one org (the limit
  every reader applies, see below). `service.grant` refuses to mint a new
  `grantId` once the grantor already holds 240 documents in that org after
  cleanup (`SERVICE_UNAVAILABLE`, with a message naming the count and the way
  out); in-place updates are not limited. A document that cannot be parsed is
  never deleted by cleanup. `service.grants` reports
  `documentCount` and `documentLimit` next to the grantor's own grants.

### Readers' bounds

Every reader of grant documents (the recipient's periodic pass and startup
check, and the exposing side's read of its own grants) applies the same bounds:
at most 256 documents and 512 KiB of listed size per grantor directory, at most
1024 grantor directories per org, a 2.0 second budget per org per periodic pass
and 0.2 seconds for the recipient's startup check. A grantor over a per-grantor
bound is unknown, not empty, and never treated as "no grant". The recipient
keeps its previous answer for that grantor; the exposing side, reading only its
own grants, counts them as unreadable and admits nothing for that org
(`directory_unavailable`, step `grants-limited`).

## Trust

Every OrgFS replica enforces the author rule of protected directory paths
itself, from the update's document id and author alone, independent of the
content. The `service-grants` section uses the default rule of the protected
directory: the author of `directory/service-grants/<owner>/...` must be
`<owner>`, the same rule as `directory/devices/<owner>/...`. Therefore Bob,
reading `service-grants/alice/...`, knows Alice wrote it, and Alice, reading
`devices/bob/...`, knows Bob published that encryption key.

Two qualifications:

1. **The author is assumed genuine, not proven.** The admission rule states
   that it assumes the update's author is genuine and that signatures are a
   separate prerequisite. No code binds an update's author to the sender's
   tailnet identity. The grant, and the encryption key it is sealed to, rest on
   that assumption. This is the same level of trust as directory device rows
   and member admission today; this design does not add a signature of its own
   (D4). Authenticating authors belongs to OrgFS.
2. **The parent directory belongs to the org owner.** Only the space owner can
   create `directory/service-grants`. A new org gets it from
   `hyprial org create`. For an existing org, the org owner's upgraded daemon
   creates it the next time it publishes its own device row (first publish
   right after its sidecar writes its address file, then at most every 10
   seconds). Until then a member's `service.grant` fails with
   `SERVICE_UNAVAILABLE` ("org service-grants directory is not initialized
   yet"). This is one-time; afterwards granting and revoking never involve the
   owner.

## Exposing side

The exposing device's daemon derives an admission key set for every port it
exposes and pushes it to its sidecar with `expose` (stdio protocol v7,
`admission: "keys"`, `keys: [...]`). The sidecar first requires tailnet member
admission, then requires the peer's per-server client key to be in that port's
set; a smaller set closes existing connections that are no longer admitted.

How the set is derived:

1. Read this owner's own grant documents in every org the device belongs to,
   under the readers' bounds. Keep effective grants whose `service.deviceId` is
   this device.
2. For each org, resolve its effective tailnet id (a sub-org inherits its
   parent's tailnet) and read the sidecar's membership roster for that tailnet
   with `membership-status`, once per derivation.
3. A device grant contributes the recipient's key: the roster member matching
   `(owner, deviceId)` with `state: "active"`, entry
   `clientPublicByServer[<this device's serverPublic>]`.
4. An `@members` grant contributes the same key for every active roster member
   of that org's tailnet that is also a member device of that org. For a sub-org
   that shares its parent's tailnet, the org's own directory decides membership,
   so members of the parent alone are not admitted; when that directory cannot
   be read, the sub-org's grants contribute nothing.
5. A port's set is the union over all orgs and grants. No grant means an empty
   set: nobody is admitted. User exposures no longer use `admission: "allow"`.

Properties:

- The set is derived, never stored: a restarted daemon derives the same set,
  and a restarted sidecar is first replayed with an empty set and then receives
  the derived one.
- Failing closed: when a grantor directory is over a bound, the directory or
  the roster cannot be read, or a recipient is not an active roster member or
  has no entry for this server, that grant contributes nothing. Each case is
  logged (`service.grant.directory_unavailable`,
  `service.grant.roster_unreadable`, `service.grant.key_unavailable`, with
  `key_unavailable` deduplicated per org, grant and reason), so "nobody was
  granted" can be told apart from "granted but no key yet". A roster refresh
  (about every 60 seconds in the sidecar) fills a missing entry; the next
  derivation picks it up.
- A set larger than 4096 keys is not pushed; the port keeps its previous set and
  `service.grant.admission_limit` is logged.
- Timing: `service.grant`, `service.revoke`, `network.expose` and a sidecar
  start each trigger an immediate derivation; otherwise the admission
  derivation runs at most every 30 seconds. A push the sidecar rejected is
  retried on the next derivation.
- `network.unexpose` first waits up to 15 seconds for an in-flight push for
  that port, then removes the port, then revokes the port's grants.
- The join port 7447 and the daemon's own Zenoh ingress are not user exposures
  and are not affected.

### Corrections

The first version of this design took the admission key from the recipient's
**directory device row**, `clientPublicByServer[<this serverPublic>]`. That
assumption was wrong: the publishing path always writes an empty map there, so
every device grant would have pushed `keys: []` and closed the port silently.
A test on a real OrgFS runtime confirmed it. The per-server client key is
derived inside the sidecar from the member's master key and the target server,
and only the sidecar's membership roster holds it. The design therefore reads
the key from the exposing device's own sidecar roster, as described above.

Consequences of the correction:

- D3 still holds for the **encryption** key used for sealing; the correction
  concerns only the **admission** key.
- The admission key comes from the exposing device's own sidecar, not from
  directory replication, so there is no "directory not replicated yet" delay
  for it. Instead, a device grant takes effect once the recipient is an active
  member of the exposing sidecar's roster with an entry for this server.
  `service.grant` still requires the recipient to have a directory device row.
- The equality "the key a peer presents on an inbound connection equals the
  roster's `clientPublicByServer[<this serverPublic>]`" is established by
  reading the sidecar source and is modelled by the test doubles; it has not
  been observed on two real machines.

## Sealing

The key is sealed with the construction provider keys already use, extracted
into one profile-parameterized primitive in identity:

- a random 32-byte data key; the payload is AES-256-GCM under that key with the
  canonical header as associated data;
- per recipient, an ephemeral X25519 key; HKDF-SHA256 over the shared secret
  with salt `epk || recipient public key` and info
  `wrap domain || 0x00 || canonical header || 0x00 || recipient key id`; the
  data key is wrapped with AES-256-GCM under the derived key, with associated
  data `canonical header || 0x00 || recipient key id`.

The provider-key profile stays signed and byte-for-byte unchanged. The
service-grant profile:

- is unsigned (no `sealedBy`), with wrap domain `hyprial/service-grant/wrap/v1`;
- has exactly one recipient;
- has header `{v: 1, alg: "A256GCM", wrap: "X25519-HKDF-SHA256-A256GCM", org,
  grantId, service: {owner, deviceId, port}, recipient: {owner, deviceId},
  encryptionKeyId}`. The header is the associated data and part of the HKDF
  info, so a ciphertext moved into another grant document, to another
  recipient, port or org, or relabelled for another key does not open;
- is opened only against a header rebuilt from the grant document being read,
  with the grantor taken from the document path; the envelope's own header is
  never trusted on its own;
- refuses all-zero, low-order and non-32-byte recipient keys when sealing.

The key's source is a user-provider entry already saved on the grantor's device
(`--credential <entryId>[:<fieldName>]`). The value is never taken from argv,
stdin or a request body, and never appears in logs, events or responses.

Why no signature is acceptable: the grant document's author is fixed by the
OrgFS author rule ([Trust](#trust)), and the envelope is bound to that document
by its header. The prerequisite is the first qualification under Trust: if an
OrgFS author could be forged, an attacker could also write a whole grant, so a
signature on the envelope alone would not close that gap.

## Recipient side

Bob's daemon looks, in every org it belongs to, for effective grants addressed
to this device or to `@members`.

**Connection.** A grant becomes a service entry when `service.deviceId` has a
valid device row under the grantor named by the document path. The entry takes
name, port, protocol and note from the grant and `serverPublic` and `address`
from the grantor's device row, the same content the hand-placed catalog entry
and `service-access.json` row would hold. It is a second source next to those
records, kept in memory only; `hyprial org service connect <name>` creates the
loopback mapping as before. A name that collides with an existing service
becomes `<name>-<grantor>`. A grant without `service.name` produces no entry.

**Key.** When the grant carries a `credential` sealed to this device, the daemon
opens it and stores it as the user-provider entry `grant.<grantId without
"sg.">`, field `KEY`, recorded in a private ledger
(`state/service-grants/consumer-ledger.json`). Because renewal and key changes
keep the `grantId` (D8), the entry name is stable: a changed ciphertext is
re-opened and only the `KEY` value is replaced; any other metadata the user
stored in the entry is kept. Bob binds the entry to an agent with
`hyprial agent secret grant`.

**Withdrawal.** When a grant stops applying (revoked, expired, credential
cleared, the grantor's device row gone, this device left the org or is no
longer a member, the org deleted, the document gone, or a check such as a
device conflict now failing), the daemon removes the entry, tears down the mapping and deletes the
key entry it wrote, and logs `service.grant.withdrawn` with a fixed reason. A
key entry whose value the user changed is no longer managed and is not deleted.
"Cannot read the directory" is never treated as "no grant": an unreadable org
or a grantor over a bound keeps the previous answer and withdraws nothing.

**Activation.** A device with neither `service-access.json` nor saved
connections starts service-connect at daemon start when a startup check
(budget 0.2 seconds) finds an effective grant addressed to it. If the first
grant arrives after startup, the daemon re-checks about every 60 seconds while
service-connect is not running and starts it on the first hit, then stops
re-checking; any `hyprial org service list|guide|connect|disconnect` call also
starts it immediately. Neither path blocks maintenance.

**Timing.** Once running, the recipient reconcile runs every 5 seconds and does
nothing when the directory did not change.

**Listing.** `service.grants` lists what this device granted and received.
Without `--org`, orgs whose directory cannot be read are named in
`unreadableOrgs` instead of being skipped silently. A grantor over a reader
bound is not listed under `received`; it is named in `limitedGrantors`
(`grantor` is null when the whole org stopped), so the listing never answers
"no grant" for what it could not read. The listing reads the directory itself;
it does not show the previous answer the reconcile keeps for that grantor. The
caller's own grants are always read in full, and `documentCount` gives the
caller's document count per org next to `documentLimit`.

Every `received` row also has four read-only local facts: `keyStored` says
whether the recipient's ownership ledger names this grant's Key entry and its
regular entry file is still present (the Key value is never read); `entryId`
is that entry name when stored and otherwise null; `connected` says whether
this grant currently has a live derived service mapping; and `serviceName` is
the final derived service name after any collision rename. The listing uses
only the ledger, the recipient's current derived snapshot, and current mapping
bindings. It never opens a credential, writes a Key, creates a mapping, or
starts service-connect. With no running service manager, `connected` is false
and `serviceName` is null; the listing does not re-derive the name just to
answer this question.

What Bob still does by hand: `hyprial org service connect <name>`; binding the
key to an agent; and, when the service is a model gateway, creating a model
connection that points at the local mapping port. Creating model connections
automatically depends on the model-connection work and is not part of this
design.

## Cadences

Three independent periodic jobs are involved. Documentation names them
separately:

| Name | Where | Interval |
| --- | --- | --- |
| device-row publish | every device | first publish right after the sidecar writes its address file, then at most every 10 seconds |
| admission derivation | exposing device | immediately after a grant, revoke, expose or sidecar start; otherwise at most every 30 seconds |
| recipient reconcile | recipient device | every 5 seconds once service-connect runs; about every 60 seconds an activation check while it does not run |

The sidecar's membership roster refresh (about every 60 seconds) is a fourth,
sidecar-internal period that decides when a new member's key appears.

## Public surface

| Item | Shape |
| --- | --- |
| Methods | `service.grant {org, port, device, expiresAtMs?, name?, protocol?, note?, credential?: {entryId, fieldName?}}` → `{grant, updated}`; `service.revoke {org, grantId}` → `{grantId, revoked}`; `service.grants {org?, port?, includeRevoked?}` → `{granted, received, documentCount: {<org>: n}, documentLimit, limitedGrantors: [{org, grantor, reason}], unreadableOrgs}`. Each `received` row adds `{keyStored, entryId, connected, serviceName}`: Key-entry ownership/presence without reading its value, live mapping state, and the final derived name after collision renaming. With no manager, `connected=false` and `serviceName=null`. All `local-operator` only, not HTTP-exposed. `org` is the full directory-space name (`group-<name>`); `device` is `<owner>/<deviceId>` or `@members`. |
| CLI | `hyprial org service grant PORT --org --device [--name] [--protocol] [--note] [--expires-at-ms] [--credential ENTRY[:FIELD]] [--json]`; `hyprial org service revoke GRANT_ID --org [--json]`; `hyprial org service grants [--org] [--port] [--include-revoked] [--json]` |
| Directory | device row keys `encryptionKeyId`, `encryptionPublicKey`; protected section `service-grants` with an owner-created parent directory |
| Sidecar | stdio protocol v7: `expose` requires `admission`; `keys` mode with at most 4096 keys |
| Events | `service.grant.*` and two `network.exposure.*` / `org.directory_roots.*` events, listed in the contract |
| Behaviour | default deny for exposures without grants (D7) |

No new error codes, `SecretSource` values, Zenoh keys, signature purposes,
org cell parameters or roster fields.

## Residual risks

- OrgFS authorship is assumed, not proven ([Trust](#trust)). Grants are as
  trustworthy as directory device rows and member admission, not more.
- The sealed key is replicated to every member device of the org. There is no
  forward secrecy: if a recipient device's private key leaks, anyone holding a
  copy of the document can open it.
- On the recipient the key is stored like any user-provider entry, as a
  private plaintext file.
- Revocation cuts connections and deletes the recipient's copy; a key the
  recipient already read must be revoked by the service itself.
- Mixed versions are not supported: older readers reject device rows carrying
  the encryption key and documents in the `service-grants` section, and daemon
  and sidecar must move to protocol v7 together. An org upgrades in one window.
- Clocks: `expiresAtMs` is judged by each device's own clock, so two devices
  with skewed clocks disagree about expiry for the size of the skew.
- The 240-document cap bounds the number of documents, not their size. A
  sealed key can be up to 16384 bytes, so roughly two dozen grants that each
  carry a near-maximum key exceed the 512 KiB per-grantor bound. Every reader,
  the grantor's own exposing side included, then treats that grantor as
  unknown, and the exposing side fails closed for its grants in that org.
  Typical API keys are far smaller than this.

## Not verified on two real machines

Everything above has been exercised on a single machine with a real OrgFS
runtime, a real supervisor and a stdio sidecar double that models the roster.
Not yet observed:

- that the key an exposing sidecar sees on an inbound connection equals the
  roster's `clientPublicByServer[<this serverPublic>]`, and how long after a
  recipient joins that entry appears;
- replication of grant documents between two daemons, the receiving replica's
  author check, and a non-owner member writing under the owner-created parent;
- the upgrade window of an existing org with live connections, and rollback;
- whether a non-authority member's sidecar reliably holds the full roster;
- opening a key with a device encryption key created under a real account.

These are tracked in [known problems](../reference/known-problems.md).
