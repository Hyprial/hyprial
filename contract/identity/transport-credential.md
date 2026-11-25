# Identity-owned transport credential storage

This contract covers only the local identity-domain boundary. It performs no
network operation and does not change the Tailcat `DeviceRecord.keyGeneration`.
The TLS `credentialGeneration` is a distinct, authority-assigned counter.

## Local files

All paths are relative to the Hyprial home.

| Path | Mode | Owner and content |
| --- | --- | --- |
| `secrets/transport/` and descendants | `0700` directories | Identity-owned TLS secret boundary |
| `secrets/transport/staged/<operationId>/key.pem` | `0600` | Staged RSA-2048 PKCS#8 private key |
| `secrets/transport/device-key.pem` | `0600` | Committed private key, renamed from staging |
| `state/transport/` | `0700` | Identity-owned credential state directory |
| `state/transport/enrollment.json` | `0600` | Exact non-secret staging record |
| `state/transport/credential.json` | `0600` | Authority projection plus the pinned bundle content/binding hashes |
| `state/transport/trust-bundle.pem` | `0644` | Pinned CA bundle named by `trustBundleId` |

`enrollment.json` has exactly `operationId`, `spkiSha256`, `owner`,
`deviceUid`, and `expectedCredentialGeneration`. The operation ID is canonical
hyphenated UUID4 text. The device UID is the existing 32-character lowercase
UUID4 hex form. A JSON boolean is never an accepted generation.

The staged key is created with `O_CREAT|O_EXCL`. Staging first reads the local
identity device store and requires the requested UID and owner to equal the local
`DeviceRecord.device_uid` and `DeviceRecord.owner` before creating transport
directories or key material. An owner mismatch returns
`TRANSPORT_OWNER_MISMATCH`. Re-entering the same operation uses the same key and
CSR. If a crash already renamed that key into the committed location, resume
recognizes it only by the recorded SPKI. A staged record for another owner,
device UID, operation ID, or expected generation is never adopted.

The CSR uses RSA-2048 and SHA-256. Its subject is empty, and it requests no SAN
or other extension.

## Frozen credential projection

`credential.json` has exactly these fields, with unknown fields rejected:

- `ownerUserId`, `owner`, `bindingId`, `deviceUid`
- `credentialGeneration`
- `certificatePem`, `chainPem`
- `cn`, `spkiSha256`, `serial`
- `notBefore`, `notAfter`, `renewableUntil`
- `trustBundleId`, `policyIssuer`
- local commit metadata: `trustBundleSha256` is the SHA-256 of the exact
  `trust-bundle.pem` bytes; `trustBundleBindingSha256` binds that digest to
  `trustBundleId`

`bindingId` is canonical hyphenated UUID4 text. `spkiSha256` is 64 lowercase
hexadecimal characters. Times are non-negative UTC epoch seconds. The CN is
`hdev-<bindingId>-g<credentialGeneration>`.

Before commit, validation requires all of the following:

- exact response schema and non-boolean integer generations;
- local and response device UIDs match;
- response owner equals staged, effective, and current local device owner;
- certificate SPKI, response SPKI, and staged key SPKI match;
- certificate chain reaches the supplied pinned bundle, every issuer's
  `BasicConstraints.ca` and `path_length` limit is enforced, and an issuer
  carrying `KeyUsage` must set `keyCertSign`;
- certificate CN, serial, and validity timestamps match the projection;
- the signed binding profile: the leaf carries exactly one URI SAN in the
  `https://auth.hyprial.com/transport/binding/` namespace, and its full value is
  `https://auth.hyprial.com/transport/binding/v1/sha256/<digest>` where the
  digest is the lowercase SHA-256 of the RFC 8785 JCS UTF-8 bytes of the exact
  ten claims (`profile` = `hyprial.transport.binding.v1`, `policyIssuer`,
  `bindingId`, `ownerUserId`, `owner`, `deviceUid`, `credentialGeneration`,
  `spkiSha256`, `renewableUntil`, `trustBundleId`) recomputed from the response
  projection; missing, duplicate, unknown-version, malformed or mismatched
  binding URIs are `TRANSPORT_RESPONSE_INVALID`, and unrelated URI SANs are
  ignored and never fetched;
