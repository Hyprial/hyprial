# Agent restart restore policy

The daemon stores restart policy in `state/agent-restore-policy.json`. The
schema-v1 document contains `restoreThresholdMs` and optional per-agent values
of `active`, `always`, or `never`. A missing document uses the 12-hour default.
An unreadable or malformed document is fail-open: running desired connectors
are restored and doctor reports `restore-policy-degraded`.

Idle suppression and human-action blocks are durable rows in
`state/agents.sqlite3`, fenced by the agent entity token. Suppression is also
fenced by a digest of the desired harness specification. Explicit start, new
pending work, or an `always` policy releases dormancy.

At daemon startup, a legacy `state/agent-keep.json` is migrated once: each
listed agent receives policy `always`, including entries that previously had
policy `never`, and the legacy file is removed only after the policy write
succeeds.

`hyprial agent unblock <name>` is the supported block-release operation.
Automatic release after a provider probe is future work: current quota and
authentication probes do not provide a trustworthy agent-incarnation mapping,
so using them would risk clearing the block on the wrong agent.
