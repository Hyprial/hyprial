"""Group conversation request handlers (slice GC2).

Decision notes (brief GC2 item 2):

- The handlers of ``conversations.create``, ``conversations.get`` and
  ``conversations.members.add``/``.remove``, and the group heads of
  ``conversations.list``, live here, beside ``history`` (the other
  ``conversations.*`` handler): ``inbox_surface`` has no room for another
  file under the seven-file limit, and ``history/__init__.py`` is near the
  line limit, so the group side is a package of its own that the list
  reaches through ``group_heads``.
- They are plain functions of the application, not mixin methods: they use
  the conversation reads' caller rule (``_history_caller``), the agent
  registry and the conversation placement, and add no application state.
- Slice GC3 adds ``messages`` (the group branch of ``message.send`` and
  ``message.reply``) and ``history`` (``conversations.history`` for a
  group).  ``send.py``, ``reply`` and ``history`` reach them by module.
- Slice GC4 adds ``delivery``: the rows a group message gives the member
  agents it wakes, and ``deliver_recent``, the restart pass composition
  runs at start.
"""

from __future__ import annotations

from .delivery import deliver_recent
from .heads import GroupHead, group_heads
from .requests import add_member, create_group, get_group, remove_member

__all__ = [
    "GroupHead",
    "add_member",
    "create_group",
    "deliver_recent",
    "get_group",
    "group_heads",
    "remove_member",
]
