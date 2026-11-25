# Manage persistent harness arguments

Persistent harness arguments are per-agent, per-harness launch preferences.
They are durable configuration, not the argv of a process that is already
running. Explicit options supplied to a later start take precedence over the
stored group.

Phase 1 supports managed argument groups for `claude`, `codex`, and `pi`.
Adapter providers and the `dsh` and `jev` launch contracts are
not edited through this interface.

## Safety boundary

- `agent.harness-args.set` and `agent.harness-args.clear` accept only the
  verified host local operator. A bound agent cannot call either write method.
- The four `agent.harness-args.*` methods are local daemon requests and are not
  exposed over HTTP in phase 1.
- `set`, `clear`, and `--ignore-stored-args` do not stop, restart, attach, or
  otherwise change a running process.
- A stored change applies to the next managed start or restore. Use the
  separately authorized lifecycle procedure if an existing process must be
  replaced.
- Do not store credentials, API keys, authorization headers, or secret-bearing
  settings in harness arguments. Audit rows redact supported sensitive forms,
  but redaction is not a credential store.

## Inspect and validate

Show the complete stored map and its current policy status:

```sh
hyprial agent args show <name>
hyprial agent args show <name> --json
```

Validate a proposed group without changing the agent:

```sh
hyprial agent args validate claude -- --model sonnet
hyprial agent args validate codex --json -- -a on-request -s workspace-write
```

CLI options such as `--json`, `--reason`, and confirmation flags go before the
double-dash separator. Every token after `--` is passed to the harness policy.
`validate` exits 0 only when `valid=true`. Dangerous classification alone does
not make otherwise valid arguments invalid.

## Replace one group

`set` replaces the whole selected group. It does not append individual tokens:

```sh
hyprial agent args set <name> claude --reason "pin model" -- --model sonnet
hyprial agent args set <name> pi --json -- --offline
```

The CLI first reads the complete current map, validates the proposed group,
then sends that map as `expectedHarnessArgs`. If another writer changes any
group in between, the daemon returns `HARNESS_ARGS_CONFLICT`. The CLI does not
retry: run `show`, review the new map, and issue a new `set` or `clear`.

An empty CLI `set` is refused to prevent accidental deletion. Use `clear`
explicitly.

Dangerous arguments require an explicit decision. An interactive terminal
shows token and category and asks for confirmation. Non-interactive and JSON
callers must use `--yes-dangerous`:

```sh
hyprial agent args set <name> claude --yes-dangerous \
  --reason "approved exception" -- --settings settings.json
```

The flag only supplies confirmation; it does not bypass semantic validation.

## Clear stored groups

Clear one group:

```sh
hyprial agent args clear <name> claude --reason "retire legacy flags"
```

Clear every group, including historical or no-longer-supported names:

```sh
hyprial agent args clear <name> --all --yes-all --reason "reset launch policy"
```

An interactive terminal may confirm `--all`; non-interactive and JSON callers
must pass `--yes-all`. Clearing a missing group is an unchanged success.

## One-shot ignore

Use the one-shot flag when stored state must be excluded from exactly one
start or resume attempt:

```sh
hyprial agent start claude --name <name> --headless --ignore-stored-args
hyprial agent start claude --name <name> --resume <session-id> \
  --ignore-stored-args
```

The stored map remains unchanged and no harness-argument audit row is written.
A later start without the flag sees the stored map again. Invalid stored state
therefore fails again with `HARNESS_ARGS_INVALID` until an operator clears or
replaces it.

## Migration and rollback

For an agent that already carries historical arguments:

1. Run `show` and retain the complete map in the change record.
2. Run `validate` for each managed group you intend to keep.
3. Replace a valid group with `set`, or remove an invalid/historical group
   with `clear`. Include `--reason` for operator traceability.
4. Do not restart an existing process as part of the edit. At the next
   separately approved start, inspect the warning and launch result.
5. To roll back, use `set` with the retained prior group. CAS still applies,
   so re-run `show` immediately before the rollback.

The audit table is additive. Old daemons ignore it; no destructive schema
rollback or automatic data migration is required.

## Version skew

A new CLI talking to an old daemon reports that persistent harness arguments
or `--ignore-stored-args` are unsupported and instructs the operator to upgrade
the daemon. It does not retry a write or silently drop the option.

An old CLI continues to use the existing start path against a new daemon. It
does not send `ignoreStoredHarnessArgs`, so stored arguments retain their normal
merge behavior.

JSON mode emits exactly one result object on stdout. A failed command has
`ok=false` and exits 1. Human warnings and confirmations use stderr.

Acceptance evidence and remaining gaps are recorded in
[the 2026-11-09 evidence matrix](../notes/harness-args-acceptance-2026-11-09.md).
