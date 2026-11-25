# E2E-004: Feishu user/adapter roundtrip

Status: executable via `run-feishu-roundtrip.py`; requires the dedicated real
environment listed below.

The runner drives a real user identity through `lark-cli` while the Python
adapter owns the approved test App connection. Adapter and gateway state live
under one disposable root. The runner prints one `PASS`, `FAIL`, or `BLOCKED`
line per assertion and never prints an App secret or access token.

## Preconditions

- `[REAL ENV]` `/opt/homebrew/bin/lark-cli` has a verified, unexpired user
  identity with message send/read/reaction scopes.
- `[REAL ENV]` The exact App is `allenwoods-hyprial-e2e`
  (`cli_aaf9ea19d4b8dbe4`); only its read-only local credential files are used.
  The three production Apps (`hq-adjutant-feishu`, `hyprial-kanban`,
  `mac-studio-feishu`) are a red line and must never be used for testing.
- `[REAL ENV]` The contract target is the App's shared test GROUP chat selected
  by the gateway's `py-test` route. Its native chat ID remains in local
  configuration and must not be copied into source, docs, logs, or evidence:
  any developer can join the group, so the scenario is reproducible by the
  whole team. A p2p DM with the bot is a per-developer private conversation
  that others cannot access; **no DM chat id may ever be committed to the
  repo** (docs, skills, runners, evidence). To run against your own DM, pin
  it via `HYPRIAL_E2E_LARK_CHAT_ID` locally.
- `[REAL ENV]` The target is resolved, never hard-coded: the runner reads
  the App's gateway configuration (exactly one direct route required) and
  verifies at preflight that the resolved chat is accessible; assertions
  compare `chat_type` against the resolved chat's actual mode (`group` or
  `p2p`). In group mode the runner @mentions the bot on every user message
  (the platform only delivers group messages that mention the bot). Both
  the authorized user and the test App must be able to access the chat.
- `[REAL ENV]` `lark-cli` only impersonates the user (`--as user` sends/reads);
  which App its auth profile is bound to is irrelevant to the App under test
  and is not a blocker.
- `[REAL ENV]` No competing connection owns the same App during inbound-event
  assertions.
- `[REAL ENV]` The caller has an open p2p DM with the approved App's bot. The
  non-text assertions require it and it is resolved live from the App name at
  preflight, never committed: the shared group only delivers messages that
  @mention the bot, and `image`, `file` and `merge_forward` messages cannot
  carry a mention, so those types provably never arrive there (measured: sent
  to the group, no `im.message.receive_v1` event is produced). Opening the DM
  once from Feishu is enough; a missing DM blocks at preflight with that
  instruction.
- The runner can allocate a disposable `HYPRIAL_HOME`, `HARNESS_STATE_DIR`,
  correlation store, command directory, and evidence log.

## Steps

