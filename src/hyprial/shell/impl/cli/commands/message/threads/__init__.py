# Decision notes (cell model phase B, CLI batch; design §9.5, §10.0; T2a, T2b):
# - The conversation and history reads join the one message noun (Allen
#   2026-10-06: the message domain is one top-level noun; R12 folded the
#   message verbs under it) as `message conversation list` and `message history`,
#   instead of a new top-level `conversation` group.  The subgroup follows the
#   `message outbox list` / `message delivery status` shape.
# - T2a (2026-11-22) removed `hyprial message thread` (`thread.py` and
#   `thread_app`); `message conversation list` and `message history` remain.
# - T2b (2026-11-22) removed `threads.*` requests, `message conversation list
#   --thread`, and the external `THREAD` column.  Every command calls
#   `conversations.list` and `conversations.history` through the CLI's daemon
#   request port only; `--json` emits the daemon answer unchanged.
# - `message history --conversation <c.>` reads one `conversations.history`
#   page; there is no second spelling (`conversation history`) of it.
# - `message send --conversation` keeps its legacy `conversationId` meaning
#   (unchanged params); `message send` gets no `--thread`.
"""``hyprial message conversation`` group."""

from hyprial.shell.impl.cli.commands.common.groups import group

conversation_app = group(help="List your conversations, newest first.")

__all__ = ["conversation_app"]
