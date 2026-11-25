# E2E-003: adapter cell binding survives daemon restart

Status: executable via `run-pin-restart-survival.py`.

The runner drives binding state only through real CLI commands. The binding
is the agent's cell document (an adapter member whose
`cell.members.<ref>.adapter` names the local adapter configuration);
`adapter list` serves it through the cell routing index.

## Preconditions

- An installed `hyprial` executable.
- A disposable home/state root owned by the runner.
- Two Lark adapters registered via `hyprial adapter add` before daemon start
  (adapter configuration is a boot-time snapshot; binding lookup is not).

## Steps

1. Register two adapters, start the daemon with the prepared home.
2. Attempt `hyprial agent cell apply` before the agent exists.
3. Create the agent, bind the adapter with a cell apply, and repeat the apply.
4. Bind the same adapter to a second agent's cell, then remove it again.
5. Stop and restart the daemon against the same state; query the `agent` column
   from `hyprial adapter list`.
6. Destroy the agent; query the list again.
7. Create an agent with the destroyed agent's name (a new agent with a new
   id), bind, unbind with another cell apply, restart, and query the list again.

## Assertions

- Applying a cell to a nonexistent agent fails with `AGENT_NOT_FOUND`.
- The apply advances the cell revision and the binding is visible in
  `hyprial adapter list` immediately, as the canonical `agent:<owner>:<node>:a.<id>` URI.
- The same adapter declared by two agent cells fails closed
  (`agent: null`), and routing recovers when the conflict is removed.
- The binding survives a daemon restart (cell documents outlive the process).
- `hyprial agent destroy` removes the binding with the agent's cell and
  reports it as `unboundAdapters`.
- Reusing the destroyed name creates a new agent: it has a different stable ID.
- Removing the adapter member detaches the adapter and survives another restart.