1. `[REAL ENV]` Start the isolated Python adapter, wait for its SDK ready
   marker, then use `lark-cli --as user` to send
   `CONTRACT_LARK_REQUEST_<nonce>` to the resolved test chat (in group mode
   the runner prefixes the bot's @mention automatically).
2. `[REAL ENV]` Wait for Harness custody and poll the user's reaction view until
   `OnIt` appears; compare timestamps to prove it appeared after custody.
3. `[REAL ENV]` Have the target actor return `CONTRACT_LARK_REPLY_<nonce>`
   through `LarkAdapter.handle_delivery`.
4. `[REAL ENV]` Fetch the bot response as the user and verify it is a native
   reply containing `Agent`, `Replied-At`, and `Reply-To` headers.
5. `[REAL ENV]` Poll until `OnIt` is absent, after provider success and Harness
   delivery acceptance.
6. `[REAL ENV]` Reply natively as the user to the bot response and verify the
   persisted native reply index routes the follow-up to the same actor.
7. Replay the original normalized event with the original event ID; verify
   `duplicate`, no second Harness request, and no request plaintext in the
   structured evidence log.
8. `[REAL ENV]` Restart the isolated gateway without replacing its state, send
   another native follow-up, and verify it still routes to the same actor.

9. `[REAL ENV]` Send a rich-text `post` (title, a paragraph with a link, a
   second paragraph) to the group and to the bot DM, and drive each all the way
   to an agent reply.
10. `[REAL ENV]` Send a `file` and an `image` message to the bot DM and drive
    each to an agent reply.
11. After every non-text message, read the adapter's private dead-letter store
    and verify the message is absent from it.

The runner also executes deterministic cases after the live flow: a forced
reply failure must reject the delivery without Harness acceptance, retain
`OnIt`, and never call reaction cleanup; a `merge_forward` message must
normalize and forward; and an unsupported type, unreadable content, and an
unparseable event must each fail loudly into a recoverable dead-letter record.

### Non-text coverage rationale

A text-only contract cannot observe the 2026-08-13 P0, in which the adapter
discarded every non-text message while text kept working, and the discarded
bodies were only visible in the adapter's private dead-letter store rather than
the main inbox DLQ. The non-text assertions therefore check three things that
"it did not raise" cannot: the event keeps its real type, the agent receives an
intelligible body, and nothing new appears in that private store.

`merge_forward` cannot be produced live by the test identity and is covered
deterministically at the `normalize_sdk_event` seam. The platform rejects
`merge_forward` for a user token (`99991668 user access token not support`),
`lark-cli` restricts the command to `bot`, and the CLI's own bot is not a member
of the contract chats (`99992402`). No other message type in the closed set is
reachable as "unknown" either, so the unknown-type case is deterministic too.

## Assertions

- `[REAL ENV]` The exact App and user identity pass preflight and the SDK emits
  a ready marker before the user send.
- `[REAL ENV]` The request event is `text` and its `chat_type` matches the
  resolved chat's mode, preserves its native message
  and event IDs, and is accepted by Harness for the expected actor.
- `[REAL ENV]` `OnIt.action_time >= harness.accepted_at`.
- `[REAL ENV]` The actor response is sent through the adapter, accepted once,
  and returned as a native reply with all three routing headers.
- `[REAL ENV]` `OnIt` disappears only after the successful reply/accept path.
- `[REAL ENV]` A user native follow-up routes to the actor stored for the bot
  reply.
- Event replay returns `duplicate`, does not create another Harness request,
  and the evidence log contains digests/lengths rather than message plaintext.
- `[REAL ENV]` The same native reply route works after a gateway-process restart
  using the unchanged isolated state file.
- A forced provider failure retains `OnIt`, rejects the delivery, produces no
  Harness delivery acceptance, and performs no reaction cleanup.
- `[REAL ENV]` A rich-text `post` is normalized as `post` and its flattened
  body matches the contract exactly -- title line, paragraph lines, and each
  link contributing its visible label and never its href -- reaches the agent
  with that same body, is answered by a native reply, and is not dead-lettered.
- `[REAL ENV]` A `file` message reaches the agent as
  `[Lark file: <name> ref:<message_id>/<file_key>]` and an `image` message as
  `[Lark image ref:<message_id>/<image_key>]`: never empty, never an
  `[Unreadable ...]` or `[Unsupported ...]` placeholder. The `ref` handle is
  the input to `hyprial adapter media get` (2026-08-14 ruling: media retrieval is
  a directed pull through the adapter, not a coordinator API replay). The
  Harness request names the exact command with its configured adapter name.
  Each is answered by a native reply and neither is dead-lettered.
- `[REAL ENV]` The exact checkout's public `hyprial message send --file` command, running
  against a disposable `HYPRIAL_HOME`/`HARNESS_STATE_DIR`, uploads a file through
  the approved test App and sends a native `file` message to the user's bot DM.
  Its route result contains the text message ID plus one file-level native
  message ID, file name, MIME type, byte size, upload type, and file key. The
  user identity reads that attachment back and downloads byte-for-byte content
  equal to the source. The test home only symlinks the approved read-only test
  credential; it never copies a secret or touches the production daemon.
- A `merge_forward` message normalizes as `merge_forward`, forwards exactly one
  Harness request carrying the merged-forward stand-in text, and is not
  dead-lettered. A child image/file ref is shell-aware:
  `ref:<top-level-shell-message-id>/<child-message-id>/<resource-key>`. Nested
  leaves retain the same top-level shell ID, and the request carries
  `hyprial adapter media get <adapter> <ref>` with `<adapter>` filled in.
- An unsupported message type, unreadable content, and an event that fails
  normalization each produce their own explicit status
  (`unsupported-message-type`, `invalid-message-content`, `normalize-error`),
  send no Harness request, leave a recoverable dead-letter record, and receive
  one native failure reply in the original conversation. None is silently
  discarded. Because Lark rejects an unknown `msg_type` before it enters a
  chat, the unsupported shape is injected at the adapter boundary using a real
  user message ID from the dedicated test App DM; the assertion then reads the
  App's real native reply and verifies its `reply_to` points to that message.
- The lark-cli identity check retries with backoff before blocking, and a
  `BLOCKED` result names the criteria that did not hold, is written to stderr
  with the count of assertions actually verified, and exits non-zero (2) so it
  cannot be mistaken for a pass.
- Evidence lines contain timestamps, nonce, Harness IDs, native message IDs,
  stable digest labels for the resolved chat/open-user identity, and routing
  outcomes, but no raw chat/open-user ID, token, App secret, dead-letter body,
  or request plaintext.

Run with the project environment that contains `lark-oapi`:

```sh
# Target resolves from the App's gateway config (the shared test group):
uv run python contract/e2e/run-feishu-roundtrip.py
# Or pin a different chat explicitly (e.g. your own DM with the bot;
# DM ids are personal and must never be committed):
HYPRIAL_E2E_LARK_CHAT_ID=REPLACE_WITH_PRIVATE_CHAT_ID \
  uv run python contract/e2e/run-feishu-roundtrip.py
```

## send-links 硬切：组织空间前置（WP8 返工 2-E，2026-10-15）

agent→agent 段在硬切后需要同一组织空间；真实 Lark 用户/route 目标不受 link 约束。若场景含 agent→agent 段，前置需建 `group-` 空间并在发送后等 hold 放行。本条为前置说明；未在本轮实测。
