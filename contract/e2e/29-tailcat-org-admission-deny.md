# E2E-029: v7 invite admission denial and business-stream revocation

## Preconditions

Run `uv run python contract/e2e/run-tailcat-org-admission-deny.py`.
Go and the locked sidecar dependencies must be available. A missing Go binary
is BLOCKED. Build/test failures are FAIL, not skips. The runner compiles the
current checkout's real engine and membership packages. It starts real Tailcat
servers/clients and an in-process loopback labrelay, with disposable `/tmp/hr-*`
key files, membership stores, Unix backends and slots. It uses no production
account, daemon, credentials, host tailscale, or public relay.

This is the **v7 transport equivalent** of #1855's E2E-029, not full CLI/OrgFS
acceptance. The private backend is an echo server, not Zenoh. E2E-025 remains the
full-daemon invite/join/OrgFS positive control. Live Casdoor, zero Zenoh subscriber
puts and preservation of a local OrgFS replica are NOT_RUN here.

## Steps

1. Start authority Alice, invited Bob and unjoined Mallory. Alice initializes
   the authoritative table, issues Bob an InviteV2, and Bob redeems via :7448.
2. Bob opens a real business connection to Alice's private echo backend.
3. Issue an invitation for Mallory, then use a raw Tailcat client to send a
   corrupted token. Require the correlated `INVITE_INVALID` control reply.
4. Submit the valid token with a forged derived peer key in JSON. Require
   `MEMBER_CONFLICT`; the actual wire key is authoritative.
5. Bypass the cooperative outbound gate: Mallory's raw client directly dials
   Alice's :7447 and writes data. Require closure, not a read timeout. Bob's
   existing echo stream must still work, and backend connection count must
   remain unchanged. The authority roster must not change.
6. Confirmed authority-local revoke tombstones Bob. Require closure of his held
   stream, then of a fresh stream through his still-mapped old local slot.
   Do not refresh Bob's roster before that fresh-stream probe.
7. Require `MEMBER_REVOKED` when Bob replays his redeemed invitation. Confirm
   Mallory's control endpoint is still reachable with an invalid-token reply.
8. Close owned engines, clients, sockets and relay; remove their temporary homes.

## Assertions

- Honest redemption and business traffic provide a positive control.
- Invalid bearer and forged JSON peer identity fail in-band without mutation.
- Real inbound admission denies an unjoined transport before backend acceptance.
- Revocation closes both existing and new business streams; consumed invitations
  cannot resurrect the tombstoned member.
- Control remains reachable independently of business admission.
- Exactly the named Go test must execute and pass; zero tests and skips fail.

The source is `sidecar/hyprial-tailcat/internal/membershiptest/join_scenarios_test.go`
(`TestE2E029AdmissionDeny`). Direct diagnostic command, from the sidecar directory:
`go test ./internal/membershiptest -run '^TestE2E029AdmissionDeny$' -count=1 -v`.
The runner writes sanitized JSON under `e2e-artifacts/` (override with
`HYPRIAL_E2E_ARTIFACTS`); it never writes invitations or full Tailcat addresses.
