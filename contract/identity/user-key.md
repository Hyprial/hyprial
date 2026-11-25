# Local user-key core (U1, X1, U2)

This is the local-core contract, not activation of provider credentials.
Production composition has no active signing purpose, Casdoor issuer, publication
path, revocation service, or personal-org naming rule. U2 adds local verification
against explicitly injected sources, not production activation. Positive signing is tested with
an injected **test-only** purpose. Provider formats remain deferred to E1/G1
under R1. X1 supplies local X25519 custody only; encryption-key declarations
remain blocked on R1/U2/R6.

## Storage and lifecycle

A key belongs to `(Casdoor sub, deviceUid)`, under the configured daemon state
root: `identity/user-keys/<identity_slug(owner)>/key.json`. The actual deviceUid
comes from device provisioning; login never invents a substitute. Local key
creation follows identity commit and successful device provisioning. An existing
same-identity, same-machine key is reused. A missing device or sub reports
`unavailable`; binding issuance is not available in U1, so generated keys are
`unbound`. Login itself remains successful when this optional stage is unavailable.
The login device result carries a public `userKey` object and a next step.
An already-logged-in session also initializes or reuses this key under the home
identity fence. It retains `status:"already-logged-in"` and adds the same public
`device.userKey` result. This requires neither `--force`, reauthentication nor
re-provisioning a complete device. Optional key-stage failures do not fail the
session; a later ordinary login retries the stage. Existing copied keys are
quarantined/replaced once the device binding is valid for this machine.
This is an additive change to published `login --json`: existing identity and
device readiness fields retain their meaning. Missing device/sub yields
`device.userKey = {"state":"unavailable","code":"USER_KEY_ABSENT",
"nextStep":"complete device provisioning and log in again"}`. Successful local
creation returns the public status fields below plus `code:"USER_KEY_UNBOUND"`
and `nextStep:"trusted user-key binding issuance is not yet available"`.
A storage refusal returns `state:"unavailable"`, its user-key error `code`, and
`nextStep:"inspect user-key storage before retrying login"`. The device's `ready`
field is independent of this optional stage. Human output warns that the key is
not active; JSON progress/warnings use stderr, never a second stdout object.

User-key storage requires POSIX uid, no-follow/nonblocking file flags and flock.
The capability check runs before any key or audit filesystem access. If a required
primitive is unavailable, the daemon catalog and application still import and
compose normally, public key status is `unavailable`, and local key operations
fail closed with `USER_KEY_UNBOUND`. Login returns `state:"unavailable"`, that
code and `nextStep:"local user-key privacy and locking are unsupported on this platform"`.
There is no permissive privacy/locking fallback. Native Windows execution remains
a separate validation requirement; the module/capability tests simulate absence.

Managed directories are 0700, files are 0600, owned by the process uid. Symlinks,
non-regular files, multiple hard links and loose modes are refused. The configured
state root is checked for ownership/type but retains its pre-existing mode, as
other daemon state shares it. A private lock serializes generation across callers.
Atomic writes use the existing kernel writer. No private material enters agent
home, bundles, public status, audit, or exception messages.

`key.json` fields: `schemaVersion:1`, `algorithm:"Ed25519"`, `keyId`, `privateKey`,
`publicKey`, `owner`, `casdoorSub`, `deviceUid`, `machineFingerprint`, `createdAt`,
`state`. Public/private keys use canonical padded base64; keyId is `ed25519:` plus
the first 32 lowercase hex characters of SHA-256(public key bytes).
States: `unbound`, `bound`, `quarantined`, `retired`. A bound flag on disk is **not**
a trust anchor. U1's production signer always remains disabled.

Fingerprint, deviceUid or sub mismatch quarantines the record. Login scrubs its
private field before archiving the public record under `quarantine/<keyId>.json`
and generating a new key. A crash after scrubbing cannot leave an archived private
key; rerunning login resumes from the public quarantined record. Owner-slug
collisions are refused, never adopted. Agent move/destroy does not touch this root.

## IPC and CLI

The catalog registers local-only methods; none is HTTP-exposed:

| Method | Params | U1 result |
| --- | --- | --- |
| `userKey.status` | `{}` | public local status |
| `userKey.sign` | `{purpose,payload,actor?,sessionRef?}` | refusal: no active production schema/trust |
| `userKey.rotate` | `{}` | `USER_KEY_UNBOUND`; no mutation |
| `userKey.revoke` | `{deviceUid}` | `USER_KEY_UNBOUND`; no mutation |
| `userKey.renew` | `{}` | `USER_KEY_UNBOUND`; no issuance or publication |

