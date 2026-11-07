# org.create recovery contract

`org.create {org}` is idempotent for one creator after any completed OrgFS
step. A retry by the same owner on the same node resumes the durable local
create intent and returns one directory space plus one bound ACL space. A
retry by another owner returns `ORG_ACTION_NOT_AUTHORIZED` and performs no
OrgFS write.

The daemon executes an admitted IPC request to completion before writing the
response. Therefore client timeout or disconnect is an unknown outcome, not
cancellation; callers retry with the same org name. Once the directory space
exists, creation writes `directory/org.json` with `state: creating` before
creating the ACL space. It writes `state: active` only after all create steps,
including the device publication attempt, have finished.

`org.list` exposes lifecycle state. Only explicit `state: creating` and
`state: partial` markers classify an otherwise bound org as incomplete. A
missing marker or malformed marker uses the legacy classification: a space
with an `aclSpaceId` binding is `legacy-active`; an unbound space is `partial`.
Malformed means unparseable JSON, a parsed non-object, an object with absent or
null `state`, or an object with empty or non-string `state`. Explicit `active`
and `deleted` retain their normal meaning. Any other non-empty string state
fails closed as `partial`.

Aggregate `org.network` keeps unavailable orgs in the response and uses
`creating`, `partial`, or `policy-denied` in `unavailableOrgs[].reason`.
Every malformed marker and unknown explicit state appears in `dataProblems`.
Each object is exactly `{org, path, reason, value?}` and uses
`path: directory/org.json`. The complete lifecycle-marker reason set is
`invalid-json`, `invalid-object`, `missing-state`, `invalid-state`, and
`unknown-state`; `value` is present only for `unknown-state` and contains the
reported marker value. `contract/org-network/data-problem-reasons.json` pins
this set.

Classification order is: a genuinely missing marker -> legacy classification
without a data problem; unparseable JSON -> `invalid-json` plus legacy
classification; a parsed non-object -> `invalid-object` plus legacy
classification; absent or null `state` -> `missing-state` plus legacy
classification; empty or non-string `state` -> `invalid-state` plus legacy
classification; an unknown non-empty string state -> `unknown-state`,
`partial`, and its value in `dataProblems`; explicit `creating`/`partial` are
refused; explicit `deleted` keeps the dev behaviour. The daemon emits a warn
event for each observed data problem, and `hyprial doctor` reports them as
`org-metadata`. `missing-acl-binding` remains an `org.repair` candidate reason,
but is not an `org.network` unavailable reason: the lifecycle classification
for that unbound space is `partial`.

The create result includes `published` and the compatibility alias
`devicePublished`. A publication failure returns both as `false`; `org create`
then emits `ok:false` and exits 1 without claiming that device publication
succeeded. The completed org remains retryable and a same-creator retry does
not create another directory or ACL space.

`org.repair {org?, apply?}` is the recovery inspection surface. `apply`
defaults to `false`. Its result is:

```json
{
  "apply": false,
  "candidates": [
    {
      "org": "group-example",
      "spaceId": "uuid",
      "reason": "missing-acl-binding",
      "repairable": true
    }
  ],
  "repaired": []
}
```

Candidates are unique directory spaces whose ACL binding is missing, unknown,
or owner-mismatched, or whose bound ACL grant document or directory lifecycle
entry is missing. `repairable` is true only when the directory owner is the
current daemon owner and any durable intent belongs to this daemon node. With
`apply: true`, each repairable candidate is resumed
through `org.create`; `repaired` contains those standard create results.
Non-repairable candidates are never modified. The command does not delete
spaces.
