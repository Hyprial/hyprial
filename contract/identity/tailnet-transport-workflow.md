# Tailnet transport workflow v1 (process-local component)

`hyprial.identity` exports `TailnetCredentialWorkflow`, opaque
`TailnetCredentialOperation`, `TailnetCredentialContext`, `TailnetCredentialError`,
`TailnetAuthorityPin`, `TailnetMember`, `TailnetMembership`,
`TailnetAuthorityBinding`, and `TailnetCredentialAssociation`.

This is a Python orchestration contract, not a new sidecar or HTTP wire protocol.
It composes the unchanged transport-client v1 requests and frozen ten-claim
certificate profile. The binding URI and opaque ownerUserId retain their existing
meaning. No deviceId/UID derivation, directory fallback, second lease authority,
new PKI, or node placement signing/checking is introduced.

## Trusted ports

`TailnetCredentialWorkflow(home, *, tailnet_id, context)` requires a producer with:

- `membership(tailnet_id) -> TailnetMembership`: requested opaque tailnet ID,
  role (`member` or `authority`), revision, authority device ID, memberReady,
  sidecar-projected lease remaining seconds and code, local active member row,
  and complete previously accepted fixed authority pin. Healthy sidecar code is
  null; authority lease must be null. Non-authority members require a positive
  non-boolean integer remaining lease no greater than 300. Ready revisions are
  non-boolean integers in 1..2**53-1. Active authority-local readiness needs no
  self lease. Identity does not compute or persist lease deadlines. Non-authority
  restart must acquire a fresh lease in the real adapter.
- `authority(tailnet_id, pin) -> TailnetAuthorityBinding`: exact full pin plus
  explicitly provisioned `DeviceAuthorityConfig`, expected policyIssuer and
  trustBundleId. Pin fields are tailnet ID, owner, device ID, master public,
  server public, address, control port. HTTPS origin/SNI/service port/CA/TLS
  are supplied separately; control 7448 is not an HTTPS default. Config equality
  includes all timeout and TLS material fields, not merely host or authority ID.
  Authority addresses are excluded from pin and nested projection repr.
- `association(tailnet_id, device_id, pin) -> TailnetCredentialAssociation | None`:
  authenticated authority registration projection containing full pin provenance,
  tailnet/device ID, owner, independent device UID, binding ID, generation, SPKI,
  opaque ownerUserId, issuer, bundle ID and active/retired state. `None` means
  authoritative absence only; unavailable/failed lookup raises. No caller JSON,
  dataclass constructor, or trusted boolean authenticates provenance.

The producer must perform actual trusted peer-to-member resolution for first
enrollment and authenticated association lookup. It supplies accepted full pins
from trusted configuration/roster; membership-status alone lacks that pin.
These producer/server operations and CA bootstrap are not implemented by this
component. Their absence blocks live acceptance.

## Operations and effects

`stage(operation_id, expected_credential_generation, *, kind="enroll"|"renew")`
checks fresh member/local owner/device/UID and pin/config before real RSA staging.
Local identity reads use the existing strict transport no-follow reader for both
device record and identity sibling, rejecting unsafe parents/symlinks/hardlinks
before their content is read; the generic device store is unchanged.
Enrollment requires authoritative association absence, generation zero and no
ready credential; renewal requires active association equal to ready credential
and matching expected generation. During first renewal and retirement
preparation, configured client TLS must equal the existing validated ready
certificate/key/bundle returned by `load_client_tls_material(home)`; absent or
wrong material is rejected before staging or HTTP. It is never reloaded or
compared with changed ready material during replay or after HTTP success.
UID comes from the safe local DeviceRecord and
is separately compared with authority registration, never guessed from deviceId.

`enroll(handle)` / `renew(handle)` / `retire_remote(handle)` freeze and retain the
original client instance, request data, full pin/config/TLS and local identity.
Every send checks fresh membership and unchanged full binding. Local identity
comparison retains the public record fields, key generation and independent UID;
cosmetic display-name changes do not strand an operation. Before first
send, association absence or old target must still hold. UNKNOWN replay skips
those old association preconditions because the first execution may have updated
registration. It uses the original client and exact original v1 body. Local
config/CA/TLS changes stop sending; they never select a replacement client.

Canonical success is copied into internal cache before subsequent membership,
association or certificate checks. Caller-returned dictionaries and offline
status are deep copies. A subsequent failed gate cannot turn success into
NOT_SENT. UNKNOWN remains UNKNOWN after a rejected replay precheck or a later
NOT_SENT connection failure. Only canonical nonretryable REJECTED is terminal and
cached; retryable authority envelopes remain UNKNOWN under transport-client v1.