Unknown fields are rejected. `actor`, `sessionRef`, known interactive peers and
worker origin never become operator authority. Worker requests are refused before
schema validation, even if they claim `caller:"operator"`. The trusted origin is
an in-process transport argument, not JSON data. Operator IPC comes through the
local socket; direct in-process origin is daemon, not operator. Rotate/revoke are
operator-only. No request can enable signing, inject a schema, or supply a trust
port. Unknown signing purposes return `PURPOSE_UNKNOWN`; reserved but inactive
purposes return `USER_KEY_UNBOUND`.

Status fields are exactly `keyId`, `publicKey`, `state`, `bindingExp`,
`revocationSeq`. Missing local key gives `absent`; missing device/sub gives
`unavailable`. The two binding metadata fields are null in U1. Self-reported
`bound` is projected as `unbound` until U2 verifies trust.

CLI: `hyprial user key status|rotate|renew|revoke --device <uid> [--json]` (the device
option applies only to revoke). CLI calls IPC only; it never reads key files.
Worker shells are refused before contacting the daemon. Standard centralized CLI
output applies: one JSON object, `ok:false` exits 1. U2 registers `renew` as an
explicit unavailable operator-only operation; workers cannot invoke it.
Status is safe to retry. Signing and lifecycle methods prohibit automatic retry;
their audit records are durable even when the requested operation is refused.

Accepted same-uid limitation: an unidentified process using the operator socket
can look like the operator. This is not isolation from hostile same-uid code.
Known worker/session/interactive identities are refused. No squire/personal-org
exception is enabled before #2078 supplies a trusted resolver.

## Purpose, trust and encoding boundary

`contract/fixtures/user-key/purposes.json` is the closed reservation list. User domains are
`hyprial-orgfs-decl-v1` and `hyprial-user-op-v1`; neither overlaps the transfer or
device signing domains. Each eventual production schema must determine its kind
and exact allowed fields. U1 deliberately leaves production kinds/schemas inactive
rather than guessing the absent OrgFS mapping contract or provider R1 format.

The test-only injection exercises strict fields, forced matching owner, caller-org
authorization and daemon-generated `issuedAtMs`. Unknown/missing fields and a
caller-supplied timestamp are refused. The local deterministic encoder accepts
UTF-8 strings, booleans, null, arrays, objects with string keys, and integers in the
exact JSON-safe range. It rejects floats, surrogates, depth over 20 and encoded
payloads over 64 KiB. Object keys sort by UTF-16 code units; separators have no
whitespace and non-ASCII text remains UTF-8. Input is
`domain || NUL || kind || NUL || canonicalPayload`. This constrained internal
fixture is **not** a frozen production OrgFS/provider wire envelope.

The synthetic envelope has `domain`, `kind`, `payload`, `signer`, `signature`;
`signer` has `keyKind:"owner"`, `owner`, `keyId`. Verification checks the registered
kind/domain/key kind, exact field sets, owner, keyId and signature. Public keys
must come from a trusted caller-supplied verification result, never the envelope.
Golden signatures use a publicly documented synthetic seed, never production keys.

The daemon signer requires all of: enabled service, a bound local record, an
active schema, authorized caller/org, and an independently verified U2 `Trust`
result matching the public key, finite future freshness deadline, and nonnegative
integer revocation sequence. No production trust source is installed in U1.
An unconfigured `OwnerKeyPort.lookup` returns `unavailable`, not `absent`.
U2 verification is described below. Production activation remains deferred.

## Policy, limits and audit

Operator policy admits the reserved user purposes; internal daemon policy admits
`orgfs.commit-as-user`, plus R4/R5 only when an in-process operation carries its
operator-initiated provenance. Other callers are refused. This policy does not
activate a reserved schema. Per `(caller class, actor, purpose)` token buckets:
operator 30/minute, revocation-ledger append 60/hour. Internal commit-as-user is
unlimited but counted. Squire is denied in U1, so no squire rate bucket is active.

