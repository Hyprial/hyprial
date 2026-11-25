# E2E-031: group conversations on a single isolated node

Runner: `run-group-conversation.py` (isolated; CI gates: `run-isolated-gate.py`
runs GC-00..GC-10 and GC-12; the `hosted` job of
`.forgejo/workflows/mock-model-harness-e2e.yml` runs the whole scenario with
`--harness pi`, GC-11 included).
Scenario package: `_group_conversation/` (`fixture.py`, `steps.py`,
`harness.py`, `phases.py`); it reuses its own session fixture (`cli.py`,
moved there from the retired `_send_links/`) and, for the opt-in stage, the mock-model loopback
gate's agent setup (`run-real-harness-loopback.py`, `_fake_provider.py`).
Feature: `hyprial message group create|list|show|members add|remove`,
`hyprial message send --conversation g.<id> [--mention NAME] [--reply-to ID]`
without `--to`, `hyprial message reply [--mention NAME] [--no-mention]`,
`hyprial message history --conversation g.<id>`, and the group delivery rule.
A group message wakes only the agent members it @-mentions. A group reply
defaults to @-mentioning the current agent author of the replied-to message,
except when that author is human, the replier itself, or no longer a member;
`--no-mention` suppresses that default. A message with no @-mentions and a
`--no-mention` reply wake no members.

## Preconditions

- `uv sync --extra test` and the editable native engine; no credentials, no
  provider account, no Feishu App, no IM adapter.
- `real_environment_required: false`: one disposable daemon under a short temp
  root, with its own `HYPRIAL_HOME`, `HARNESS_STATE_DIR`, socket and loopback
  ports, `HYPRIAL_NETWORK_ISOLATED=1`. The runner refuses a home equal to
  `$HOME/.hyprial` and never touches the production daemon.
- Agents A, B and C are plain agents. Each gets one verified channel session
  (`session.register` with a fresh lease, kept alive by the session heartbeat
  thread). An agent's turn is what its harness would run from its shell: the
  public CLI under that agent's session (`HYPRIAL_WORKER_ACTOR` /
  `HYPRIAL_WORKER_SESSION_REF`). An agent send without that pair is refused
  `SENDER_NOT_AUTHENTICATED`, so the pair is the agent's identity, not a
  bypass. The owner writes as `user:<owner>` from the operator CLI.
- Opt-in stage GC-11 (`--harness pi`) additionally needs `pi` on `PATH` and
  starts real headless Pi workers against the local mock model
  (`FakeProvider(reply_to_harness_requests=True)`); `pi` missing is BLOCKED
  (exit 2), so the hosted job fails rather than passing without GC-11.
- Budgets: owner/agent sends deliver inside the send call, so every row check
  is immediate after a positive barrier; the restart pass is awaited for at
  most 60 s (`conversation.group_catch_up_done` in the scenario's own daemon
  log); GC-11 waits at most 120 s for both model turns.

## Steps

- **GC-00 fixture**: start the daemon, create agents A, B, C, confirm
  `bypass:true`.
- **GC-01 create**: `message group create --title e2e-031-<nonce> --member A
  --member B --json` answers `ok:true`; `message group show` lists the owner
  (`kind:user`, `self`) and A, B (`kind:agent`); `message group list` lists the
  group. An `IPC_TIMEOUT` answer is a FAIL even when the group lands later; the
  runner then continues with the group it finds by title.
- **GC-00b sessions**: register one channel session per agent and start the
  heartbeat thread (after GC-01, so the first OrgFS write is not inside the
  six-second liveness window).
- **GC-02 owner send**: `message send --from user:<owner> --conversation <g>
  --summary m0 ...` (no mentions) answers one delivery to the group id,
  `queued:false`, lands in history, and wakes nobody (no durable row for A, B
  or the owner). Then `message send --from user:<owner> --conversation <g>
  --mention A --mention B --summary m1 ...` gives A and B each one durable
  inbox row for M1, whose id is `uuid5("hyprial:group-row:<g>:<M1>:<member>")`,
  whose payload names M1, and which `message.pending.list` shows once, from
  `user:<owner>`. The owner holds no row.
- **GC-03 turns**: A and B each `message reply <row> --from <self>` under their
  own session; each M1 row is then durably settled (`consumed:1`). `message history` shows each reply once with `intent:reply`,
  `from` the member id and `replyTo.messageId == M1`; no inbox row names either
  reply; neither agent keeps a group row pending.
- **GC-04 mention**: `message group members add <g> C`; A (its session) sends
  `--conversation <g> --mention B`. History shows the entry from A with
  `mentions == [B]`; B holds exactly one row for it (payload `mentions`),
  visible in its pending list (admitted, not held); A (author) and the owner
  hold none.
- **GC-05 reply default mention vs `--no-mention`**: B replies to its M2
  row (`message reply` without `--mention` or `--no-mention`); because M2 was
  authored by agent A, B's reply records `mentions == [A]` and wakes A (one row
  for A, none for B, C or the owner). A then answers its row using `message
  reply --no-mention`; that reply omits the default `@` (no `mentions`) and
  wakes nobody.
- **GC-06 unmentioned member**: C (never mentioned) has an empty pending list
  and no durable row in the group.
- **GC-07 remove and reply `--mention`**: `message group members remove <g> B`;
  `show` no longer lists B. Owner sends M3 with `--mention A --mention C`: A
  and C hold exactly one row each; B holds none and its `message history
  --conversation <g>` is refused `CONVERSATION_NOT_MEMBER`. A acknowledges M3;
  C answers M3 using `message reply --mention A` (human owner M3 has no default
  `@`, so `--mention A` alone lands in `mentions`), waking A (and settling C's
  M3 row), and A acknowledges C's reply.
