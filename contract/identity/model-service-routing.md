# Owner-selected model service routing

An explicit local operator can save a Key with its model service address through
`agent.secret.provider-configure`. This extends the existing user-provider Secret
store; it does not create an identity issuer, owner-key authority or OrgFS record.
The production owner-key authority is not supported by this operation.

## Save and inspect

The HTTP-exposed unary write requires `entryId`, `fieldName`, `endpoint`,
`protocol` and `modelProvider`; optional write-only `value` replaces that field.
Omitting `value` requires an already saved nonempty field. Unknown parameters
are rejected. Origin must be trusted local IPC and the peer must not be an
interactive Agent; browser-supplied actor/session identity is not accepted.
The existing HTTP operator bearer and admission boundary remain required.

Optional boolean `createOnly` defaults to false, preserving explicit updates.
When true, any existing entry is rejected with `INVALID_ARGUMENT` before writing,
including an entry containing a different field. The existing store lock and
exclusive atomic publication enforce this condition; concurrent creates cannot
replace one another. GUI creation uses this option in one request, without a
client-side catalog-read/write sequence. Rejection does not reveal the old value.

The first supported selection is Pi / deepseek / deepseek-flash with protocol
`openai-completions`. Configuration admits only `modelProvider=deepseek` and
that protocol. It does not infer model compatibility from a successful probe.
Other models, vendors, harnesses and interactive custom-route launch are not
supported. The interactive launch path explicitly refuses custom routed grants.
Transfer export marks routed credentials as requiring reauthentication without
exporting their values: the existing transfer envelope cannot carry their route.
The target owner must configure and grant the service again before startup.

An endpoint is an absolute HTTPS base URL without userinfo, query, fragment,
backslash, whitespace or control characters. Literal loopback HTTP is allowed
for explicitly configured local services; this does not grant remote callers
access to the local operator API. Credentials belong in `value`, never the URL.
The save operation does not connect to the endpoint or test the Key.

Successful configuration returns only `ok`, `entryId`, `fieldNames` and `routes`.
The existing catalog adds optional `routes` for configured entries; ordinary
entry receipts retain their shape. A route maps a saved field name to
`{endpoint, protocol, vendorId}`. Reads do not return values, private paths or
Agent authorization. The ordinary writer's `fieldNames` reflects the saved
record, including preserved fields after rotation.

## One atomic record

Ordinary files stay flat JSON string dictionaries. Explicit configuration writes
one private record at the existing `H/secrets/providers/<entryId>.json`:

```json
{
  "$schema": "hyprial-model-secret/v1",
  "values": {"key": "synthetic-example"},
  "routes": {"key": {
    "endpoint": "https://model.example/v1",
    "protocol": "openai-completions",
    "vendorId": "deepseek"
  }}
}
```

The reserved `$schema` cannot be a valid legacy field name. Unknown schemas,
extra envelope/route fields, unsafe files and routes without saved fields fail
closed. `ProcessFileLock` serializes cooperating writers; the kernel private-file
writer publishes values and routes in one atomic replacement with its existing
flush semantics. There is no separate route file, high-water mark or global
migration. The file is bounded at 1 MiB and retains the existing private access
requirements. Catalog scans ignore the non-JSON `.write.lock`.

Ordinary writes to an already configured record retain its routes and other
fields. A failed write is not reported as success; filesystem failures after
replacement may mean the new record is visible despite the failure. The caller
must inspect/retry explicitly and cannot assume rollback. Reads use one private
descriptor so values and routing come from the same record snapshot.

## Grant and launch

The existing grant database adds internal boolean `route_required`, default false
for old rows. It can be true only for user-provider grants. The daemon derives
it from the selected saved field before the registry command; caller extensions
cannot set or unset it. The registry persists it in the existing transaction
and retains incarnation/revision/revocation checks.

A routed field with a false grant, or a true grant with a flat/missing route,
refuses resolution. Configuring an already granted plain Key therefore requires
explicit re-grant with the next revision. This never silently changes grants on
other Agents. Later Key rotation preserves the existing true grant. If an older
writer replaces the record with flat data, the true grant refuses startup until
the owner reconciles it; explicitly re-granting flat data adopts ordinary
deployment routing again. A configured record cannot be granted as a whole
file. Ordinary legacy whole-file text remains supported, without route metadata.

The daemon worker checks the explicit model selection against one routed grant,
whose only destination environment name is `DEEPSEEK_API_KEY`. A second grant
supplying that name, multiple routed grants, an implicit or unsupported selection
are refused before handing the value to a child. The validated route overrides
the deployment `HYPRIAL_OPENAI_BASE_URL` in the trusted base environment and
reuses the existing controlled Pi model definition. It is never injected as a
secret grant's reserved `HYPRIAL_*` name. Consumers without route admission
cannot obtain a routed environment through `ResolvedSecret.environment()`.

This binds one read snapshot, not an authorization lease. Rotation/revocation
after resolution does not erase values already held by a running child. The
contract does not defend against the same OS owner modifying all private state,
promise rollback protection, or make an older binary understand the new schema.
GUI editing and an installed browser-to-model reply require separate evidence.
