"""Crash-safe persistent Lark message correlation state, backed by SQLite.

One shared database (``~/.hyprial/state/adapters.sqlite3``) serves every adapter
process on the host; rows are namespaced by an ``adapter`` column.  The
storage conventions follow ``hyprial.inbox.service``: WAL journal, full
synchronous, and a busy timeout so concurrent adapter processes queue on the
write lock instead of failing.  Mutations run under ``BEGIN IMMEDIATE`` so a
read-modify-write (capacity checks, upsert-detection) is atomic across
processes, not just across threads.

There is no migration from the JSON era (product decision: the old runtime
state is void after the cutover).  A pre-SQLite state file found on disk is
renamed with a ``.retired`` suffix -- kept for audit, never read -- and the
store starts empty.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from hyprial.daemon.impl.adapters.lark.state.records import DEFAULT_ADAPTER
def _retired_target(path: Path) -> Path:
    target = path.with_name(path.name + ".retired")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.retired.{counter}")
        counter += 1
    return target


def retire_legacy_state(path: Path) -> Path | None:
    """Rename a pre-SQLite state file aside and report where it went.

    The retired file is kept, never deleted: the dead letters inside remain
    available for a manual audit.  Nothing is read back — the cutover starts
    from an empty database by decision, not by accident.
    """

    if not path.is_file():
        return None
    target = _retired_target(path)
    path.rename(target)
    return target


@dataclass(frozen=True)
class ObservedChat:
    """One chat this adapter has already received traffic from."""

    chat_id: str
    chat_type: str | None = None


def observed_chats(
    path: Path, *, adapter: str = DEFAULT_ADAPTER
) -> tuple[ObservedChat, ...]:
    """Chats with recorded inbound activity, read without creating that state.

    First-run asks "which chat did the user already talk to this bot in" while no
    route exists yet, and the answer lives in the adapter's own correlation
    tables.  Two properties matter here:

    * the connection is opened ``mode=ro`` and a missing database is an empty
      answer, so asking the question cannot create the state being asked about --
      ``onboarding plan`` is documented as read-only;
    * retired chats are excluded exactly like :meth:`LarkStateStore.recent_chats`:
      a chat the platform permanently refused is not a candidate to bind.
    """

    if not path.is_file():
        return ()
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return ()
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT chat_id FROM request_correlations WHERE adapter = ?
               UNION
               SELECT chat_id FROM dead_letters WHERE adapter = ?
               EXCEPT
               SELECT chat_id FROM retired_chats WHERE adapter = ?
               ORDER BY chat_id""",
            (adapter, adapter, adapter),
        ).fetchall()
        if not rows:
            return ()
        chat_ids = [str(row["chat_id"]) for row in rows]
        # Dead letters carry the chat type they were written with; live events are
        # the better source and win when both exist.  Either way the field is
        # presentation-only, so an unknown type stays None rather than blocking.
        types = {
            str(row["chat_id"]): str(row["chat_type"])
            for row in connection.execute(
                "SELECT chat_id, chat_type FROM dead_letters"
                " WHERE adapter = ? AND chat_type IS NOT NULL",
                (adapter,),
            )
        }
        types.update(
            {
                str(row["chat_id"]): str(row["chat_type"])
                for row in connection.execute(
                    "SELECT chat_id, chat_type FROM chat_types WHERE adapter = ?",
                    (adapter,),
                )
            }
        )
        return tuple(
            ObservedChat(chat_id=chat_id, chat_type=types.get(chat_id))
            for chat_id in chat_ids
        )
    except sqlite3.Error:
        # A database predating the correlation tables (or one being written by a
        # worker mid-rotation) is absence of evidence, not a plan failure.
        return ()
    finally:
        connection.close()


def replied_chats(path: Path, *, adapter: str = DEFAULT_ADAPTER) -> tuple[str, ...]:
    """Chats where an inbound message and its native reply are both recorded.

    First-run's last step asks whether two-way messaging actually works, and the
    adapter's own state is the only place that knows: ``request_correlations`` is
    written only after a real inbound event was forwarded, and ``reply_routes``
    only after the platform returned a native id for the reply.  A chat holding
    both, joined on the harness message id they share, is a completed exchange
    that this machine performed -- not a claim about one.

    The evidence is bounded like the rest of this store: an exchange pruned out
    of the correlation tables is no longer evidence.  Reads keep the
    :func:`observed_chats` contract -- ``mode=ro``, a missing database is an
    empty answer, an unreadable one is absence rather than failure -- so asking
    the question can never create the state being asked about.
    """

    if not path.is_file():
        return ()
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return ()
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT DISTINCT c.chat_id
                 FROM request_correlations AS c
                 JOIN reply_routes AS r
                   ON r.adapter = c.adapter
                  AND r.harness_message_id = c.harness_message_id
                  AND r.chat_id = c.chat_id
                WHERE c.adapter = ?
                  AND c.chat_id NOT IN (
                      SELECT chat_id FROM retired_chats WHERE adapter = ?)
                ORDER BY c.chat_id""",
            (adapter, adapter),
        ).fetchall()
        return tuple(str(row["chat_id"]) for row in rows)
    except sqlite3.Error:
        # A database predating either table (or one being written by a worker
        # mid-rotation) is absence of evidence, not a plan failure.
        return ()
    finally:
        connection.close()
