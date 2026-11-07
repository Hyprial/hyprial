# Inbox watch

The loop every client harness runs against its daemon inbox: register, refresh
after a daemon restart, list, hold on `message.pending.wait`, decide what is
new, re-wake the backlog. The Claude Channel, the Codex carrier and pi attach
all drive one I/O-free state machine:

- Python: `src/hyprial/daemon/impl/inbox_watch.py`
- TypeScript port: `pi/resources/pi_inbox_watch.ts` (planned)

The cases in `cases/` are the contract both implementations run. The
design is in the HQ note `design-shared-inbox-watch-2026-10-05.md`.

## Model

A listed row carries the sender, intent, messageId, `summary` and `bodyBytes`;
it never carries the body.  The body is fetched by id through
`message.read_body` (consuming, does not close the row).  The row key set is
exactly `messageId, deliveryId, conversationId, from, to, intent, kind,
summary, bodyBytes, origin`; a row written before the hard cut reports
`summary: null`.  See `../wire/send-links.md` §2.

Two lanes run concurrently:

| lane | actions | rule |
|---|---|---|
| `poll` | `register`, `refresh`, `list`, `wait`, `sleep`, `quiet` | one request at a time |
| `heartbeat` | `heartbeat`, `refresh`, `sleep`, `quiet` | may refresh while the poll lane holds a wait |

A lane asks for its next action at a time `now` (milliseconds), performs it,
and reports the outcome: `ok` with the daemon response, `rejected` with a
normalized error code, or `unreachable`. Sleeping is an action with a
duration, so a case needs no clock. Refresh is single-flight across lanes.

Two premises every client must meet:
- **Not thread-safe.** A client that runs its lanes on separate threads (the Codex carrier) serializes the `next`/`done` calls itself; an anyio or JS-timer client runs them on one loop.
- **Every request carries the session's `sessionRef`.** That fence is how any request, not only a register or refresh, learns `SESSION_SUPERSEDED`.

A `register` answer may omit `daemonEpoch` (an idempotent register). The machine then adopts the first epoch any later response carries, so a later restart is still detected (G14). If that first epoch already belongs to a restarted daemon, the adoption cannot tell; the restart is then caught by the heartbeat's `STALE_DAEMON_GENERATION` (G14b). A client that heartbeats is therefore required, not optional.

A `list` outcome yields an inbox change:

- `new`: listed ids never listed before. Deliver them.
- `rewake`: listed ids seen before, when a re-wake is owed. Re-notify them now, bypassing any pacing. `rewakeReason` is `epoch`, `start` or `recovery`.
- `completed`: ids that left the inbox without this client settling them. Drop their pending wakes.

## Rules

1. Any response carrying a new `daemonEpoch` (list, wait, heartbeat, refresh, or one fed in with `observe`)
   owes a refresh and a re-wake. So does `STALE_DAEMON_GENERATION` on a
   heartbeat. The re-wake rides on the first list after the refresh
   succeeds, whichever lane saw the epoch.
2. A refused refresh is retried with backoff. Listing and delivery continue
   meanwhile; only the waits stop until the refresh succeeds. This includes a
   refresh the heartbeat lane has in flight: the poll lane sleeps the interval
   instead of holding a wait.
3. `SESSION_SUPERSEDED` from any request: both lanes go quiet for good.
4. `STALE_SESSION`: re-register (every client), then re-wake with reason `start`.
   Exception: when refresh is already owed, `STALE_SESSION` from a list or wait
   backs off that read without cancelling refresh. A restarted daemon can keep
   the persisted registration but lack a live binding until refresh succeeds.
   The refresh's own verdict decides whether re-registration is needed (G9b);
   `SESSION_SUPERSEDED` still silences both lanes immediately.
5. `wait` carries every id the last list returned, system notices included.
   A held wait (`held: true`) lists at once. A wait that is not held, refused
   or failed sleeps the poll interval and never spins. `METHOD_NOT_FOUND`
   turns the wait off until the daemon epoch changes, then probes once.
6. Failed lists back off `min(5000, interval * 2^(failures-1))`.
7. A response the client got outside both lanes (a tool call such as a read) is fed in with `observe`; a new epoch there counts like any other (R2).
8. An error code a request outside both lanes got (a turn report) is fed in with `observe_rejection`: `SESSION_SUPERSEDED` silences both lanes (R3).
9. A re-wake requested with `now: false` keeps the current pace: it rides on the next list that would happen anyway, for a client re-owing one it could not apply (R4).
10. `use_wait` is read when the next action is asked for, not only after a list: a client that turned busy (a batch in flight) and switches it off gets an interval sleep, never a hold, and no `wait.unheld` count (R5).
11. A client-local recovery pulse (session start/resume, failed turn) asks for
   a re-wake with reason `recovery` on the next list.

Clients keep for themselves:
- how a request is performed, and error-code normalization;
- the delivery action and its payload validation;
- pacing of repeated notifications.

## Case format

```json
{
  "id": "G8",
  "title": "...",
  "config": {"pollIntervalMs": 500, "heartbeatIntervalMs": 1000, "holdMs": 2000, "useWait": true},
  "steps": [
    {"at": 0, "lane": "poll", "expect": {"op": "register"}},
    {"at": 0, "lane": "poll", "respond": {"ok": {"ok": true, "daemonEpoch": "e1"}}},
    {"lane": "poll", "expect": {"op": "wait", "knownMessageIds": ["m1"]}},
    {"lane": "poll", "respond": {"rejected": "METHOD_NOT_FOUND"}},
    {"lane": "poll", "respond": {"unreachable": true}},
    {"lane": "poll", "expect": {"op": "sleep", "ms": 500}},
    {"lane": "poll", "respond": {"ok": {"messages": []}}, "emit": {"new": ["m2"]}},
    {"at": 300, "lane": "poll", "rewake": "recovery"},
    {"at": 350, "lane": "poll", "rewake": "recovery", "now": false},
    {"lane": "poll", "useWait": false},
    {"at": 400, "lane": "poll", "observe": {"daemonEpoch": "e2"}}
  ],
  "counters": {"rewake.epoch": 1}
}
```

- `at` sets the time, which never goes backwards, and defaults to the previous step's.
- `expect` asks the lane for its next action and compares it field by field.
- `respond` completes the lane's outstanding request.
- A `respond` without `emit` must produce an empty change.
- `counters` lists a subset that must match at the end.

Python runs the cases with `tests/daemon/inbox_watch/test_golden_cases.py`.
