# Decision notes (slices GC5a/GC5b; group chat design draft §6, owner
# decisions §10, 2026-11-18; owner 2026-11-19):
# - The group commands sit under the one message noun (ruling 2026-10-06,
#   see message/threads): `message group create|show|list` and
#   `message group members add|remove`, thin wrappers over
#   `conversations.create`, `conversations.get`, `conversations.list`
#   (`kinds=["group"]`) and `conversations.members.add|remove`.  There is no
#   `members list` (`show` answers the members) and no rename, as in §10.
#   The package is a subdirectory because `message/` is at its file limit.
# - Sending, replying and reading history are not group commands: they stay
#   `message send --conversation <g.>`, `message reply` and
#   `message history --conversation <g.>` (owner, 2026-11-19).
# - Every command calls the daemon request port only and `--json` emits the
#   daemon answer unchanged; the daemon decides who may do what.
# - The caller is the verified agent session, else the local operator, by
#   the conversation reads' rule (`message.threads.reader.reader_params`).
#   `--from` / `--on-behalf-of` are left out this round.
# - A mutation mints a fresh idempotency key per invocation: the key makes a
#   transport retry of one call safe; running the command again is a new call.
# - `members remove` takes a member id or a member's name: one
#   `conversations.get` maps a name to its member id; anything that matches
#   no member is sent as typed and the daemon answers.
"""``hyprial message group``: group conversations and their members."""

from hyprial.shell.impl.cli.commands.common.groups import group

group_app = group(help="Create group conversations and manage their members.")
members_app = group(help="Add and remove members of a group.")

__all__ = ["group_app", "members_app"]
