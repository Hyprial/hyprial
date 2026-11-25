# Agent addresses at the send boundary (UUID addressing §2.3)

Source of decisions: `docs/design/uuid-addressing-implementation-2026-11-03.md`
§2.3 and ruling U1 in `docs/design/uuid-addressing-migration-path.md`;
ruling X's frozen names were withdrawn on 2026-11-19 (a used name is free,
and an agent created with it is a new agent).
Everything below applies only once id-form minting is on; before the flip,
sends behave as they always did.

## Which addresses are checked

A target is checked when it is a four-segment `agent:<owner>:<node>:<segment>`
URI whose node is this daemon's node and whose owner is this daemon's owner
or the owner of a live agent hosted on this node. Other targets (other
nodes, `user:`, `adapter:`) are not affected. Once the last agent
hosted for an owner is destroyed, nothing on this node records that owner:
its addresses, the destroyed agent's `a.` id included, are foreign again and
are forwarded, where they fail to resolve elsewhere.

Entry points: `message.send` (CLI, MCP `harness_send`, adapters, daemon
relays), `message.reply`, routine `actor` and escalation targets, and PAC
`escalate_to` and node `owner`. Routine and PAC actor/owner fields only get
the retired-address check; their existing `SENDER_UNRESOLVED` handling of
unknown principals is unchanged.

## Outcomes

| Segment | Result |
|---|---|
| The `a.` id of a live agent | Delivered. |
| The `actor` or display name of a live agent | Refused: `AGENT_ADDRESS_RETIRED`, data `{target, address, agentId}`; `address` is the agent's id URI. When the name belonged to a destroyed agent and a new agent now holds it, this is the new agent's id URI: the old address is refused, never delivered to it. |
| An `a.` id with no live agent | Refused: `TARGET_UNRESOLVED`, data `{target}`. |
| A name nothing on this node claims | Refused: `TARGET_UNRESOLVED`, data `{target}` (open HQ question Q2; before the flip this queued with a `TARGET_UNRESOLVED` delivery code). |
| A daemon service actor (`dispatch-coordinator`, `_turn-hooks`, `pac-expansion`) with no registry record | Delivered by name. `squire` is not a service actor: it is an ordinary agent and gets the agent rows above (Allen 2026-11-06). |
| A name claimed by an unregistered streaming harness, interactive session, managed worker or online actor | Delivered by name. |

A name no live agent holds (a destroyed agent's name, or a display name an
agent renamed away from) is an ordinary name: unclaimed, so
`TARGET_UNRESOLVED`, unless a claimant carries it.

A registered agent's names count only under its own owner: an
`agent:<this owner>:<node>:<name>` URI whose name is the actor of an agent
hosted for another owner is not that agent's address, and is refused with
`TARGET_UNRESOLVED` unless something else claims it.

A bare name with no `:` is still resolved to the live agent's official (id)
address before these checks; this is what the CLI and group membership
accept. A name-form URI is never accepted as a send target, including as the
recipient of `message.reply` to a pre-flip sender. A refused reply leaves the
original message pending.

A Lark inbound submission refused with `TARGET_UNRESOLVED` or
`AGENT_ADDRESS_RETIRED` is a definitive pre-admission refusal: it is
dead-lettered with that code, its pending submission is released and the
user is told it was not delivered.

## Senders (integrator decision D8)