Immediately before invoking the original client, the workflow saves the previous
operation outcome and conservatively latches UNKNOWN. KeyboardInterrupt and
SystemExit still propagate as the original exception instance, including when
raised after the request was written and a response read/decoded; they cannot
leave the operation discardable as NOT_SENT. This latch does not prove a write
or any remote result. A definitive typed NOT_SENT restores NOT_SENT only when
the previous operation outcome was not UNKNOWN. An earlier UNKNOWN followed by
a NOT_SENT attempt retains the existing overall/attempt outcome distinction.
Pre-delegate membership/association failures for a never-delegated operation
remain NOT_SENT. Canonical success/rejection and cached replay are unchanged.
After an interrupted send, only exact replay through the same handle, original
client, request and binding can settle the pending operation; replay skips the
original association-absence/old-generation preconditions as above.

`validate(handle)` validates the internal response against original CA, owner,
UID, SPKI and ten claims, then requires current active authority association
including binding/generation/SPKI/ownerUserId/issuer/bundle. `commit(handle)`
requires this internal validated result, repeats fresh membership/local binding
and association checks, and delegates real safe installation. Failed installation
keeps success pending; the same process can resume staging with the original
operation and verifies the original SPKI before retrying installation. Committed
handles are idempotent observations and do not regenerate staging.

`prepare_retire(operation_id)` first validates a canonical lowercase UUID4 before
creating any owned pending handle, then checks fresh membership and active association
against ready state, then freezes the original ready binding/generation/UID/SPKI
and client. UNKNOWN retries never derive a new target from current ready state
or require the old association still active. Remote success/rejection is cached.
No automatic local deletion or cleanup convenience exists; original explicit
local-retire primitive remains separate. `remoteEnforcementState="pending"`
remains pending, not proof of Zenoh rejection or tailnet member revocation.

Handles have no writable public state. Foreign/fabricated handles and route/kind
misuse fail closed. Within one workflow, a pending issuance (including rejected
staging or success awaiting install) blocks replacement mutations. A pending
retirement blocks replacements until its remote result is canonical. Operation
IDs cannot be reused within the same workflow.

## Offline staging discard

`discard_staged(handle, *, confirmed=False)` discards only issuance staging
created by that workflow. Before delegating a new stage, the workflow uses the
existing strict no-follow reader to establish that enrollment.json is absent.
Only successful new creation latches private origin proof. Adopting persistent
staging never supplies this proof: a new client's NOT_SENT or even canonical
REJECTED under a new configuration cannot establish the original operation's
frozen target or outcome. Caller booleans, responses and enrollment fields do
not supply origin or remote-outcome authority.

For nonterminal discard, confirmed must be strictly True. It expresses intent.
The owned original operation must have overall NOT_SENT, or the original client
must have accepted canonical nonretryable REJECTED. UNKNOWN, including UNKNOWN
followed by a NOT_SENT attempt, success awaiting installation, partial key
installation, COMMITTED, retirement and foreign/fabricated handles are ineligible.
No membership, authority or association port is needed for this offline recovery.

Before the first deletion, all relevant directory ancestry and target files are
preflighted through the existing storage primitives: no symlink traversal,
correct directory/file types and required modes, current-user directory/file
ownership, single-link regular files, exact frozen home/key path, all five
enrollment fields and actual staged-key SPKI. An optional installed key is checked
even without a complete ready projection; matching staged SPKI forbids discard.
Initial missing targets are rejected. No old ready key/projection/bundle, Tailcat
file, sibling staging or other operation-directory file is removed.

After complete preflight, private start/progress and file fingerprints are
latched before key unlink. The order is key unlink, operation-directory fsync,
matching enrollment unlink, state-directory fsync. No directory is removed.
Any failure after start keeps the slot pending with phase DISCARD_PENDING and
the original remoteOutcome. All enroll/renew/retire/validate/commit paths reject
before trusted ports, cached CSR or storage delegates after discard starts.
I/O failures use TransportCredentialError code TRANSPORT_DISCARD_IO; unsafe
storage retains its existing typed codes. Preflight failures before start leave
the original operation available for normal recovery.

