# Claude headless session continuity

After a Claude managed worker reports native SDK readiness, its native session
ref is included in the successful harness lifecycle commit. Restart consumes
that desired-state `sessionRef`; it does not select a conversation from worker
channel registration history. `agents.lastSessionId` records the native ref
after successful CLI lifecycle startup through the existing Agent authority.

A fresh launch supplies one pinned native ID. Subsequent launches use that
established ID with the SDK's `resume` option. A missing, unreadable or rejected
resume target fails worker startup with the target ID in the error. There is
no fresh-session replacement, including for legacy Claude workers. A ready
message reporting a different or absent ID is rejected when an ID was pinned.

`worker.ready` keeps its existing event name and PID and adds these fields:

| Field | Meaning |
| --- | --- |
| `last_session_id` | Agent registry snapshot read for this launch, including explicit JSON null. |
| `spec.session_ref` | Ref on the actual launch spec, including explicit JSON null. |
| `session_decision` | `resume` when the effective SDK client was launched with resume; otherwise `fresh-mint`. |
| `sessionId` | Native ID accepted from the ready handshake. |

The snapshot fields describe launch inputs, not a later registry read. A
reconnected client may choose resume while the original fresh spec still has
null `session_ref`. The worker channel session ref is a separate identity.

Regression commands use disposable homes only:

```sh
uv run pytest tests/daemon/sessions/restart tests/daemon/sessions/test_session_ref_resume.py
```

The controlled restart regression freezes the interval before periodic ref
sync. It must resume the original ID. Its model protocol double is not a
vendor history-restoration acceptance test; real provider acceptance remains
E2E-006 with the same native transcript before and after daemon restart.
