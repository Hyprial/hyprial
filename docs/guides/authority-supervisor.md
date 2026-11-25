# Member launch policy (cell reconcile)

The standalone authority supervisor is no longer started by the daemon
(I6-5a). `HYPRIAL_AUTHORITY_SUPERVISOR`, `HYPRIAL_AUTHORITY_STORE` and
`HYPRIAL_AUTHORITY_AUDIT` have no effect, and the `authority.*` requests were
retired in I6-4. A harness member's launch-time allow/deny policy now lives
in its agent cell document and is applied by the member reconcile loop.

## Declare a policy

Write the rules under the harness member in `cell.members`:

```json
{
  "cell.members": {
    "agent:alice:mac:worker": {
      "allow": ["Read", "Bash(hyprial:*)"],
      "deny": ["Bash(hyprial daemon:*)"]
    }
  }
}
```

Apply it with `hyprial agent cell apply <agent> --expected-revision N --file CELL.json`
(or `agent.cell.update`). The rule strings are passed to the harness verbatim.
A member without `allow` and `deny` declares no policy.

## Template rules

The daemon has no built-in allow or deny list. When a `claude` harness member
declares neither `allow` nor `deny`, the agent cell registrar writes the
`managed-worker` template rules into the document once (`allow`: the
`mcp__harness-bridge__*` tools plus `Bash(hyprial:*)`; `deny`: the daemon,
init, org, agent-destroy and upgrade commands). It does this when the
member's harness becomes known (its local launch record) and right before
`hyprial agent start` launches it. An owner edit is never overwritten: a member
that declares either key is left alone. Members of other harnesses are not
seeded. A seed that cannot be written (cell runtime unavailable, slow or
stale) is logged as `cell.member_policy_seed_failed` and the start proceeds
with the document as it is.

## What reconcile does

- A revision that changes only a running member's policy plans one
  `cell.member.policy.apply` effect: stop the member, start it with the new
  policy, and accept it only when the worker's ready frame acknowledges the
  same policy digest and version (the cell revision).
- On a failed acknowledgement the effect relaunches the last verified policy;
  the member keeps running it, stops admitting new work, and the cell is
  `degraded` until a later revision applies or reverts the policy. The
  document is never rewritten.
- A revision whose `allow`/`deny` has a wrong type, a duplicate rule, or a rule
  in both lists is rejected as a whole (`members-policy-type`,
  `members-policy-overlap`); the last effective revision stays in force.
- An agent has one harness. Its own member (the member named by the agent's
  URI) is the harness `hyprial agent start` runs, `<harness>:<agent name>`.
  Reconcile never launches a second one: it takes over the running harness
  when it already runs the declared configuration and acknowledged the
  declared policy, and otherwise restarts that same harness through the
  ordinary stop and start path, resuming its learned session. If the
  session cannot be resumed, nothing is stopped: a policy change leaves the
  previous policy running and the cell `degraded`. A `kind` in the document
  that differs from the running harness is refused before anything is
  stopped (switch harnesses with `hyprial agent start --harness`): the agent
  keeps running and the cell is `degraded` until the document is reverted.
- Reconcile manages only members declared in `cell.members`; a claude
  agent's own member is declared by the template seed. At daemon start it
  waits until restore has relaunched the agents, then takes over what
  restore started. A harness whose configuration or acknowledged policy
  differs from the document is restarted at most once for a given declared
  configuration; if it still differs, the member is `failed` and later
  revisions that leave that configuration unchanged do not restart it
  again. A revision that changes the configuration gets one restart of its
  own; the document matching the running harness, or a daemon restart,
  clears the state (KP-122). An agent that restore left down (its last
  start failed) stays down whatever the document says, including the
  seed's revision and later edits; only `hyprial agent start` starts it
  again.
- A restart keeps the agent's launch intent. Reconcile stops the harness
  without removing its desired-state row, so a daemon killed between the
  stop and the start restores the agent at the next boot. When the new
  configuration fails (for example its policy is not acknowledged, or a
  `model` change does not come up), the harness is relaunched with the
  configuration and policy it ran before; the member stops admitting new
  work and the cell is `degraded`. If that relaunch fails too, the agent is
  down and the member `failed` until the next daemon start, a revision that
  changes its configuration, or `hyprial agent start` (KP-131).
- A member kept running after a failed start is not marked healthy by a
  later revision that leaves its configuration unchanged; it stays
  `degraded` until it acknowledges the declared rules. The relaunch does not
  pin anything: a later `hyprial agent stop` and `hyprial agent start` reads
  the document's rules again, and reconcile checks that launch's
  acknowledgement before it admits work again.
- Every launch of the member reads its policy from one place: the policy
  reconcile last published for that launch, otherwise the member's rules in
  the local copy of its cell document (a read-only cache read; OrgFS is not
  contacted). This covers `hyprial agent start`, recovery after a daemon
  restart and crash restarts.
- Without a usable document policy (no cell yet, an unreadable cache, a
  document with structure problems, or no rules declared) the worker starts
  with its harness-bridge tools only, and the reason is logged as
  `cell.member_policy_unavailable`. No policy is ever made up in code.

## Apply outcomes

Each outcome is a cell observability event on the daemon log (domain
`cells`); there is no separate audit file:

- `cell.member_policy_apply_started` when a policy change begins;
- `cell.member_policy_applied` when the worker acknowledged the new policy
  (`action=apply`), a first launch with a policy was acknowledged
  (`action=ensure`), or reconcile took over a running harness that had
  already acknowledged it (`action=adopt`);
- `cell.member_policy_apply_failed` when it was not; `rolledBack` says whether
  the last verified policy was relaunched (`rollbackCode` names a failed
  rollback).

Every event carries `cell`, `agent`, `member`, `harness`, `revision` (the
policy version), `policyDigest` and `previousPolicyDigest`. The standalone
AuthoritySet store, apply loop and JSONL audit are deleted (I6-5b).

## Scope

Only `claude` (Agent SDK, headless) has a policy mapping
(`allowedTools`/`disallowedTools`). A member of another harness that declares a
policy fails and the cell is `degraded`; it is never launched with the policy
silently dropped. Migration from AuthoritySet tasks is manual; see
[cell-authority-migration](cell-authority-migration.md).
