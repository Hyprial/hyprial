# hyprial-tailcat

The sidecar owns Tailcat membership and admission. The daemon uses the closed
stdio-v6 protocol. Each tailnet has one fixed authority, a 300-second member
lease, and a 60-second refresh interval. There is no automatic authority election.

The normative contract is
[tailcat-cycle-fix-contract-2026-10-27.md](../../../docs/design/tailcat-cycle-fix-contract-2026-10-27.md).
Shared wire examples are in [contract/sidecar-tailcat/golden](../../../contract/sidecar-tailcat/golden/).

## Commands

```text
hyprial-tailcat version
hyprial-tailcat genkey --out <absolute-path> [--force]
hyprial-tailcat forward
```

`genkey` exits with code 3 if the key already exists and `--force` is absent.
`forward` accepts v6 only. A version mismatch returns
`SIDECAR_PROTOCOL_MISMATCH`; unknown operations or fields return `BAD_REQUEST`.

## Managed lifecycle

`up` requires `requestId`, `keyFile`, `addressFile`, `membershipStore`, `owner`,
`deviceId`, and a private `unix:/absolute/path` inbound backend. Its parent must
already be owned by the process user and have mode 0700. The backend socket need
not exist: sidecar and control readiness precede membership, Zenoh, and OrgFS.
Managed Windows operation is not supported by this private-Unix contract.

Membership operations are `membership-init`, `roster-fetch`, `member-announce`,
`member-revoke`, `membership-status`, `invite-create`, `invite-redeem`, and
`authority-repin`. Local-only operator operations cannot run over control-v1.
`allow`, `allowAny`, and public `admission:any` are retired. Only the internal
control listener bypasses business membership admission. Existing standalone
engine/servicehost allow configuration is separate from the managed-v6 entry.

An empty announcement map is expanded with derived public keys for the live
roster's servers. Expansion is persisted beside the store as `.announcements`,
so retries keep the same payload after a restart. The membership file and its
operation receipts are committed together. Revoke and re-pin require confirmation,
revision agreement, and a private pre-change backup. Re-pin also requires a
quiescent network and does not reopen admission until restart.

A non-authority process never restores a lease from disk. Expiry closes business
connections and mappings, but leaves control available for recovery. An active
membership in another tailnet can keep the same peer admitted.

## Transport invariants

Mesh, service, and control dials share one client pool per `serverPublic`.
The master client key never becomes a DERP client identity. Address handoff closes
the old transport before activating a replacement with the same derived key.

Fixed outbound slots use absolute Unix paths or loopback `tcp:host:port` ids.
`tcp:127.0.0.1:0` requests an assigned port. Unbound slots hold connections unread
for `slotHoldMs` (default 10000). `map-peer` binds an admitted peer to a slot;
`unmap-peer` closes its streams but keeps the slot listener. Running state reports
resolved slot ids. Unix growth slots must share the configured directory and use
`slot-NN` names. TCP growth uses port zero.

## Private data

Keys, PSKs, addresses, and invite tokens must not enter argv, logs, telemetry,
public status, or unsolicited events. Address files and membership stores use
0600 files in private directories. Explicit correlated membership results can
carry addresses or invite tokens only on the private owner stdio channel;
control replies can carry rosters. These payloads must not be logged.

The mobile key-generation ABI returns private text only to the same-process native
caller. See [keygen ABI v1](../../../docs/notes/tailnet-keygen-abi-v1-20261022.md).

## Validation

```sh
go vet ./...
go test ./...
go test -race ./internal/membership ./internal/membershiptest ./internal/engine ./internal/serve
```

From the repository root, the real Zenoh slot runner uses isolated processes and
local labrelay only:

```sh
uv run python sidecar/e2e-zenoh-slots/run_slots_e2e.py
uv run python sidecar/e2e-zenoh-slots/run_slots_e2e.py tcp
```

`internal/membershiptest` covers real control bootstrap with absent backends,
fresh invites, live revocation, and concurrent control/mesh/service traffic to
three servers. Membership unit tests cover restart, receipts, lease deadlines,
backup failures, malformed stores, and operator safeguards. Stale Tailcat status
entries are not proof of admission: revocation tests check actual stream closure.