- **GC-08 restart between send and turn**: owner sends M4 with `--mention A
  --mention C`; A and C hold one row each and nobody fetches them. Stop the
  heartbeat, `daemon stop`, `init`, re-register each session with a new lease,
  wait for the restart pass to log `conversation.group_catch_up_done` again.
  The durable rows for M4 are the same single row per member (same ids), each
  listed once. A then takes its turn: history holds exactly one reply to M4. C
  acknowledges. The whole set of durable row ids of the group is the same
  before the stop and after the restart pass, so no acknowledged entry is
  delivered again either.
- **GC-09 1:1 stays separate**: the owner sends A a direct message (`--to A`);
  A's row carries a non-group conversation id. `message conversation list`
  (owner, and A under its session) lists no `g.` conversation and the owner's
  list does include the direct one; the direct conversation's
  `message history` contains no group entry.
- **GC-10 history**: `message history --conversation <g> --limit 200` lists
  every entry once in `(createdAtMs, messageId)` order; the interaction entries
  are exactly M0, M1, A's reply, B's reply, M2, B's default-mention reply to M2,
  A's `--no-mention` reply to B, M3, C's `--mention A` reply to M3, M4, A's
  reply to M4 in that order; the system entries are created, A added, B added,
  C added, B removed.
- **GC-11 harness turn (opt-in, `--harness pi`)**: two agents P1, P2 configured
  as the loopback gate configures its Pi agent, started `--headless`; a group of
  the owner, P1 and P2; one owner message with `--mention P1 --mention P2`.
  Within 120 s history holds a reply to it from each of P1 and P2 (the mock
  model calls `harness_read` then `harness_reply`): exactly one entry per
  worker, authored by that worker (`from` is its member id), `intent: reply`,
  `replyTo` the owner's entry, `conversationId` the group, and the mock model's
  text `E2E031_REPLY`, which only the harness's own `harness_reply` writes.
  Each worker's row is exactly one and settled; no row names either reply; the
  mock model saw `/v1/chat/completions`. The two replies arrive within
  milliseconds of each other, so this also drives two concurrent appends to the
  group document (the first local runs failed the second one as
  `DAEMON_UNAVAILABLE` until `conversations/groups/log.py` serialised appends,
  2026-11-20).
- **GC-12 cleanup**: GC-12a first proves the check can see the running daemon
  (its pid from `state/daemon.json` is live). Teardown stops the heartbeat and
  the owned daemon; no recorded daemon pid (every generation, including the
  one stopped in GC-08) and no process whose command line names the
  scenario's temp root survives 15 s.

## Assertions

| Id | Assertion | Checked on a real daemon by the runner | How it is driven |
|---|---|---|---|
| GC-01 | Creating a group with two agents answers the group with the owner and both agents as members, and lists it. | yes | operator CLI |
| GC-02 | An unmentioned owner message wakes nobody; an owner message mentioning A and B reaches only those mentioned agents as one row each, never the owner. | yes | operator CLI; durable inbox rows read-only |
| GC-03 | An agent answers its row with an ordinary reply; the reply lands in the group as a reply to the owner's entry and wakes nobody. | yes | agent-session CLI (`message reply`) |
| GC-04 | An agent message that mentions another agent wakes only that agent, and its row is admitted (link bypass on). | yes | agent-session CLI (`message send --mention`) |
| GC-05 | A reply to an agent's entry mentions and wakes that author by default; `--no-mention` omits the default mention and wakes nobody. | yes | agent-session CLI (`message reply`, `message reply --no-mention`) |
| GC-06 | A member that is never mentioned gets no row. | yes | pending list and durable rows |
| GC-07 | A removed member gets no row for later messages and can no longer read the group; `message reply --mention A` wakes A. | yes | operator CLI; agent-session CLI (`message reply --mention`) |
| GC-08 | A daemon restart between a send and the turn creates no second row, and the turn after it writes one reply. | yes (graceful stop) | `daemon stop` / `init`; restart-pass log line |
| GC-09 | The group never appears in 1:1 conversation lists, and group rows never enter a 1:1 history. | yes | operator and agent-session CLI |
| GC-10 | Group history lists every entry once, in order. | yes | operator CLI |
| GC-11 | A woken real harness takes a model turn and its `harness_reply` lands in the group as the reply. | yes, with `--harness pi` (the mock-model-harness-e2e `hosted` job); the isolated gate does not pass the flag | real headless Pi + mock model |
| GC-12 | Teardown leaves no process behind. | yes | `ps` after teardown |

Not checked here, and why:

- **An agent composing a mention through its model**: the mock model only
  calls `harness_read` and `harness_reply`, and `harness_send` takes no
  conversation or mentions, so a model-authored mention cannot be scripted
  without inventing a tool. GC-04 drives the same daemon path through the
  agent's own CLI session. UNKNOWN for a real model.
- **Restart by crash (SIGKILL) between send and turn**: GC-08 stops the daemon
  gracefully. Rows are created inside the send, before the answer; the
  restart pass is the same either way, but a crash between the document write
  and the row creation is pinned only by unit tests
  (`tests/daemon/conversations/group_delivery/test_group_exactly_once.py`).
- **Multi-machine groups, GUI, real IM adapters**: out of scope. Group delivery
  is single-machine by design; nothing here forwards to the network.

Every runner line is `RESULT <STATUS> E2E-031 <id> driven=<how> [detail=...]`
with STATUS one of PASS, FAIL, BLOCKED, UNKNOWN, NOT_RUN; a FAIL detail names
the command, the expectation and the observation. A failed step marks only the
steps that need its state NOT_RUN. The final line is `RESULT PASS|FAIL|BLOCKED|
INCOMPLETE E2E-031 group-conversation pass=.. fail=.. blocked=.. unknown=..
not_run=..`; only PASS exits 0.