Audit is append-only `audit.jsonl`, 0600, at most 16 MiB per file with four retained
rotations. Exact fields are in `contract/fixtures/user-key/audit.json`. It records success
and refusal, no raw payload or session reference; unknown purpose and invalid
payload metadata are null. Audit failure prevents signature delivery. Only
validated metadata is logged. Rate and audit decisions are serialized per signer;
a file lock protects audit rotation and append across local processes.

New public codes: `USER_KEY_ABSENT`, `USER_KEY_UNBOUND`,
`USER_KEY_BINDING_EXPIRED`, `USER_KEY_QUARANTINED`, `PURPOSE_UNKNOWN`,
`PAYLOAD_INVALID`, `RATE_LIMITED`. Authorization uses `CALLER_NOT_AUTHORIZED`;
invalid IPC shape uses `INVALID_ARGUMENT`. Error text never interpolates key
records, parser errors or payload values.

## U2 local verification (not production issuance)

`UserKey.verify_binding` and `verify_revocations` are network-free. Inputs are a
compact JWT, an exact issuer, an explicitly trusted kid→RSA public-key map, and a
clock. Nothing in a token selects its verification key source. Header fields
must be exactly `alg`, `kid`, `typ`; only RS256 with RSA keys of at least 2048 bits
is accepted. JWT segments use canonical unpadded base64url. Duplicate JSON keys,
unknown/missing fields, invalid types and oversized tokens (>64 KiB) are refused.
Errors contain no token, parser exception or supplied key bytes.

Binding typ is `hyprial-user-key-binding+jwt`. Exact claims are `iss`,
`aud:"hyprial-user-key"`, `sub`, `name`, `owner:"hyprial"`, `keyId`, `publicKey`,
`keyAlg:"Ed25519"`, `deviceUid`, `iat`, `nbf`, `exp`, `jti`. Public keys use
canonical padded base64 and must match the Ed25519 keyId hash. Date values are
nonnegative JSON-safe integers; `iat <= nbf <= now < exp <= iat + 30 days`.
A binding's name/sub/device correspondence is checked by the consumer, after
cryptographic verification. No key-binding token is a Lark identity assertion:
the legacy verifier explicitly rejects both reserved user-key token types,
even if other legacy claims are otherwise valid.

Revocation typ is `hyprial-user-key-revocations+jwt`. Exact claims are `iss`,
`aud:"hyprial-user-key"`, `sub`, `seq`, `revokedKeyIds`, `iat`, `exp`. Sequence is
a nonnegative JSON-safe integer; keyIds must be valid and unique (at most 4096).
`iat <= now < exp <= iat + USER_KEY_REVOCATION_MAX_AGE`, where the exported kernel
budget is 86400 seconds. There is no signature-time exemption for revoked keys.

`daemon.UserKey.OwnerKeyPort` accepts daemon-owned callables for trusted
owner→sub resolution, JWKS, binding and revocation sources, plus optional fallback
sources. The default instance has no sources and always reports unavailable.
Source tokens are tried in directory-then-fallback order; a cached token may be
used on missing/unreachable sources only after revalidation with current JWKS and
clock. A newly supplied invalid token cannot silently fall back to an older cache.

Lookup results:

- `verified`: public key, matching device/sub, deadline `min(binding.exp,
  revocations.exp)`, and anchor `{iss,jwksKid,credentialSha256,revocationSeq}`.
- `absent`: fresh, verified positive revocation evidence or a verified binding
  for the requested keyId belonging to another owner/sub. No old-signature grace.
- `unavailable`: missing source/mapping/JWKS/document, malformed/bad signature,
  expired evidence, mismatched keyId, or rollback/conflict.

Within one port instance, revocation sequence and issued-at cannot decrease,
revoked IDs cannot disappear, and equal sequence cannot change the revoked set.
This memory is **not** a durable revocation ledger or global freshness proof.
This local slice does not claim restart/cross-daemon revocation persistence;
production activation must provide that state via the approved lifecycle/storage
integration. No second database or inbox is introduced. The trust adapter also
requires binding public key, deviceUid and sub to match the local signing record.
Local `bound` state remains insufficient, and the production signer stays off.

The shared JWKS reader enforces exact discovery issuer, same HTTP(S) origin for
jwks_uri, no URL credentials/fragment, RS256/signature-use RSA keys, unique kid,
and a 300-second cache. A failed refresh does not extend stale keys. Token jku/x5u
is never followed. The legacy identity verifier consumes the same parser while
retaining its existing transport and cache/unknown-kid refresh discipline. U2
only instantiates injected test transports; it installs no production endpoint.