The same names are refused as a sender. A session-less `message.send` (and
any other session-less message request) whose `from` is an
`agent:<this owner>:<this node>:<segment>` URI, and an operator's
`onBehalfOf` agent URI (with the target's owner rule above), is checked
like a target:

| Sender segment | Result |
|---|---|
| The `a.` id of a live agent | Accepted. |
| The `actor` or display name of a live agent | Refused: `AGENT_ADDRESS_RETIRED`, data `{target, address, agentId}`; `target` is the given sender, `address` its id URI, and the message says to use the id address. |
| Anything else nothing registers | `SENDER_UNRESOLVED`, as before (`onBehalfOf` keeps an unregistered canonical URI verbatim, as before). |

The name is never rewritten to the id address. A bare sender name (no `:`)
still resolves to the agent's id address. `message.send.lookup` declares
`AGENT_ADDRESS_RETIRED` for this refusal.

## Delivery side

A message reaching this node's receive for a self-node `agent:` recipient
(an owner checked as above) that nothing claims is not committed. Claimed
here means: a live agent's stored (id) URI under the agent's own owner, a
daemon service actor, or a name that an unregistered streaming harness,
interactive session, managed worker or online actor carries. No registered
agent claims a name-form URI, its own actor name included: a network arrival
or a message already in flight to a destroyed agent's name fails terminally
instead of reaching a new agent that took the name. An unclaimed message:

- a copy this node holds (the local sender's outbox row, or a custody row)
  moves to the DLQ with reason `TARGET_UNRESOLVED` and a terminal `expired`
  record;
- a network arrival gets a terminal `refused` record with reason
  `TARGET_UNRESOLVED`, no inbox row and no receipt, and the sender's node is
  sent a system notice.

Such a recipient is never handed to a custody mailbox. A message already
received under the same id keeps its duplicate acknowledgement. The local
submit and the outbox and custody pumps report the dead-lettered copy as
not accepted with code `TARGET_UNRESOLVED`, not as queued.

## Sessions

A first `session.register` for a local name with no agent record creates the
record before the registration is serialized, so it is keyed, fenced and
stored at the agent's official (`a.` id) address like every later
registration of that agent. Concurrent first registrations for one name end
with one session at the id address and none at the name URI.

A session ref retired by a destroy is refused by its incarnation, never
through a name: a call presenting it is refused with `SESSION_SUPERSEDED`
unless the presenting live agent's own session history holds the ref. So a
destroyed agent's carrier is refused by name (no agent record is created),
at the dead id, and after a same-name agent was created; a harness session
that moved to another name while the old agent was alive keeps working
after the old agent is destroyed. A destroyed agent's session cannot be
resumed onto an agent that never registered that ref.

`session.register`, `session.refresh`, `session.heartbeat` and every
session-fenced call whose actor is a local `a.` id with no live agent (a
destroyed incarnation, or an id nothing ever had) are refused with
`SESSION_SUPERSEDED`, data `{target}`. This is the code a destroyed agent's
retired session already gets, so a carrier goes quiet and never
re-registers; no session is ever kept at a dead id. `TARGET_UNRESOLVED`
stays the send-boundary code for the same id: carriers treat it as
transient and would keep retrying.

`session.register` and `session.register_guarded` whose actor is the
owner's user agent (the name `hyprial-user`, its name-form address on any
machine, this machine's `a.` id address, or its bare `a.` id) are refused with
`USER_AGENT_IMMUTABLE` before any session or agent row is written. Until
the ensure has created the user agent's record, only the name and its
name-form address get that code: the id-form address and the bare `a.` id
name no live agent yet and get `SESSION_SUPERSEDED` from the stale-address
rule above (data `{target}`); either way nothing is written. A
process claim that does not match the caller is still refused with
`CALLER_NOT_AUTHORIZED` first.

## Read side (integrator decision D9)

A live agent's consumer reads take its id URI only: message fetch and
pending lists, the headless pull, the dormant wake, `message.history` and the
reply-reminder lane. The read-only actor-name alias of decision D4 was
removed on 2026-11-19, so rows queued at a name-form URI before the id-form
flip are not read by any agent; no row is rewritten. `workflow.list` with an
agent selector still lists runs whose YAML names the live agent by its
name-form URI.

## Error code

| Code | When |
|---|---|
| `AGENT_ADDRESS_RETIRED` | A send-boundary target, a session-less sender URI or an operator's `onBehalfOf` URI names a live agent by name. |
| `SESSION_SUPERSEDED` | A session call names a local `a.` id with no live agent (see Sessions). |