The same live handle may retry, allowing only its attempted missing targets.
Surviving files must retain the recorded inode/metadata/content fingerprint and
matching enrollment/SPKI. Replacements are rejected even with identical bytes.
Directory fsync is repeated as needed; a failed final fsync never completes the
operation. There is no rollback promise for two separate unlinks.

Only after both fsyncs succeed does the operation become DISCARDED and release
the slot. The returned receipt is a copy of exactly
`{"operationId": "<original UUID4>", "phase": "DISCARDED"}`. Terminal replay
returns a fresh copy before confirmation or any file access. It cannot touch a
new operation's enrollment; discarded handles cannot mutate again. Operation IDs
remain non-reusable within the workflow.

New TailnetCredentialError codes are TAILNET_DISCARD_INELIGIBLE (intent/origin/
outcome/handle-kind eligibility), TAILNET_DISCARD_MISMATCH (frozen paths,
enrollment/key binding, installed SPKI, missing or replaced targets) and
TAILNET_DISCARD_STARTED (mutation after local discard start). They retain safe
phase/remote_outcome fields and the existing detached exception boundary.

## Offline observations and failures

`read()` delegates safe local credential reading; `status()` returns its existing
non-secret status projection or null if absent. Neither consults a port.
`operation_status(handle)` is offline cached observation with exactly:
`operationId`, `kind`, `remoteOutcome`, `phase`, `errorCode`, `hasResponse`.
`remoteOutcome` is NOT_SENT, UNKNOWN, REJECTED or SUCCESS; phases are PREPARED,
RECEIVED, VALIDATED, COMMITTED, DISCARD_PENDING and DISCARDED. `errorCode` is the last client error, possibly
historical after recovery; hasResponse is a boolean. Status never contains the
cached certificate/chain PEM. Cached success remains available through the
matching enroll/renew/retire method without a new HTTP request.

`TailnetCredentialError` has `code`, `phase`, `remote_outcome`; string/repr do not
include paths, key material, PEM, owner/UID or producer diagnostics. Codes are
TAILNET_CONTEXT_UNAVAILABLE, TAILNET_CONTEXT_MISMATCH,
TAILNET_ASSOCIATION_UNAVAILABLE, TAILNET_ASSOCIATION_MISMATCH,
TAILNET_ASSOCIATION_CONFLICT, TAILNET_OPERATION_INVALID and
TAILNET_OPERATION_PENDING. Original typed client/credential failures are retained.
A gate error describes its phase and known previous remote outcome, not remote
current truth. Producer exception chains are detached. TAILNET_TLS_MISMATCH
rejects missing/wrong ready client TLS during first preparation.

When the latest transport attempt is NOT_SENT after an earlier UNKNOWN, the raw
TransportClientError contract is unchanged. The workflow raises a distinct
TailnetCredentialError with code TAILNET_ATTEMPT_FAILED, phase send,
remote_outcome UNKNOWN and attempt_outcome NOT_SENT, retaining transport_code,
retryable, http_status, operation_id, reason_code and exception_class. All public
exception chains are detached. Callers must not clean a home based on the latest
attempt alone. For other gate failures attempt_outcome/transport_code are null;
normal raw client/credential errors retain their existing types and semantics.

## Required runtime ownership, still unimplemented

One home has one transport slot across all tailnets. A real home-owned caller
must serialize all workflow instances/processes and journal original request,
staging, pin/config/TLS references and phases before send, recording known success
before later lookups. This process-local component has no global dictionary,
durable engine or journal extension. Existing enrollment.json and client attempts
are not cross-restart proof. Production caller, trusted runtime adapter, authority
service, durable caller journal and cross-process/restart recovery remain required
follow-up work. Safe discard is process-local and only origin-proven; after a
crash, missing private origin/progress cannot be reconstructed from enrollment
or a caller's claimed outcome. Such recovery remains BLOCKED without the real
owner journal. Rejected issuance retains staging until an eligible explicit
discard completes; only then may another issuance start. Callers must never
use broad local retirement to clean UNKNOWN or successful pending installation.

The workflow public exception boundary detaches both __context__ and __cause__
for the existing TailnetCredentialError, TransportClientError and
TransportCredentialError classes, including cached rejection re-raise inside a
caller's active except block, validation failures and offline read/status. It
re-raises the same error instance with its existing code/outcome and safe fields;
it does not reclassify errors, add online conditions to offline reads, or catch
KeyboardInterrupt/SystemExit. Raw client/storage public semantics are unchanged;
the existing directory preflight gains opt-in owner/presence checks for discard.
