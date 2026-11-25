# Provider-key envelope and grant primitives (E1, R1 v1)

This contract implements provider-plan §8.1 at `8d5933f6b4a5feafaa3286575ec38c72e66765db`.
It is a pure identity-domain format, not production credential delivery or launch
integration. `hyprial.identity.ProviderKeys` exports the namespace. It does not
change the transfer prototype, create an organization, publish documents, enable
daemon signing, resolve a host signer, or read/write a credential store.

The R&D lead confirmed the grant wrapper and timestamp representation in message
`25578525-37b8-5550-8ce4-c381bd0ecc79` (2026-10-31): grant is `{payload, sealedBy}`;
sign `purpose || 0x00 || SHA-256(enc(payload))`; `createdAt`, `issuedAt`, `expiresAt`
are nonnegative integer Unix milliseconds. No `revision` or `revoked` field is
added. Exact current grant/agent/version expectations come from the caller;
global freshness remains provider-plan R4. This paragraph preserves that decision
outside the message channel. E1 review also acts as the R1 crypto review.

## Encoding and limits

`enc` is exactly U1's `UserKey.canonical_bytes`: deterministic bounded JSON,
UTF-16-key sort, UTF-8 output, no whitespace, integer-only numeric values with
absolute value at most 2^53−1, maximum nesting 20 and encoded length 65536 bytes.
No second canonicalizer is defined. Input objects are detached before validation.
Fields are closed: missing and unknown fields fail. Integers cannot be booleans.

Binary fields use canonical padded standard base64 (not base64url). Raw keys are
32 bytes, Ed25519 signatures 64 bytes, GCM nonces 12 bytes, and wrapped DEKs
48 bytes (32-byte DEK plus 16-byte tag). Cryptography's GCM ciphertext includes
the tag at its end. Public-key IDs retain U1/X1's `ed25519:`/`x25519:` followed by
the first 32 lowercase hex characters of SHA-256(raw public key).

Local resource bounds: plaintext is 1–16384 bytes; recipients count is 1–32;
provider, keyId and owner are nonempty strings of at most 256 characters, without
NUL. Each recipientKeyId is unique. Empty, duplicate, malformed and oversized
inputs fail closed. These bounds do not add fields to R1 v1.

## Envelope

Only these top-level fields are accepted: `header`, `recipients`, `payload`,
`sealedBy`.

```text
header = {
  v: 1,
  alg: "A256GCM",
  wrap: "X25519-HKDF-SHA256-A256GCM",
  provider, keyId, owner,
  dekVersion: positive integer,
  createdAt: nonnegative integer Unix milliseconds
}
payload = {nonce, ciphertext}
recipients = [{recipientKeyId, epk, nonce, wrappedDek}, ...]
sealedBy = {keyId, sig}
```

Unknown v/alg/wrap values are refused. No `kind`, `grantRef`, device ID, public
signing key, or signature is added to the header. The recipient array order is
signed as given. The `sealedBy` keyId must match the separately trusted owner key.

Each seal draws a fresh 32-byte DEK, a fresh payload nonce, and one independent
32-byte ephemeral X25519 private key plus nonce per recipient, using the operating
system CSPRNG through `secrets.token_bytes`. There is no public deterministic
entropy argument. Fixed entropy is injected only by tests.

```text
headerBytes = enc(header)
payload.ciphertext = AES-256-GCM(DEK, payload.nonce, plaintext, AAD=headerBytes)
epk = X25519-public(esk)
shared = X25519(esk, recipientPub)                 # reject all-zero shared secret
info = UTF8("hyprial/provider-key/wrap/v1") || 0x00 || headerBytes || 0x00 || UTF8(recipientKeyId)
kek = HKDF-SHA256(shared, salt=epk || recipientPub, info=info, L=32)
wrappedDek = AES-256-GCM(kek, nonce, DEK, AAD=headerBytes || 0x00 || UTF8(recipientKeyId))
unsigned = {header, recipients, payload}
signingInput = UTF8("hyprial/provider-key/envelope/v1") || 0x00 || SHA-256(enc(unsigned))
sig = Ed25519(ownerPrivate, signingInput)
```