- RSA-2048/SHA-256 certificate profile, a non-CA leaf, RSA key usage with
  `digitalSignature` and `keyEncipherment`, and extended key usage containing
  both `clientAuth` and `serverAuth`;
- `notAfter` is more than five minutes after validation time; and
- `renewableUntil >= notAfter`.

The binding URI namespace is a fixed protocol identifier, not an OIDC issuer
configuration or a fetch location. Its value is independent of `policyIssuer`
and of the configured network profile's issuer; authority origins may differ
while the signed binding namespace remains unchanged.

The chain check is a bounded verifier for this pinned enrollment profile, not
a general Web PKI verifier. It checks issuer/subject linkage, signatures,
validity windows, loops, the pinned self-signed trust anchor, CA basic
constraints and path length, and `keyCertSign` whenever an issuer carries
`KeyUsage`. The leaf checks above separately bind its exact CN, SPKI, metadata,
and client/server usages. It does not fetch AIA, CRL, or OCSP data because
credential reads must work offline and renewal/retirement owns revocation. It
does not process DNS/IP name identity, name constraints, or certificate-policy
constraints because this protocol assigns identity and authorization to the
exact owner/device/binding projection rather than Web PKI names or policy OIDs.
The single URI-SAN binding profile digest is the only SAN input it reads.
It does not require AKI/SKI because issuer-name selection followed by signature
verification supplies the cryptographic issuer proof.

Rejections are `TransportCredentialError` values with stable codes including
`TRANSPORT_OWNER_MISMATCH`, `TRANSPORT_SPKI_MISMATCH`,
`TRANSPORT_CHAIN_UNTRUSTED`, `TRANSPORT_RESPONSE_INVALID`, and
`TRANSPORT_CERT_EXPIRING`. Error messages contain no PEM, CSR, token, or key
material. The one-request device HTTP client is a separate boundary documented
in [`transport-client.md`](transport-client.md).

## Commit, read, retire, and status

The commit boundary repeats the full response, certificate, local-device,
owner, key, and pinned-chain validation. It does not trust a caller-constructed
or subsequently changed `TransportCredential` value. Commit fsyncs files and
containing directories in this order:

1. write `credential.json` to a `0600` temporary file;
2. rename the staged key to `device-key.pem` (or recognize the same SPKI there
   while resuming a prior interrupted commit);
3. atomically install `trust-bundle.pem` at `0644`;
4. rename the credential temporary file to `credential.json`; and
5. remove `enrollment.json`.

Validation and commit re-read the local device owner and UID. A changed owner
with the same UID returns `TRANSPORT_OWNER_MISMATCH` without changing staged
material or an existing ready credential. This local binding consistency check
does not establish tailnet membership or authority provenance. Offline read,
status, and explicit local retirement do not require active membership or an
owner match to the current local device record.

