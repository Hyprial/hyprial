# E2E-030: v7 already-running joiner and lost-reply redemption resume

## Preconditions

Run `uv run python contract/e2e/run-tailcat-org-join-resume.py`.
Go and the locked sidecar dependencies are required. Missing Go is BLOCKED;
build failures, timeouts, skipped tests and absent test selection are FAIL.
The runner compiles the current checkout's real engine/membership packages,
uses an in-process loopback labrelay, and creates disposable `/tmp/hr-*` homes,
keys and stores. No production account, daemon or credentials are used.
Allow about 90 seconds: the idle period exceeds the real 60-second refresh tick.

This is the **v7 transport equivalent** of #1855's E2E-030. The interruption is
between authority commit and receipt delivery, not between Python bootstrap
steps. The Go engines and stores are stopped/reopened; this is not a daemon
SIGKILL test or a power-loss/fsync fault test. CLI workflow continuation and
OrgFS directory publication are NOT_RUN here. Use E2E-025 for the full-daemon
positive control. The obsolete directory-row continuation window is not restored.

## Steps

1. Start the authority and an uninvited joiner. Keep the joiner control-ready,
   member-not-ready for 65 seconds, beyond one v7 refresh interval.
2. Only then issue InviteV2. Use the joiner's derived transport identity to send
   a real control redemption frame, but never read its response.
3. Observe commit through the authority's local roster operation. Require the
   joiner's engine to remain member-not-ready: it received no roster/lease.
4. Close the raw client before reusing its identity. Stop both engines and reopen
   both durable stores. No backend socket exists yet.
5. Replay the exact operation through the joiner's production membership API.
   Repeat it. Require identical terminal receipts and a fresh valid member lease.
   The authority revision and member count must remain at the original commit.
6. Start both private echo backends. Require traffic in both directions, without
   waiting for another refresh. Start a different device of the same owner and
   require `INVITE_CONSUMED` for that same bearer; it must remain member-not-ready.
7. Close only owned engines/clients/relay and remove their temporary homes.

## Assertions

- An already-running control-only joiner needs no startup allow exception.
- First redemption commits without any Zenoh socket or OrgFS holder.
- Loss of the response plus reopening both stores does not lose single-use state.
- Same-device recovery yields the original receipt and current roster lease;
  another device cannot redeem the bearer.
- The recovered membership permits immediate bidirectional business traffic.
- Exactly `TestE2E030JoinResume` executes and passes; no skip is acceptance.

Source: `sidecar/hyprial-tailcat/internal/membershiptest/join_scenarios_test.go`.
Direct diagnostic command from the sidecar directory:
`go test ./internal/membershiptest -run '^TestE2E030JoinResume$' -count=1 -v`.
Sanitized evidence goes to `e2e-artifacts/`, or `HYPRIAL_E2E_ARTIFACTS` when set.
No bearer token, raw receipt or credential-bearing address is included.