`sealedBy` is not part of the signature input. Its keyId is checked against the
trusted public key, not used to accept a key from the document. The complete
structure and owner signature are verified before any X25519 exchange, DEK unwrap
or payload decryption. The requested recipient must be explicitly listed, and the
local private key must derive that exact recipientKeyId. There is no fallback.

`reseal` verifies and opens the old envelope, preserves its plaintext and key
identity, increments dekVersion by exactly one, and seals with fresh DEK/nonces
and ephemeral keys. Its explicit created_at cannot precede the previous timestamp.
Old-DEK failure against the new payload is tested. `seal` is a stateless primitive:
it cannot prove uniqueness of a version across independent calls, storage replicas
or daemons. The future storage owner must allocate/serialize versions; E1 does not
supply that authority. No claim is made that Python can securely erase all memory
copies or revoke plaintext previously learned by a host.

## Grant

```text
signedGrant = {payload, sealedBy: {keyId, sig}}
payload = {
  v: 1, provider, keyId, owner,
  agent: canonical lowercase UUID string,
  dekVersion: positive integer,
  recipientKeyId,
  issuedAt: nonnegative integer Unix milliseconds,
  expiresAt?: nonnegative integer Unix milliseconds
}
signingInput = UTF8("hyprial/provider-key/grant/v1") || 0x00 || SHA-256(enc(payload))
```

When present, expiresAt must be greater than issuedAt. Verification requires an
explicit valid `now` with `issuedAt <= now < expiresAt`; absence of expiresAt
means no expiry in this format, not evidence of non-revocation. Boolean clocks,
future issue times, expired grants and malformed UUIDs fail. Envelope and grant
purposes are distinct and signatures cannot be interchanged.

`verify_grant` checks the trusted owner's signature, then exact agreement with
the supplied header's provider/keyId/owner/dekVersion and the requested stable
agent UUID and recipientKeyId. It does not authorize a process launch or prove
that this stable UUID still refers to the same live incarnation.

`check_access` additionally requires an explicit current_grant_digest (SHA-256 of
`enc(signedGrant)`), current_dek_version and boolean revoked fact. The candidate
is detached once so signature and digest checks cannot mix two replacements.
A valid old signature is insufficient if its exact digest/version is not current.
A current grant does not create a missing recipient wrap. There are no wire-level
revision/revoked fields and no inferred current-state defaults.
`owner_key` verifies the envelope; the optional `grant_owner_key` verifies the
grant instead (default: `owner_key`). Both are caller-supplied verified anchors of
the same owner, so the envelope and the grant may be sealed by two keys of that
owner after a signing-key rotation; each object is still verified only with the key
its own `sealedBy` names.

## Trust and ownership boundaries

`OwnerKey` is a caller-supplied, already-verified U2 anchor for the exact owner,
keyId and raw public key; key_kind must be `owner`, never `device`/host. It is not
a certificate verifier. Only the caller's verified lookup can construct a trusted
anchor: passing a self-reported key to this low-level API cannot establish trust.
`SigningKey` holds a caller-supplied in-memory Ed25519 private key with a redacted
repr; it is not an IPC capability and does not enable the U1 daemon signer.
`Recipient` is an already-resolved X1 public key whose keyId is checked locally.
The host trust/signing authority remains R6 and is not implemented here.

`ports.py` defines protocols only: StoragePort reads candidates, OwnerKeyPort
returns KeyLookup's verified/absent/unavailable state, RecipientPort resolves
trusted public keys. No protocol has a storage/network implementation here.
Unavailable is not absence. No global freshness or revocation authority is
created. R4 must establish reliable current-state facts before a future launch
consumer uses `check_access`; a digest from an arbitrary stale replica is not
such a fact. Production lookup/launch/error mapping remains outside E1.

