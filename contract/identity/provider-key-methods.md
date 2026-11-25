# Provider-key daemon methods (IPC, r3)

Source of truth: `docs/design/provider-key-contract-freeze-2026-11-03.md` §5 (MethodSpec),
§5.3 (GUI admission), §7.2 (errors), HQ rulings F-1 and F-9. This page records what the
daemon declares in `hyprial.daemon.impl.provider_keys.ipc` (`PROVIDER_KEY_BINDINGS`) and
what `contract/daemon-request/openapi.json` (`x-hyprial-methods`) exports. Record and
envelope formats are in `contract/identity/provider-keys.md`.

## Common declaration

| Field | Value |
| --- | --- |
| owner | `daemon.provider-keys` |
| mode | `unary` |
| audience | `("local-operator",)` |
| http_exposed | `False` (F-9: opened only after gui-v1 lands in release, with `IdentityBinding("none")`) |
| confirmation | `none` |
| reads (`user.provider.list`, `agent.provider.status`) | `mutates=False`, `retry="safe"`, idempotency `safe` |
| writes (the other five) | `mutates=True`, `retry="never"`, idempotency `never` |

`user` is a migrated family (`MIGRATED_FAMILIES`): an undeclared `user.*` name returns
`METHOD_NOT_FOUND`. The `agent` family stays open; `agent.provider.allow/revoke/status` are
declared methods served by the registry before any legacy branch.

Every params and result object is closed (`additionalProperties: false`) at every level.
`secret` and `replaceSecret` are `writeOnly: true`, `minLength: 1`, `maxLength: 16384`,
and single line (`not: {type: string, pattern: "[\r\n]"}`). Because `maxLength` counts
code points, the handler also requires each present write-only value to encode as 1–16384
bytes of valid UTF-8 (a lone surrogate is invalid) and refuses anything else with
`INVALID_ARGUMENT` and a fixed message, before the service runs. The target agent parameter
is named `agent`, never `actor`.

## Preauthorization

Registered as `BoundMethod.preauthorize`; it runs before schema validation and again at
the top of each handler. A request is refused with `CALLER_NOT_AUTHORIZED` (fixed message,
no echo of params) unless all of the following hold:

1. the transport-authenticated origin is exactly `"ipc"` (`None`, `"daemon"`, `"worker"`
   and every other value are refused);
2. params contain neither `actor` nor `sessionRef` (checked before the schema, so such a
   request is never reported as `INVALID_ARGUMENT`);
3. when the IPC peer pid is known, it is not a registered interactive session
   (`_interactive_identity_binding_peer`); an app without that check fails closed.

The HTTP shell forwards to the daemon over the local socket, so a forwarded request
arrives with origin `"ipc"` and the request-http process as the peer. A managed worker's
`worker.channel.request` relay only admits the worker-channel allowlist (no provider-key
method) and would dispatch with origin `"worker"`.

## Methods

Param fields marked `?` are optional. Patterns: `provider` `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`;
`keyId` `^pk\.[a-z2-7]{26}$`; `environmentNames` 1–8 unique items matching `^[A-Z][A-Z0-9_]*$`;
`label` ≤128 characters with no character of Unicode general category C* (Cc, Cf, Cs,
Co, Cn: controls, format characters such as U+200B or U+202E, surrogates, private use,
unassigned); `agent` 1–512 characters (local name,
`a.` id or agent URI); `expectedAgent` `{uri: string≥1, createdAtMs: integer≥0}`.

| Method | Params | Result |
| --- | --- | --- |
| `user.provider.login` | `{provider, kind:"api-key", secret, label?, environmentNames?}` | `{key: ProviderKeyView}` |
| `user.provider.list` | `{provider?, includeDeleted?: boolean}` | `{keys: [ProviderKeyView], invalid: [{keyId, code}]}` |
| `user.provider.logout` | `{keyId}` | `{keyId, state:"deleted", revokedGrants: [{agent, agentId\|null, grantId, provider}], alreadyDeleted}` |
| `user.provider.rotate` | `{keyId, replaceSecret?}` | `{key: ProviderKeyView, reissuedGrants: integer≥0}` |
| `agent.provider.allow` | `{agent, provider, keyId?, environmentNames?, replace?: boolean, expectedAgent?}` | `{grant: GrantView, changed}` |
| `agent.provider.revoke` | `{agent, provider, expectedAgent?}` | `{grant: GrantView, changed, restart: {state: "not-running"\|"not-supported"}}` |
| `agent.provider.status` | `{agent, provider?, expectedAgent?}` | `{agent, agentId, actor, grants: [GrantStatus]}` |

