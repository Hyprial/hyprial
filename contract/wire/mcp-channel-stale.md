# MCP channel build skew (wire contract)

A Claude interactive session keeps its stdio MCP channel process across a
hyprial daemon upgrade.  That process compiles the tool schema it advertises (for
example whether `harness_send` has a required `summary`), so a long-lived
channel can be older than the daemon it talks to even when both report the
same `channelProtocolVersion`.

## Registration

`session.register` keeps the existing `channelBuildVersion` field.  Every
registration answer may add:

```json
{
  "channelStale": {
    "code": "MCP_CHANNEL_STALE",
    "channelBuildVersion": "<process build>",
    "daemonVersion": "<daemon build>",
    "restart": "restart the Claude session ..."
  }
}
```

The key is present exactly when the process build differs from the daemon
build.  It is additive: an older channel that does not understand it still
registers, and the daemon records the mismatch for `ps`/doctor.

`ps.interactiveSessions[]` exposes `channelBuildStale: true|false` for a
Claude channel that registered a build token.  `doctor` reports the count as
`mcp-channel-generation.metrics.buildStale` and points at the restart command.
`channelProtocolVersion` alone is not enough to detect this skew: protocol 2
predates `summary`.

## Tool calls

A channel that received `channelStale` refuses its own tool calls with the
same `MCP_CHANNEL_STALE` code rather than letting the old MCP schema fail at
the input edge (`SUMMARY_REQUIRED`).  The refusal tells the operator to
restart the Claude session; the channel never restarts itself because that
would terminate the conversation.

An already-running process built before this contract cannot discover its own
staleness; after an upgrade the daemon's `ps`/doctor warning is the source of
truth and one manual session restart is required.

## Harness start failure

`HARNESS_START_FAILED` is the stable code for a harness start that the
lifecycle manager accepted but that refused or died during its own start
precondition.  The CLI reports it as `ok:false` and exits non-zero.  It is
distinct from `DAEMON_START_FAILED` (the daemon itself failed to start).

`lifecycle.start` waits for `ready=true` or a recorded `last_error`,
whichever comes first, bounded by `START_FAILURE_OBSERVATION_SECONDS` (1
second).  A successful start returns as soon as readiness is observed, with no
unconditional one-second delay.  If the window expires with neither readiness
nor an error, the start answer carries `"readiness": "unconfirmed"`; it never
claims that the harness is ready.  Failures after the window remain visible
through `ps` and doctor.