`directory/user-keys/<owner>/<leaf>` is reserved as a protected partition.
Unsigned key-document writes are denied in every space, including personal
spaces, even to the path owner. Mutations of the reserved partition in existing
organization-directory trees are also denied. This does not install personal-org
creation or tree projection. `origin.author` alone cannot establish a key anchor. `daemon.UserKey.publish`
explicitly refuses with `USER_KEY_UNBOUND`. This reservation does not choose a
personal org or grant any reader/writer. The dispatcher confirmed #2078's naming
ruling at `4bee2117a` (§8.1): `personal-<userKey>`, with its long-name hash rule.
That input is used only in isolated admission tests, not production naming or
publication. Production publication remains blocked on #2078 implementation and
the approved authenticated publication integration.

Actual Casdoor issuance, renewal, revoke, rotation/re-signing and publication are
not implemented here. CLI/IPC renew/rotate/revoke refuse without mutation. Local
verification of synthetic fixtures is not UK-CAS acceptance. Provider record
re-signing also awaits its unapproved R1 format. The binding fixture contains
only public synthetic issuer material and signed test documents.

## X1 local encryption core (not activated)

`identity.EncryptionKey.Store` is an in-process daemon custody primitive, not an
IPC or login API. It generates independent random X25519 keys; it never converts
Ed25519 keys or reads Tailcat/transfer recipient files. User keys are scoped by
`(Casdoor sub, deviceUid)`; host keys by `deviceUid` only. A host constructor
rejects a non-null sub, so a login-owner change cannot change its key. Callers
must supply identity from trusted device context; accepting a string here does
not authenticate a device or a user.

Local storage is `state/identity/encryption-keys/<locator>/key.json`, where locator
is SHA-256 of the compact JSON array `[keyKind, casdoorSub, deviceUid]` (default
Python ASCII escaping). Full identity is checked on every read. This is a local
storage locator, not a directory publication path. Privacy, capability and lock
checks reuse U1. JSON duplicate/unknown fields, mismatched key pairs, unsafe
files and stored identity mismatches are refused. Public keys use canonical
padded base64; local keyId is `x25519:` plus the first 32 SHA-256 hex characters.

The local record adds `schemaVersion:1`, `machineFingerprint`, `createdAt` and
`privateKey` to the public fields `algorithm`, `keyKind`, `casdoorSub`, `deviceUid`,
`keyId`, `publicKey`, `state`. `algorithm` is `X25519`; `keyKind` is `user|host`.
`local` means only that local generation succeeded, **not** that an anchor or
recipient is trusted. `absent` status does not generate a key. A changed machine
fingerprint durably quarantines a local record. Neither `ensure` nor exchange
can use it or implicitly replace it. Quarantine retains the private bytes for
explicit recovery; automatic scrubbing/rekeying could destroy encrypted data.

Exchange returns raw X25519 shared bytes only to its in-process caller; it does
not export private-key bytes, derive a DEK, select HKDF parameters, or wrap a
provider value. It requires the exact expected local keyId and rejects invalid
or low-order peer keys. The caller must authenticate the peer separately. No
exchange operation is exposed to an agent or IPC. `revoke` is an unwired local
disable/scrub primitive, with an idempotent `revoked` tombstone; it is not remote
revocation and not a complete rotation workflow. Future orchestration must
complete rewrap/read-back before invoking destructive lifecycle operations.

Policy requires an owner signer for user declarations and a device signer for
host declarations. `signer_kind_allowed` checks this policy only; it is **not**
signature verification. No declaration schema, purpose/domain, signature format,
trusted lookup or publication service is implemented or frozen here. R1, U2,
R6/T7 and #2078 remain explicit blockers. The draft encryption fixture records
those blockers instead of providing a self-signed compatibility format. No
production wiring, CLI/IPC fields, errors, login behavior or existing signer
behavior changes in X1; it does not close PK-1/3/6.

Contract-Impact: additive, inactive U1 IPC/CLI and local storage contract;
X1 adds an in-process local core only, with draft declaration fixtures. U2 adds
local verification, an unavailable renew command/method and a deny-all reserved
key partition. OpenAPI and CLI surface are updated for renew.
Skill-Impact: new user-key CLI; installed hyprial-ops skill must be synchronized by
its owner before release. This slice does not write user-level skill/config files.
