# Agent display names and name reuse (UUID addressing §2.2)

Source of decisions: `docs/design/uuid-addressing-implementation-2026-11-03.md`
§2.1–§2.2 and ruling U2 in `docs/design/uuid-addressing-migration-path.md`.
Ruling X's frozen names were withdrawn by the owner on 2026-11-19: a used
name is not tombstoned.

## Record fields (additive)

Every serialized agent record (`Agent.to_json`, the Agent projection payload
and `agent.list` rows) gains two keys; no existing key changes meaning.

| Key | Type | Meaning |
|---|---|---|
| `agentId` | string or null | The `a.` id this incarnation derives from its entity token. Null only for a legacy record whose token cannot derive an id. |
| `displayName` | string or null | The renameable label. Null means the display is `actor`. |

`actor` stays the immutable internal key: pins, grants, blocks, home and
desired-state references keep keying on it, and a rename changes none of them.

## Names

- Names are unique across the live agents' `actor` and `displayName` union.
- A bare name input resolves a live display name to its agent, as it resolves
  an actor name.
- A destroyed agent's name, and a display name an agent renamed away from,
  is free: `agent.create`, `agent.start`, `agent.attach`
  and `agent.rename` accept it. An agent created with a previously used name
  is a new agent with a new id: it inherits nothing of the old one (grants,
  pins, home contents, session refs, pending messages, routines, default
  conversations are keyed by the incarnation). The registry keeps no record of
  used names.
- A name-form URI never reaches a destroyed agent's successor: a send to
  `agent:<owner>:<node>:<name>` is refused (see `agent-addressing.md`), and no
  agent reads rows queued at a name-form URI.
- A destroyed agent's retired session refs stay refused by incarnation, not
  by name: a carrier presenting such a ref is refused by name, at the dead id
  and after a same-name agent was created, and never creates or binds the new
  agent. A destroyed agent's session cannot be resumed onto an agent that
  never registered that ref.

## Output surface (lane C)

| Face | Additive keys |
|---|---|
| `agent.create` / `agent.get` / `agent.list` / `agent.rename` agent rows, `ps` `agents[]` | `agentId`, `displayName` (from the record) |
| `targets` rows of local registered agents | `agentId` (users, routes and unregistered actors gain nothing) |
| `org.network` `self` | `deviceUid` (null before device identity is provisioned) |
| `org.network` directory rows | `deviceUid` when the row carries one (see `contract/org-network/README.md`) |

`--json` output keeps every existing key. The human views of `hyprial agent
list` and `hyprial agent rename` head each agent row with `display(a.<id>)`:
the display name (else the actor) followed by the agent id in parentheses;
a legacy record without an id shows the display name alone.

Not applied (explicit N/A): `top.snapshot` rows and the human view of
`hyprial agent top`. Those rows are actor-status rows (connectors,
interactive sessions, presence actors and registered agents) built by
`daemon/impl/operations/top.py`; they carry `actor` (the agent's address,
which is the `a.<id>` URI once id-form minting is on) and `name`, and no
`agentId` or `displayName`. The ACTOR column keeps printing `name`. Adding the
`display(a.<id>)` label would add `top.snapshot` keys and, because `agentId`
is not gated on the switch, change the human `agent top` table with id-form
minting switched off. It is left for a follow-up that decides that output.
The forwarding membership roster is N/A as well; see
`contract/org-network/README.md`.

## Rename

`hyprial agent rename <name|a.id|uri> <new-display-name>` calls the daemon
request method `agent.rename` with `{"name": <target>, "displayName": <new>}`
and answers `{"ok": true, "agent": <agent row>}`. The target may be the actor,
the current display name, the `a.` id or an agent URI. Renaming to the actor
name clears `displayName` to null. Ruling U2: any caller with the existing
operator or bound-agent IPC trust may rename; no further permission check
applies. `agent.rename` is declared in `contract/daemon-request/openapi.json`
(mutating, never retried, not HTTP-exposed until the HTTP write admission is
integrated).

| Code | When |
|---|---|
| `INVALID_ARGUMENT` | `name` or `displayName` is missing or not a non-empty string. |
| `INVALID_AGENT_NAME` | The display name is not a bare agent name. |
| `AGENT_NOT_FOUND` | No local agent answers to the target. |
| `AGENT_EXISTS` | Another live agent holds the name as actor or display name. |
| `AGENT_ERROR` | Any other registry or domain-command refusal. The message starts with the underlying code (for example `DOMAIN_COMMAND_TIMEOUT: ...`); after a timeout the outcome is unknown, so re-read with `agent.get` before retrying. |

## Taken-name remedy

The `AGENT_EXISTS` message of `agent.create` (and of the registry's create)
says to destroy that agent first to reuse the name.