`ProviderKeyView`, `GrantView` and `GrantStatus` are exactly the freeze §5.1/§5.2 shapes;
all their fields are required (nullable where the freeze says `|null`). `invalid[].code`
is `PROVIDER_KEY_SIGNATURE_INVALID` or `PROVIDER_KEY_NOT_FOUND`. `GrantStatus.code` is
`null`, one of the six provider codes, or `PROVIDER_STORE_UNAVAILABLE`.

Handler mapping to the service seam (`daemon/impl/provider_keys/api.py`): `revoke` uses
slot `grantId = provider`; the three agent methods resolve `agent` and `expectedAgent`
through one registry snapshot (`context.resolve_agent`) after the service is obtained,
so a closed gate reports `PROVIDER_STORE_UNAVAILABLE` before any agent lookup.

## Errors

"Common" = `INVALID_ARGUMENT`, `CALLER_NOT_AUTHORIZED`, `PROVIDER_STORE_UNAVAILABLE`,
`PROVIDER_STORE_CONFLICT`. "Signing" = `USER_KEY_ABSENT`, `USER_KEY_UNBOUND`,
`USER_KEY_BINDING_EXPIRED`, `USER_KEY_QUARANTINED`. "Agent" = `AGENT_NOT_FOUND`,
`AGENT_INCARNATION_MISMATCH`.

| Method | Declared errors |
| --- | --- |
| login | Common, Signing, `PROVIDER_KEY_UNSEAL_FAILED`, `PROVIDER_KEY_SIGNATURE_INVALID` |
| list | Common |
| logout | Common, Signing, `PROVIDER_KEY_NOT_FOUND`, `PROVIDER_KEY_SIGNATURE_INVALID` |
| rotate | Common, Signing, `PROVIDER_KEY_NOT_FOUND`, `PROVIDER_KEY_UNSEAL_FAILED`, `PROVIDER_KEY_SIGNATURE_INVALID` |
| allow | Common, Signing, Agent, `PROVIDER_KEY_NOT_FOUND`, `PROVIDER_KEY_RECIPIENT_PENDING`, `PROVIDER_KEY_SIGNATURE_INVALID`, `PROVIDER_CREDENTIAL_EXISTS` |
| revoke | Common, Signing, Agent, `PROVIDER_GRANT_MISSING`, `PROVIDER_KEY_SIGNATURE_INVALID` |
| status | Common, Agent (the six codes appear only in `grants[].code`) |

`PROVIDER_STORE_CONFLICT` is declared on the reads as well so every method shares the
Common set. With the gate at `legacy` (default) every method returns
`PROVIDER_STORE_UNAVAILABLE`. An undeclared code raised by the service becomes
`DAEMON_ERROR` at the registry. Any non-`DaemonRequestError` exception becomes
`DAEMON_ERROR` with a fixed message, and a declared error whose message or data would
contain a write-only value is re-raised with the same code and a fixed message.

## Authority gate (freeze §8.7)

The freeze names the switch `provider_keys.authority`; that is its logical name. It is
persisted in `H/settings.json` as the camelCase section `providerKeys` with key
`authority` (like `workerProxy`):

```json
{"providerKeys": {"authority": "owner-keys"}}
```

One reader, `hyprial.daemon.impl.provider_keys.storage.gate.authority`, serves the seven
methods, managed-worker launch injection and the legacy `agent.secret.*` adapter. It is read
on every use, never cached, and has no environment override and no writer.

| `settings.json` | Authority |
| --- | --- |
| no home, no file, no `providerKeys` section, or no `authority` key | `legacy` |
| `authority` exactly `"legacy"` or `"owner-keys"` | that value |
| unreadable or non-JSON file, non-object top level or section, any other value | refused, `PROVIDER_STORE_UNAVAILABLE` |
| a top-level `provider_keys`, `provider_keys.authority` or `providerKeys.authority` key (the logical name spelled literally) | refused, `PROVIDER_STORE_UNAVAILABLE` |

A refused gate never falls back to `legacy`: the seven methods return
`PROVIDER_STORE_UNAVAILABLE`, launch reports `AGENT_SECRET_RESOLUTION_FAILED`, and the
legacy `agent.secret.*` adapter applies its §8.5 mapping.
