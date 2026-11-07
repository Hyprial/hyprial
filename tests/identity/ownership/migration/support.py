"""The one-time owner-segment rewrite, and the three things it must NOT change.

The literal old owner appears with four meanings in real state (see
``owner_migration``'s module docstring).  Two of them are not owners at all,
and rewriting either is a data-loss bug that no green test would catch unless
it is asserted negatively.  Hence the shape of this file: the positive case is
one test, and most of the rest assert byte-identity of things that must not
move.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path



OLD = "bobadmin"
NEW = "alicesmith"

#: The peer really is named this: the old owner is a PREFIX of another node's
#: machine segment.  This single fact is why a substring replace is wrong.
# ⛔ NOT a peer: this is what THIS machine was called before its 2026-08-17
# rename.  The old owner is a prefix of it, so a bare replace would falsify
# this node's own history.  (Earlier revisions called it a peer — wrong
# reason, same required behaviour.)
FORMER_SELF = "studio-node-mac-01.example-tailnet.ts.net"


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    return state


def _seed_agents(state: Path) -> None:
    db = sqlite3.connect(state / "agents.sqlite3")
    with db:
        db.execute("CREATE TABLE agents (actor TEXT, owner TEXT, uri TEXT, cwd TEXT)")
        db.execute(
            "INSERT INTO agents VALUES (?, ?, ?, ?)",
            (
                "squire",
                OLD,  # bare owner column
                f"agent:{OLD}:node-hq-01:squire",  # address
                f"/Users/{OLD}/Workspace/hyprial",  # ⛔ path, must not move
            ),
        )
        # The real historical shape (pre-P1a dev schema): lifecycle_resources
        # has always had resource_key/resource_token/active.  The P1a custody
        # counter queries this table, and by the round-2 rule only a table
        # *verified absent* counts as zero — a present but unqueryable shape
        # is a typed refusal, so the fixture must model the queryable reality.
        db.execute(
            "CREATE TABLE lifecycle_resources ("
            "resource_key TEXT PRIMARY KEY, resource_token TEXT NOT NULL, "
            "active INTEGER NOT NULL, payload TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO lifecycle_resources VALUES (?, ?, ?, ?)",
            (
                "agent-record:worker",
                "token",
                1,
                # ⭐ one cell holding BOTH an address and a path
                json.dumps(
                    {
                        "actor": f"agent:{OLD}:node-hq-01:worker",
                        "cwd": f"/Users/{OLD}/checkout",
                    }
                ),
            ),
        )
    db.close()


def _seed_inbox(state: Path) -> None:
    db = sqlite3.connect(state / "inbox.sqlite3")
    with db:
        db.execute("CREATE TABLE inbox (sender TEXT, recipient TEXT)")
        db.execute(
            "INSERT INTO inbox VALUES (?, ?)",
            (
                f"agent:{OLD}:node-hq-01:me",
                # ⛔ a peer address: its OWNER is already the new value, and the
                # old owner survives only as a prefix of the peer's machine.
                f"agent:{NEW}:{FORMER_SELF}:hq-adjutant",
            ),
        )
        db.execute("CREATE TABLE dlq (owner TEXT)")
        # ⛔ despite the column name, this holds a machine, not an owner.
        db.execute("INSERT INTO dlq VALUES (?)", (FORMER_SELF,))
    db.close()


# ── residual: connector / Lark app names under precise external conventions ──
#
# U5 drill 2026-09-05 (PR #372 runbook reading 5, card 8595e1e9): switching a
# production state copy away from the old owner aborted on seven cells of
# adapters.sqlite3 identities.adapter / display_name -- `lark:alicesmith-hyprial-e2e`
# and `alicesmith-hyprial-e2` (the bot display name, truncated by Lark).  Those are
# external identifiers that merely follow the org's naming convention; the
# connector name is also the key behind every adapter:lark:<name> address.
# They must survive byte-identical, and the form must stay narrow enough that
# an unrelated `<owner>-other` spelling still aborts.


def _seed_identities(state: Path, *cells: tuple[str, str]) -> None:
    db = sqlite3.connect(state / "adapters.sqlite3")
    with db:
        db.execute("CREATE TABLE identities (adapter TEXT, display_name TEXT, actor TEXT)")
        for adapter, display in cells:
            db.execute(
                "INSERT INTO identities VALUES (?, ?, ?)",
                (adapter, display, f"agent:{OLD}:{FORMER_SELF}:hq-adjutant"),
            )
    db.close()


# ── P1b B0: pac-graph.sqlite3 joins the owner-migration input set ──────────────


def _seed_pac_graph(
    state: Path,
    *,
    old: str = OLD,
    with_sentinels: bool = True,
) -> Path:
    """Real pac-graph schema via the real store; sentinel rows per owner column.

    The schema comes from ``hyprial.daemon.impl.pac.store.connect`` (which runs the real
    schema migrations), never from a hand-written DDL copy; the rows carry the
    principal shapes the write boundaries actually store (``_validate_owner``
    and ``parse_principal`` accept only full URIs; bare names appear only as
    user-principal display values, e.g. notifications.recipient).
    """

    from hyprial.daemon.impl.pac.storage.store import connect as pac_connect

    path = state / "pac-graph.sqlite3"
    db = pac_connect(path)
    try:
        with db:
            db.execute(
                "INSERT INTO graphs (graph_id, name, version, created_by,"
                " created_at) VALUES ('g1', 'graph', 1, ?, 111)",
                (f"user:{old}",),
            )
            db.execute(
                "UPDATE graphs SET activated_at = 222, activated_by = ?,"
                " closed_at = 333, closed_by = ? WHERE graph_id = 'g1'",
                (f"agent:{old}:node:worker", f"user:{old}"),
            )
            db.execute(
                "INSERT INTO nodes (graph_id, node_id, owner, brief_ref, kind,"
                " flag, flag_set_by, flag_set_at) VALUES"
                " ('g1', 'n1', ?, 'brief', 'task', 1, ?, 444)",
                (f"user:{old}", f"user:{old}"),
            )
            db.execute(
                "INSERT INTO flag_events (event_id, graph_id, version, node_id,"
                " action, actor, at) VALUES ('e1', 'g1', 2, 'n1', 'set', ?, 444)",
                (f"user:{old}",),
            )
            db.execute(
                "INSERT INTO journal (event_id, graph_id, version, type, at,"
                " data_json) VALUES ('j1', 'g1', 2, 'flag_set', 444, ?)",
                (
                    json.dumps(
                        {
                            "nodeId": "n1",
                            "action": "set",
                            "actor": f"agent:{old}:node:worker",
                            "owner": f"user:{old}",
                        }
                    ),
                ),
            )
            db.execute(
                "INSERT INTO notifications (event_id, edge, kind, recipient,"
                " round_no, text, sender, at, plan_json) VALUES"
                " ('e1', 'n1 -> n2', 'turn', ?, 1, ?, ?, 444, ?)",
                (
                    old,
                    f"turn for {old}",
                    f"user:{old}",
                    json.dumps({"graphId": "g1", "version": 2}),
                ),
            )
            # The real B2 report shape (migrations.py builds ``{"graphs": […
            # {"kept": […short names…]}]}``; ``unrewritten_owners_note`` reads
            # exactly this) — a kept short name is a record of the v8 era, and
            # must survive a rename byte-for-byte.
            db.execute(
                "INSERT OR REPLACE INTO schema_era (id, user_version,"
                " migrated_at_ms, graphs_total, owners_rewritten, owners_kept,"
                " report_json) VALUES (1, 9, 555, 1, 0, 1, ?)",
                (
                    json.dumps(
                        {"graphs": [{"graphId": "g1", "kept": [old]}]}
                    ),
                ),
            )
    finally:
        db.close()
    return path


def _pac_value(state: Path, sql: str) -> object:
    from hyprial.daemon.impl.pac.storage.store import connect as pac_connect

    db = pac_connect(state / "pac-graph.sqlite3", read_only=True)
    try:
        return db.execute(sql).fetchone()[0]
    finally:
        db.close()
