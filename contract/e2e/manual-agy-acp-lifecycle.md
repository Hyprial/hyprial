# Manual real acceptance: Antigravity (agy) ACP worker lifecycle

## Manual-only status (coordinator decision, 2026-11-22)

This scenario is **not registered** in `contract/e2e/scenarios.json` and is
not part of the E2E release inventory: there is no sustainable managed macOS
CI provisioning for a real Antigravity OAuth token, so a registered entry
would keep the nightly credentialed gate permanently `BLOCKED` — and the
protected `.forgejo` workflows are not modified for it either. The runner
`contract/e2e/run-agy-acp-lifecycle.py` is executed **manually** on the
**final pinned SHA** of any change that touches the agy ACP wiring, and its
evidence identity is the literal `AGY-ACP-LIFECYCLE` on every `RESULT`
line — it never claims a release `E2E-<number>` id. It is also never wired
into `contract/run-all.sh --isolated-only` or the isolated gate: it needs
real credentials, so an isolated-only run must never pick it up.

## Preconditions

- [REAL ENV] The real `agy` CLI (Antigravity) is installed and on `PATH`;
  the runner pins the daemon-side launch to its absolute path. Set
  `HTTPS_PROXY` / `HTTP_PROXY` if outbound access to Google requires a local
  proxy.
- [REAL ENV] `AGY_E2E_OAUTH_TOKEN` names a readable, non-empty, regular
  (non-symlink) Antigravity OAuth token file. The runner copies it exactly
  once into its one-shot temp root and stages that copy for
  `hyprial agent provider login agy --source`; the bytes are never emitted
  into logs, reports, argv, or commits, and the temp root is deleted in `finally`.
  Without this variable the whole run reports `BLOCKED`, never `PASS`.
- [REAL ENV] Optional: `AGY_E2E_GEMINI_API_KEY` (or `GEMINI_API_KEY`) holds
  a real Gemini API key for the `--provider google` grant leg. Without a
  key that leg reports an explicit `BLOCKED` (never `PASS`); the key is
  piped via stdin to `agent secret provider-write`, never argv, never
  output. The runner strips `GEMINI_API_KEY` from the daemon environment so
  the OAuth legs cannot silently switch to the API-key auth path.
- `HYPRIAL_BIN` is pinned to this checkout's `.venv/bin/hyprial`; any other
  resolution fails `preflight.checkout-binary` before a daemon starts. The
  runner prints the source SHA it tests.
- The runner owns disposable `HYPRIAL_HOME` / `HARNESS_STATE_DIR` roots, uses
  only the isolated daemon's own socket, never reads the production daemon,
  and asserts the user's global `~/.gemini` filesystem metadata is unchanged
  before any `PASS` may stand.

## Steps

1. Start one disposable, network-isolated daemon (`IsolatedDaemon`,
   loopback, no discovery), register the probe sender, and create a local
   org space so agent-to-agent sends are writable.
2. `hyprial agent create` worker A, seed its native credential with
   `hyprial agent provider login agy --actor A --source <staged copy>`, and
   start it headless with
   `hyprial agent start agy --name A --headless -- --append-system-prompt
   <integrity nonce>`. Assert `ps` running, the exact schema-v1 desired
   state row (including the passthrough arg), and that the one-way
   native_root → session_root token projection exists under the agent's own
   home.
3. Drive one real turn whose only accepted reply path is the
   `harness_read` → `harness_reply` MCP tool chain: the reply must carry a
   fresh marker AND the nonce injected through `systemPrompt.append`
   (proving the appended system prompt reached the ACP session), and its
   message id must equal `uuid5(url, "hyprial:reply:<request id>")` — a
   turn-result synthesis fails this check.
4. Permission probe in default mode (no `--dangerously-skip-permissions`):
   the worker must call `harness_whoami` (auto-allowed harness tool), then
   attempt exactly one non-harness file-creating tool and report the
   outcome. PASS requires the reported rejection, the correct whoami
   actor, `harness_reply` as the reply path, and the sentinel file absent
   on disk.
5. Record the persisted `sessionRef`; it must be non-empty and match a
   `worker.ready` sessionId in the daemon's own worker logs.
6. Concurrency: create + login + start worker B beside the running A;
   B completes its own native roundtrip while A stays running and A's
   projected token copy is not rewritten (size+mtime fingerprint); the two
   workers' session_root projections live in distinct agent homes. Stop B.
7. Daemon restart: A recovers as a desired worker, the post-restart
   `worker.ready` names the SAME ACP sessionId (no fresh session), a second
   native roundtrip completes on the resumed session with the same nonce,
   and `harness_whoami` + `harness_reply` remain usable after recovery.
8. `hyprial agent provider logout agy --actor A`, then daemon restart: the
   native_root token is gone, the session_root token copy is removed, the
   desired worker never becomes running again (no credential-less reuse),
   and an explicit `agent start agy` is refused.
9. Google leg (when a key was supplied): `agent secret provider-write` via
   stdin, `agent secret grant --environment-name GEMINI_API_KEY`,
   `agent start agy --provider google --model gemini-3.8-flash --headless`,
   running + one native roundtrip. Without a key: explicit `BLOCKED`.
10. `finally` stops all workers and the daemon, waits for teardown, and
    deletes the temp root (token copy included); the `~/.gemini` snapshot
    must be unchanged.

## Assertions

- Every line and the final summary is classified `PASS`, `FAIL`, or
  `BLOCKED` under the `AGY-ACP-LIFECYCLE` identity; a missing token/key is
  `BLOCKED`, never a skip and never a `PASS`.
- The tested binary is exactly this checkout's `.venv/bin/hyprial` and the
  run reports its source SHA.
- The native `harness_read`/`harness_reply` closed loop is the only
  accepted reply path (uuid5 discriminator); turn-result synthesis alone
  fails.
- `systemPrompt.append` shaping is proven behaviorally: every reply carries
  the injected nonce, before and after resume.
- Default-mode `session/request_permission` auto-allows only the registered
  `harness_*` MCP tools; a non-harness tool call is rejected and has no
  filesystem effect.
- `sessionRef` is persisted and the daemon restart resumes the SAME ACP
  sessionId with MCP tools restored.
- Two concurrent agy workers project their credentials into distinct
  agent-home roots without clobbering each other.
- After `provider logout agy` and a restart, the session_root token copy is
  deleted and credential-less reuse (restore or explicit start) is refused.
- The GEMINI_API_KEY grant path either runs end-to-end or is an explicit
  `BLOCKED`.
- No token byte is printed or persisted outside the one-shot temp root; the
  global `~/.gemini` is untouched.

Run (manual, on the final pinned SHA of the change under acceptance):

```sh
HYPRIAL_BIN="$PWD/.venv/bin/hyprial" \
AGY_E2E_OAUTH_TOKEN=/path/to/antigravity-oauth-token \
uv run python contract/e2e/run-agy-acp-lifecycle.py
```