## Failures and redaction

Exactly these six provider codes are registered in kernel.ipc_errors.ALL_CODES:

| Code | E1 meaning |
| --- | --- |
| PROVIDER_GRANT_MISSING | Explicit access input has no grant. |
| PROVIDER_GRANT_REVOKED | Explicit trusted revocation input is true. |
| PROVIDER_KEY_NOT_FOUND | A present, non-revoked grant has no envelope input. |
| PROVIDER_KEY_RECIPIENT_PENDING | Requested recipient is not in the verified envelope. |
| PROVIDER_KEY_UNSEAL_FAILED | Local private-key mismatch, zero exchange, invalid seal inputs, unwrap/decrypt failure. |
| PROVIDER_KEY_SIGNATURE_INVALID | Malformed signed structure, wrong signature/purpose/keyKind/owner/context, invalid validity window, or stale digest/version. |

Access-check precedence: missing grant; invalid/revoked caller fact; missing
envelope; envelope schema/signature; grant schema/signature/context/time; exact
current version and digest; recipient membership. Decryption is a separate
primitive after signature verification. An expired grant is a failed validity
check, not invented positive revocation evidence. The failure fixture executes
the missing/revoked/missing-key ordering.

Errors have fixed messages and no input data. No key bytes, plaintext, JWT,
crypto/parser exception message or user-supplied value is interpolated. No
logging, status writer or environment materializer exists in this package.
These codes do not claim that a real child process was prevented from starting;
that requires G2 integration and acceptance.

## Fixtures and validation

`contract/fixtures/provider-keys/envelopes.json` and `grants.json` contain only
publicly documented synthetic test keys, nonces, payloads and independent fixed
vectors. The envelope fixture includes expected header AAD, KEKs and signing
input. The independent builder is `tests/identity/provider_keys/envelope/vectors.py`;
it uses cryptography primitives and U1 encoding, not the product seal functions.
`failures.json` fixes all six codes and executable access precedence cases.

Run `uv run pytest tests/identity/provider_keys/envelope`. Coverage includes
multi-device opens, every signed field changed or missing, unknown fields,
unsupported formats, malformed bounds, purpose swap, key-kind/owner mismatch,
recipient-wrap substitution after valid re-signing, low-order exchange, invalid
ciphertexts, pre-decrypt signature checking, re-seal/old-DEK failure, exact grant
bindings, expired grants, replay fences and shared-candidate replacement.

Only pure local crypto and verification are covered. PK-6's process-start and
admission assertions, all real Casdoor/provider operations, grant publication,
host signing, cross-device execution and migration remain unimplemented or
BLOCKED on their approved dependencies. No UK-CAS or full PK case is closed.

## Owner records (r3)

Contract freeze r3 (`docs/design/provider-key-contract-freeze-2026-11-03.md`,
§2, §3.1–§3.3, HQ ruling F-2) adds one owner-signed record format. The
schemas and their field rules are frozen there; this section records the
signing and verification rules implemented in
`identity/impl/provider_keys/records.py` and exported through
`hyprial.identity.ProviderKeys`.

```text
record       = {payload, sealedBy: {keyId, sig}}
signingInput = UTF8("hyprial/provider-key/record/v1") || 0x00 || SHA-256(enc(payload))
payload.type ∈ {"provider-key", "provider-grant-state"}
```

- The record purpose is the third whitelisted purpose. Envelope, grant and
  record signatures are not interchangeable in either direction.
- `verify_record(record, owner_key=, record_type=)` detaches the input, requires
  exactly `{payload, sealedBy}`, verifies the owner signature against the
  verified U2 `OwnerKey` before reading any payload field, then applies the
  closed schema. `payload.type` must equal the expected `record_type` and
  `payload.owner` must equal `owner_key.owner`. It returns the detached payload.
