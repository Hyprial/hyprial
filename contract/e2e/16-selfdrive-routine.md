# E2E-016: source-mode routine through PAC

## Preconditions

Use `run-selfdrive-routine.py` with Taskwarrior installed. The runner owns an
isolated TASKDATA, TASKRC, home, daemon and authenticated passive coordinator.
The coordinator is explicitly created, and its official `a.<id>` URI is read
from the public agent list. Its passive carrier registers the current channel
protocol, renews its lease while polling, and re-registers the same binding
after the planned daemon restart.

## Steps

Run `uv run python contract/e2e/run-selfdrive-routine.py`.
The runner creates its own task source and daemon, dispatches and completes one
request, restarts, verifies deduplication, then cleans its daemon.

## Assertions

Tagged task selection and untagged positive control; source-mode
routine registration; route:self dispatch to the explicit borrowed actor; an
ordinary DONE reply leaves the graph running; owner/session/current-request
completion records evidence; source-slot dedup prevents replaying one admitted
slot;
routine registration and outcome ledger survive daemon restart; exact daemon
cleanup precedes PASS. Each PAC run is checked by its durable
`routine:<routine>:<task UUID>:<slot ms>` operation key, so the same admitted
slot cannot appear twice while a later slot may create another run. No model or
production task database is used.

The fixture uses a `dispatch` tag and asserts the exported tag set before
starting the daemon. This avoids Taskwarrior versions that parse `+route:self`
as description text and correctly send that unmatched task to default escalation.
Routine removal must confirm `removed: true` and preserve the borrowed actor.

The former reply-match completion, implicit report and repeated task execution
assertions are retired. `run-routine-template-pac-journal.py` separately exercises
the packaged PAC-journal source against a real managed workflow assignment, with
full URI ownership and an explicit completion flag.
