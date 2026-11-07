# Interactive-to-headless carrier switch

`lifecycle.start` accepts `replaceInteractive: true` only together with
`sessionRef`. When the same actor has an interactive binding, the daemon
signals the fenced carrier process (PID + birth identity), waits up to five
seconds for it to exit, unregisters the interactive route/binding, then starts
the headless worker with the requested native session ref. A carrier that does
not exit, or a missing `sessionRef`, is refused before the switch. On a later
headless start failure the daemon removes the headless binding rather than
leaving an unowned binding.

`lifecycle.start` returns the session ref actually established by
`_verify_started_resume`, not an echo of the request. A mismatch returns
`STRICT_RESUME_FAILED` and the daemon stops the worker.

`INTERACTIVE_CARRIER_STOP_FAILED` is the switch-only failure: no PID/birth
fence, an unknown process state, or the five-second exit timeout. It is not
`STRICT_RESUME_FAILED`, which means a worker was started and then stopped.

Event: `agent.binding.replaced` (`actor`, `previousRuntime`,
`previousSessionId`, `harness`).