- `sign_record(payload, signer=)` validates the detached payload with the same
  schema before signing; an invalid payload is never signed.
- Schemas: `provider-key` is §3.2, `provider-grant-state` is §3.3, with the §2
  identifier rules (provider/grantId/legacyRef names, `pk.` + 26 base32 keyId,
  `environmentNames` 1–8 unique, label 0–128 characters with no Unicode `C*`
  category). State-dependent nulls are enforced: a deleted key has
  `dekVersion`/`envelopeDigest` null and no recipients; an active grant state
  carries an E1 grant whose provider/keyId/owner/agent equal the outer fields
  and whose `expiresAt` is present; revoked has `grant` null; awaiting-key has
  `keyId` and `grant` null and a non-null `legacyRef`.
- `check_transition(previous, current)` (both already verified) requires the
  same type, unchanged identity fields (key: owner, keyId, provider, kind,
  createdAt, legacyRef; grant state: owner, agent, grantId), a strictly larger
  `seq` and a non-decreasing `updatedAt`. A deleted `provider-key` is terminal.
  A revoked grant state may become active again only through a higher `seq`.
- `check_envelope_matches(meta, envelope)` requires an active meta whose
  `envelopeDigest` equals `record_digest(envelope)` (SHA-256 of `enc`, the same
  rule as the E1 grant digest), header keyId/owner/provider/dekVersion equal to
  the meta, and the exact recipient keyId set.
- `new_key_id()` draws 128 CSPRNG bits; `agent_uuid(entity_token)` accepts only
  32 lowercase hex characters and returns the canonical UUID.
- Every refusal above is `PROVIDER_KEY_SIGNATURE_INVALID` with a fixed message
  and no input data.

The X1 store opens envelopes without exporting its private key:
`recipient()`, `open_sealed(envelope, owner_key=)`,
`probe_sealed(envelope, owner_key=)` and
`reseal_sealed(envelope, owner_key=, signer=, recipients=, created_at=)` run
under the store lock. `probe_sealed` uses the pure `unwrap_check`, which
verifies the envelope and unwraps only this device's DEK without decrypting the
payload; it returns True or raises (`PROVIDER_KEY_RECIPIENT_PENDING`,
`PROVIDER_KEY_UNSEAL_FAILED`, `PROVIDER_KEY_SIGNATURE_INVALID`), never False.

Two non-provider-authorization codes (F-1) have fixed messages in
`ProviderKeys.Error`: `PROVIDER_STORE_UNAVAILABLE` (owner store port absent,
space unreadable/unwritable, feature gate closed, or U2 anchor unavailable) and
`PROVIDER_STORE_CONFLICT` (conditional write still conflicting after the
bounded retries). They do not take part in `check_access` precedence.

An owner-key launch requires every held slot to pass its current grant check.
A revoked slot raises `PROVIDER_GRANT_REVOKED` and refuses the whole launch;
it never continues with that credential omitted or falls back to a legacy
grant. The legacy launch surface keeps its declared
`AGENT_SECRET_RESOLUTION_FAILED` wrapper and retains the provider code only
as its value-free category. Revoke remains idempotent, and a revoked grant is
not renewed implicitly.

Local durable high-water observations, individual mark lookups and marked
slot inventories read the current atomic mark-file snapshot before relying
on cached state. A second instance's higher sequence must be visible, and an
equal sequence with a different digest refuses. Saves continue to merge
under the existing file lock. These are local state-directory guarantees;
they do not establish cross-device freshness or survive deletion of all local
state followed by a fresh process.

`contract/fixtures/provider-keys/records.json` holds one signed synthetic record
of each type, signed with the public test owner key `bytes(range(32))`. It is
built independently of the product signer by
`tests/identity/provider_keys/records/record_vectors.py`; run
`uv run pytest tests/identity/provider_keys/records`.
