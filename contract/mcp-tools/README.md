# MCP tool black-box contract

`run.py` exercises all seven public Harness tools through the official MCP
Python SDK and the same fixed-identity server used by Hyprial's stdio
worker/Channel entry points. The client transport stays in-process while the
server still crosses the public stateless Unix-daemon proxy seam, so the
contract does not claim a production daemon or socket.

`harness_send` takes `to` as one target or a non-empty list plus a required
`summary`; `harness_reply` requires `summary`; `harness_read` calls
`message.pending.batch` and returns one `inbox-batch-v1` snapshot with every
pending item's full body inline or one private spill-file reference. The
executable fixture pins the fixed top-level keys, complete item shape and
canonical `delivery` shape from `contract/wire/inbox-batch-v1.md`.
With `conversationId` (and optionally `mode`, `cursor`, `limit`),
`harness_read` instead calls `conversations.history` with that page and
settles nothing; this is how an agent reads a group's history, which the
daemon answers only to current members. Paging arguments without
`conversationId` are a tool error.
`harness_read_body` still returns one body and leaves the row pending for
single-ID recovery. A send is never a reply. The inbox batch contract is
`contract/wire/inbox-batch-v1.md`; the tool list is also pinned by K12.

`WORKER_HARNESS_TOOLS` and `_CLAUDE_HARNESS_TOOLS` include `harness_read_body`;
the Pi extension carries the same operations over Unix IPC. Its result/error
and interactive settlement requirements are in [pi-receipts.md](pi-receipts.md).
