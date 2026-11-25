# Tailnet credential bootstrap and preparation component

Normative design: `docs/design/zenoh-tailnet-credential-runtime-contract-0552.md`
at `e73dddc251f6113273d1b80203aba0a1ff6fed7c` (SHA256
`22ec68696de473a9176e89a2303acac8b364f20cbe00b442c0583a032a888adc`).

This increment adds one identity export, `TailnetCredentials`. Its nested
`DialTarget`, `Descriptor`, `FrozenTransport`, `MtlsReference` and
`MemberProjection` accept only recursively immutable validated values. Network
owner and a member's self-reported device UID confer no application ownership.

`golden/bootstrap-v1.json` fixes exact request/response frame bytes and their
descriptor. Its CA certificate is synthetic public fixture material; no private
key is committed. The client exchanges one bounded `HCA1\n` frame (network-order
uint32 length, maximum 65536 UTF-8 JSON bytes) on a daemon-supplied authenticated
socket, requires EOF after the response, and checks every authority field,
tailnet ID and remote port before installing a pin. Production authentication
still requires the future daemon/sidecar adapter; socketpair tests supply a
synthetic trusted dial boundary and do not prove that production boundary.

The socket exchange uses the remaining portion of a ten-second clock started
before dial. The synchronous daemon-supplied `dial` callback itself cannot be
interrupted by this client; it may block longer than that clock. End-to-end dial
deadline/cancellation remains OPEN for the daemon adapter. This component does
not establish a ten-second bound for the complete bootstrap operation.

The local component supports durable enroll preparation and the explicitly
approved authority-local issuer path. It verifies the fresh full local network
tuple against the authority pin, initializes a private per-tailnet CA, and calls
the internal issuer without self-dial or a public trusted flag. Local execute
durably latches SENDING/UNKNOWN before entering the handler, then saves the exact
operation receipt as RECEIVED. A new local process can replay that same request
and receipt. It does not create an active generation or claim local ready.
Non-authority execute, renew/retire/current-key and discard remain unsupported;
remote handle/peer_member reject all caller-constructed connection objects until
the actual daemon authentication adapter is implemented.

`golden/local-enroll-v1.json` captures a real synthetic local enrollment request,
signed association/credential and exact durable rejection response. All keys in
the golden are public CSR/certificate material; private keys remain under /tmp.
The device UID is member-self-reported metadata, never application ownership.

Authority current state, binding history, metadata and exact success/business
rejection receipts are one SQLite image. SQLite only opens `:memory:`; image
load and atomic fsync publication use the slot's safe descriptor-relative file
operations while holding the OS lock. No SQLite disk pathname is opened, and
unexpected journal/wal/shm files fail closed. Image capacity is 16 MiB, checked
before publication; overflow preserves the previous image and returns an
operational failure without a durable operation receipt. Memory cost scales
with the image limit plus SQLite and serialization copies, not a measured RSS
ceiling. Platforms without serialize/deserialize fail closed without a path
fallback. A partially initialized CA/store is preserved and may require manual
engineering recovery; it is not reset or silently assigned a replacement CA.

Local status and journal inspection are observations, not network readiness.
An existing unsupported active generation is preserved and cannot be reported
ready. Malformed pointers and unsafe files are typed failures, not absence.
Journal format `hyprial.tailnet.credential-journal.v1` binds canonical home
dev/inode, exact tailnet ID, device network tuple, immutable descriptor, request
bytes/digest and staging inode/owner/mode/nlink/digest. Copying it across homes or
replacing a staged key with byte-identical material does not establish provenance.

Run the focused component with `uv run pytest
tests/identity/devices/tailnet/runtime/`. The isolated contract runner integration,
remaining mutation goldens, active-generation commit, remote replay/cancel
recovery, daemon composition and real networking remain T7 work. Allen's approved
owner-confirmed re-registration and UUID-verified migration are not automated by
this component. Existing `contract/identity` v1 semantics are unchanged.