Before returning absent, a read validates any existing staged enrollment and
its state directory; unsafe mode, type, or symlink state is a typed error. A
read returns `None` unless the committed key, credential projection, and
pinned trust bundle all exist, the key/certificate/projected SPKIs match, the
bundle's exact content hash and ID binding match the committed projection, and
the certificate chains to that bundle. An interrupted commit safely removes
its operation-scoped regular temporary files before retrying; unsafe temporary
file types or modes produce a typed error. It never exposes a credential before
its trust bundle is installed. Before any mutation, staging preflights every
base directory, the operation-scoped root, and existing key/enrollment paths,
including directory modes. Commit preflights both destination roots and the
staged key's operation root; committed-key crash recovery does not require an
obsolete operation root. Device-record reads open `HYPRIAL_HOME`, `state`, and
`tailcat` in sequence with descriptor-relative no-follow directory opens, then
open the owner-only, single-link, mode-checked file relative to the validated
`tailcat` descriptor. Retirement inspects both root parents, both root types,
every descendant below `secrets/transport/`, and each state
file it will unlink before it removes anything. An unsafe child aborts with
`TRANSPORT_STORAGE_UNSAFE` and zero writes. Storage directory
creation walks from the credential root with descriptor-relative no-follow
operations, so a symlinked parent cannot redirect a write or leave changes in
another validated root. Storage reads use no-follow opens: symlinks,
non-regular or multiply linked files, non-`0700` transport directories,
non-`0600` key/credential/enrollment files, and a non-`0644` trust bundle fail
with the typed
`TRANSPORT_STORAGE_UNSAFE` error rather than becoming ready or absent. The
complete read/write/delete inventory and its evidence are recorded in
[`docs/reference/transport-credential-storage-path-audit.md`](../../docs/reference/transport-credential-storage-path-audit.md).

Local retirement removes `secrets/transport/`, `credential.json`,
`enrollment.json`, and `trust-bundle.pem`. It lstat-checks each transport root
and all deletion targets for owner, type, link count, and mode before mutation,
unlinks a root symlink itself, and never follows it into external storage. It
does not remove or rotate
`secrets/tailcat/device-key.json`, and it does not claim remote retirement.

The status projection contains only `cn`, `bindingId`,
`credentialGeneration`, the first 12 lowercase hex characters of
`spkiSha256`, `notAfter`, and `renewableUntil`. It contains no certificate,
chain, CSR, private key, token, or key path.

## Whole-set commit rejection and special-file reads

Before any directory creation, recoverable-temporary cleanup or installation,
commit checks every existing destination and operation temporary file as one
set: `device-key.pem`, `credential.json`, `trust-bundle.pem`,
`.credential.<operationId>.tmp`, and `.trust-bundle.<operationId>.tmp`.
Every present file must be regular, owned by the current uid, single-link and
have its exact mode (`0600` for key/credential, `0644` for bundle). An unsafe
member returns `TRANSPORT_STORAGE_UNSAFE` without changing another member,
the staged key or an existing ready credential. Safe temporary files are
removed only after the whole set passes, and are rechecked before removal.
This supplements the existing root/source/enrollment/device validation.

Transport file reads and descriptor-relative Tailcat device reads open the
final file with `O_NONBLOCK` as well as `O_NOFOLLOW` before checking the open
descriptor's regular type, owner, link count and mode. A preexisting FIFO must
promptly return `TRANSPORT_STORAGE_UNSAFE` instead of waiting for a writer.
Only a descriptor that passed all checks is read. This preserves the existing
concurrent-replacement limitation; whole-set preflight is not a filesystem
transaction against subsequent adversarial path replacement.

The authority client returns protocol data or a typed error and never installs
credentials. Callers explicitly pass a returned enrollment response through
`validate_enrollment_response` and then `commit_transport_credential`; both
boundaries verify the signed binding profile. Resolving a client operation after
a definite authority rejection changes only that client's in-memory replay
bookkeeping; it cannot bypass the whole-set file preflight above.

`load_client_tls_material` preserves credential/storage rejection codes while
converting them to client `NOT_SENT` errors. It detaches both public exception
chain links, including safe-reader and material-encoding failures, and exposes
no raw underlying credential exception through those links or diagnostics.
The credential module's read, validate and commit behavior is unchanged.

The identity public [tailnet workflow](tailnet-transport-workflow.md) composes
these storage primitives with trusted fresh membership, fixed authority config,
and authority-registration ports. Raw offline read/status/local retirement and
the strict local schemas here remain unchanged. Process-local orchestration is
not evidence of caller journaling or cross-restart recovery.
