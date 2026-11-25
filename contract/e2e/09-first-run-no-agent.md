# E2E-009: a clean first run creates no agent

Status: isolated automation. A disposable home runs `hyprial init` (and the
daemon restart that follows it) and proves that first run creates no agent, no
adapter and no pin. The retired `hyprial init onboarding` group and every old
step id are refused before any daemon contact.

## Preconditions

- A disposable `HYPRIAL_HOME` and `HARNESS_STATE_DIR`, both isolated from any
  real home, plus an `e2e-` node id and the `e2e` owner: the daemon runs as a
  test node and reports network isolation.
- No App credential, tenant authorization, harness login or model reply is
  involved; nothing here needs a real Lark App.

## Steps

1. Run `hyprial init --no-service --json` on the empty home and wait for a
   daemon that reports ready.
2. Read `hyprial agent list --json` and `hyprial adapter list --json`: both are
   empty, and `agent list --include-user-agent` holds no agent called
   `squire`, `assistant`, `default` or `worker`.
3. Run `hyprial init --no-service --json` again; it reports the running daemon
   and step 2 still holds.
4. Stop the daemon, start it again with `hyprial init --no-service --json`, and
   repeat the empty lists: a restore creates nothing either.
5. Run every retired command — `hyprial init onboarding`, `plan`, `apply`
   (`--auto` and one per old step id), `harnesses`, `harness-select`,
   `harness-install` — and assert exit 2 with no business result: usage text on
   stderr without `--json`, and the CLI's one `INVALID_ARGUMENT` error object
   (`No such command 'onboarding'`) with `--json`.

## Assertions

- First run creates no agent, no adapter, no route and no pin; no fixed
  first-run name (`squire`, `assistant`, `default`, `worker`) exists.
- Repeating `init` and restarting the daemon create nothing as well.
- Every retired onboarding subcommand and every old step id
  (`runtime-ready`, `default-agent`, `worker-agent`, `squire`, `lark-app`,
  `lark-authorize`, `lark-route`, `lark-pin`, `lark-roundtrip`) exits 2 with no
  business result; no alias, compatibility path or legacy step id is offered.
- Connecting a Lark App, authorizing scopes, adding a route, pinning an agent
  and verifying a real round trip are explicit operator actions
  (`docs/guides/onboarding-checklist.md`); this scenario makes no claim about
  them.
