# Decision notes (E2E-031, 2026-11-19):
# - The agents of the base stage are plain agents with one registered channel
#   session each.  An agent's turn is what its harness
#   would run from its shell: the public CLI under that agent's verified
#   session (HYPRIAL_WORKER_ACTOR / HYPRIAL_WORKER_SESSION_REF).  An agent send
#   without that pair is refused SENDER_NOT_AUTHENTICATED, so the pair is the
#   agent's identity here, not a shortcut.
# - The owner writes as ``user:<owner>`` from the operator CLI.
# - The restart is graceful (``daemon stop`` then ``init``); the restart pass
#   has run when ``conversation.group_catch_up_done`` appears once more in the
#   scenario's own daemon log.
"""The base-stage assertions of E2E-031 (GC-01 .. GC-10)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .cli import SessionHeartbeat, pending_rows

from .fixture import (
    Expected,
    Group,
    daemon_log_count,
    daemon_pid,
    durable_rows,
    evidence,
    group_row_id,
    member_ids,
    require_ok,
    rows_for_entry,
    run_json,
    wait_for,
)

CATCH_UP_DONE = "conversation.group_catch_up_done"
RESTART_PASS_TIMEOUT = 60.0
CREATE_SETTLE_TIMEOUT = 60.0


@dataclass
class World:
    """The daemon, its agents and what the scenario has written so far."""

    daemon: Any
    owner: str
    nonce: str
    heartbeat: SessionHeartbeat
    agents: dict[str, str] = field(default_factory=dict)
    sessions: dict[str, str] = field(default_factory=dict)
    group: Group = field(default_factory=Group)
    history_ids: list[str] = field(default_factory=list)
    daemon_pids: list[int] = field(default_factory=list)

    def remember_pid(self) -> None:
        pid = daemon_pid(self.daemon)
        if pid is not None and pid not in self.daemon_pids:
            self.daemon_pids.append(pid)

    @property
    def owner_member(self) -> str:
        return f"user:{self.owner}"

    def worker_env(self, name: str) -> dict[str, str]:
        uri = self.agents[name]
        return {"HYPRIAL_WORKER_ACTOR": uri, "HYPRIAL_WORKER_SESSION_REF": self.sessions[uri]}

    def as_agent(self, name: str, *args: str) -> dict[str, Any]:
        self.heartbeat.check()
        return run_json(self.daemon, *args, env=self.worker_env(name))

    def pending(self, name: str) -> list[dict[str, Any]]:
        self.heartbeat.check()
        uri = self.agents[name]
        return pending_rows(self.daemon, uri, self.sessions[uri])

    def group_pending(self, name: str) -> list[dict[str, Any]]:
        return [row for row in self.pending(name)
                if row.get("conversationId") == self.group.group_id]

    def member(self, name: str) -> str:
        return self.group.members[name]

    def history(self) -> list[dict[str, Any]]:
        answer = run_json(self.daemon, "message", "history", "--conversation",
                          self.group.group_id, "--limit", "200")
        require_ok("message history --conversation <g>", answer)
        return [entry for entry in answer.get("entries") or [] if isinstance(entry, dict)]

    def entry(self, entry_id: str) -> dict[str, Any] | None:
        return next((item for item in self.history() if item.get("messageId") == entry_id), None)


def _one_row(world: World, entry_id: str, name: str) -> dict[str, Any]:
    """The single durable row *name* holds for *entry_id*, checked by id and count."""

    rows = rows_for_entry(world.daemon, world.group.group_id, entry_id)
    mine = rows.get(world.agents[name], [])
    expected_id = group_row_id(world.group.group_id, entry_id, world.member(name))
    if len(mine) != 1 or mine[0]["message_id"] != expected_id:
        raise Expected(f"durable inbox rows of {name} for entry {entry_id}",
                       f"exactly one row with id {expected_id}", mine)
    return mine[0]


def _no_rows(world: World, entry_id: str, *names: str) -> None:
    rows = rows_for_entry(world.daemon, world.group.group_id, entry_id)
    for name in names:
        address = world.owner_member if name == "owner" else world.agents[name]
        if rows.get(address):
            raise Expected(f"durable inbox rows of {name} for entry {entry_id}", "no row",
                           rows[address])


def _owner_send(
    world: World, summary: str, text: str, *, mentions: tuple[str, ...] = ()
) -> str:
    command = f"message send --from {world.owner_member} --conversation <g> --summary {summary}"
    args = [
        "message", "send", "--from", world.owner_member, "--conversation",
        world.group.group_id, "--summary", summary,
    ]
    for name in mentions:
        args.extend(["--mention", name])
    args.append(text)
    answer = require_ok(command, run_json(world.daemon, *args))
    deliveries = answer.get("deliveries") or []
    if answer.get("queued") is not False or len(deliveries) != 1 \
            or deliveries[0].get("target") != world.group.group_id:
        raise Expected(command, "one accepted delivery to the group id, queued:false", answer)
    entry_id = str(answer["messageId"])
    world.history_ids.append(entry_id)
    return entry_id


def _agent_reply(
    world: World,
    name: str,
    row_id: str,
    summary: str,
    text: str,
    *,
    mentions: tuple[str, ...] = (),
    no_mention: bool = False,
) -> str:
    command = f"message reply {row_id} --from {name} (as {name}'s session)"
    args = [
        "message", "reply", row_id, "--from", world.agents[name], "--summary", summary,
    ]
    for target in mentions:
        args.extend(["--mention", target])
    if no_mention:
        args.append("--no-mention")
    args.append(text)
    answer = require_ok(command, world.as_agent(name, *args))
    if answer.get("replied") is not True or not answer.get("replyMessageId"):
        raise Expected(command, "replied:true with a replyMessageId", answer)
    reply_id = str(answer["replyMessageId"])
    world.history_ids.append(reply_id)
    return reply_id


def _reply_entry(world: World, reply_id: str, name: str, replied: str) -> dict[str, Any]:
    entry = world.entry(reply_id)
    expected = {"intent": "reply", "from": world.member(name), "replyTo.messageId": replied}
    actual = None if entry is None else {
        "intent": entry.get("intent"), "from": entry.get("from"),
        "replyTo.messageId": (entry.get("replyTo") or {}).get("messageId")}
    if actual != expected:
        raise Expected(f"message history --conversation <g>: entry {reply_id}", str(expected),
                       entry)
    return entry


def setup(world: World, names: tuple[str, ...]) -> None:
    """GC-00: the agents exist."""

    from .cli import agent_uri

    for name in names:
        world.daemon.command("agent", "create", "--name", name, "--json")
        world.agents[name] = agent_uri(world.daemon, name)
    evidence("GC-00", agents=world.agents)


def sessions(world: World) -> None:
    """GC-00b: one verified channel session per agent, kept alive by the heartbeat thread."""

    from .cli import register_session

    for name, uri in world.agents.items():
        session_ref = f"{name.lower()}-{world.nonce[:8]}"
        register_session(world.daemon, uri, session_ref, world.daemon.root / f"{name.lower()}-cwd")
        world.sessions[uri] = session_ref
    world.heartbeat.start()
    evidence("GC-00b", sessions=sorted(world.sessions.values()))


def gc01_create(world: World) -> None:
    """GC-01: create a group with two agents; show and list report it."""

    title = f"e2e-031-{world.nonce}"
    command = "message group create --title <t> --member A --member B"
    started = time.monotonic()
    created = run_json(world.daemon, "message", "group", "create", "--title", title,
                       "--member", "A", "--member", "B")
    seconds = round(time.monotonic() - started, 2)
    if created.get("ok") is False and created.get("code") == "IPC_TIMEOUT":
        evidence("GC-01-create-timeout", answer=created, seconds=seconds)
        listing = wait_for(
            "the timed-out group create to land",
            lambda: run_json(world.daemon, "message", "group", "list"),
            lambda value: any(row.get("title") == title for row in value.get("conversations") or []),
            timeout=CREATE_SETTLE_TIMEOUT)
        row = next(row for row in listing["conversations"] if row.get("title") == title)
        world.group.group_id = str(row["conversationId"])
        raise Expected(command, "ok:true within the CLI's IPC budget",
                       {"answer": created, "groupLandedAnyway": world.group.group_id})
    require_ok(command, created)
    world.group.group_id = str(created["conversationId"])
    show = require_ok("message group show <g>", run_json(
        world.daemon, "message", "group", "show", world.group.group_id))
    world.group.members = member_ids(show)
    kinds = sorted((row.get("kind"), row.get("displayName")) for row in show.get("members") or [])
    expected = sorted([("user", world.owner), ("agent", "A"), ("agent", "B")])
    if kinds != expected or show.get("self") != world.owner_member:
        raise Expected("message group show <g>", f"members {expected}, self {world.owner_member}",
                       show)
    listing = run_json(world.daemon, "message", "group", "list")
    if world.group.group_id not in {row.get("conversationId") for row in listing.get("conversations") or []}:
        raise Expected("message group list", f"lists {world.group.group_id}", listing)
    evidence("GC-01", groupId=world.group.group_id, members=world.group.members, createSeconds=seconds)


def ensure_group(world: World) -> bool:
    """Whether a group exists to continue with, even when GC-01's answer failed."""

    if not world.group.group_id:
        return False
    if not world.group.members:
        show = run_json(world.daemon, "message", "group", "show", world.group.group_id)
        world.group.members = member_ids(show)
    return {"A", "B"} <= set(world.group.members)


def gc02_owner_send(world: World) -> None:
    """GC-02: unmentioned owner message wakes nobody; mentioning A and B wakes one row each."""

    quiet_id = _owner_send(world, "m0", "M0 from the owner without mentions")
    if world.entry(quiet_id) is None:
        raise Expected("message history: M0", "owner's unmentioned entry recorded", None)
    _no_rows(world, quiet_id, "A", "B", "owner")
    entry_id = _owner_send(world, "m1", "M1 from the owner to the group", mentions=("A", "B"))
    world.group.entries.append(entry_id)
    for name in ("A", "B"):
        row = _one_row(world, entry_id, name)
        if row["payload"].get("groupMessageId") != entry_id or row["consumed"] != 0:
            raise Expected(f"durable row of {name}", "pending row naming the entry", row)
        listed = [item for item in world.group_pending(name) if item.get("messageId") == row["message_id"]]
        if len(listed) != 1 or listed[0].get("from") != world.owner_member:
            raise Expected(f"message.pending.list {name}", "the row once, from the owner",
                           world.group_pending(name))
        world.group.rows[f"M1:{name}"] = row["message_id"]
    _no_rows(world, entry_id, "owner")
    evidence("GC-02", quiet=quiet_id, entry=entry_id, rows={k: v for k, v in world.group.rows.items()})


def gc03_turns(world: World) -> None:
    """GC-03: each agent takes its turn; the reply lands in the group and wakes nobody."""

    m1 = world.group.entries[0]
    replies = {}
    for name in ("A", "B"):
        reply_id = _agent_reply(world, name, world.group.rows[f"M1:{name}"], f"r1-{name}",
                                f"{name} answers M1")
        _reply_entry(world, reply_id, name, m1)
        rows = rows_for_entry(world.daemon, world.group.group_id, reply_id)
        if rows:
            raise Expected(f"durable rows for {name}'s reply", "none (a reply to the owner wakes nobody)",
                           rows)
        replies[name] = reply_id
        settled = [row for row in durable_rows(world.daemon, world.group.group_id)
                   if row["message_id"] == world.group.rows[f"M1:{name}"]]
        if len(settled) != 1 or settled[0]["consumed"] != 1:
            raise Expected(f"durable M1 row of {name} after its reply", "consumed:1", settled)
    for name in ("A", "B"):
        if world.group_pending(name):
            raise Expected(f"message.pending.list {name}", "no group row left after the turn",
                           world.group_pending(name))
    evidence("GC-03", replies=replies)


def gc04_mention(world: World) -> None:
    """GC-04: an agent message mentioning the other agent wakes only that one."""

    added = require_ok("message group members add <g> C", run_json(
        world.daemon, "message", "group", "members", "add", world.group.group_id, "C"))
    show = run_json(world.daemon, "message", "group", "show", world.group.group_id)
    world.group.members = member_ids(show)
    if "C" not in world.group.members:
        raise Expected("message group show <g>", "C listed after members add", {"add": added, "show": show})
    command = "message send --conversation <g> --mention B (as A's session)"
    answer = require_ok(command, world.as_agent(
        "A", "message", "send", "--conversation", world.group.group_id, "--mention", "B",
        "--summary", "m2", "M2 from A naming B"))
    entry_id = str(answer["messageId"])
    world.history_ids.append(entry_id)
    world.group.entries.append(entry_id)
    entry = world.entry(entry_id)
    if entry is None or entry.get("from") != world.member("A") \
            or entry.get("mentions") != [world.member("B")]:
        raise Expected("message history: A's entry", "from A, mentions [B]", entry)
    row = _one_row(world, entry_id, "B")
    if row["payload"].get("mentions") != [world.member("B")]:
        raise Expected("durable row of B", "payload mentions [B]", row)
    if not any(item.get("messageId") == row["message_id"] for item in world.group_pending("B")):
        raise Expected("message.pending.list B", "the mention row is admitted, not held",
                       world.group_pending("B"))
    _no_rows(world, entry_id, "A", "owner")
    world.group.rows["M2:B"] = row["message_id"]
    evidence("GC-04", entry=entry_id, row=row["message_id"])


def gc05_reply_needs_mention(world: World) -> None:
    """GC-05: a reply to an agent's entry mentions and wakes that author; --no-mention suppresses it."""

    m2 = world.group.entries[-1]
    reply_id = _agent_reply(world, "B", world.group.rows["M2:B"], "r2", "B answers A")
    reply_entry = _reply_entry(world, reply_id, "B", m2)
    if reply_entry.get("mentions") != [world.member("A")]:
        raise Expected("message history: B's plain reply", "mentions [A]", reply_entry)
    row = _one_row(world, reply_id, "A")
    if row["payload"].get("mentions") != [world.member("A")]:
        raise Expected("durable row of A for B's reply", "payload mentions [A]", row)
    _no_rows(world, reply_id, "B", "C", "owner")
    quiet_reply_id = _agent_reply(
        world, "A", row["message_id"], "r2-nomention", "A settles without waking",
        no_mention=True,
    )
    quiet_entry = _reply_entry(world, quiet_reply_id, "A", reply_id)
    if quiet_entry.get("mentions"):
        raise Expected("message history: A's --no-mention reply", "no mentions", quiet_entry)
    _no_rows(world, quiet_reply_id, "A", "B", "C", "owner")
    evidence("GC-05", defaultReply=reply_id, wokeA=row["message_id"],
             quietReply=quiet_reply_id)


def gc06_unmentioned(world: World) -> None:
    """GC-06: the member that was never mentioned holds no row at all."""

    rows = world.pending("C")
    durable = [row for row in durable_rows(world.daemon, world.group.group_id)
               if row["recipient"] == world.agents["C"]]
    if rows or durable:
        raise Expected("message.pending.list C and C's durable group rows", "none",
                       {"pending": rows, "durable": durable})
    evidence("GC-06", member="C", pending=0, durable=0)


def gc07_remove(world: World) -> None:
    """GC-07: a removed member gets no row for later messages; reply --mention wakes A."""

    removed = require_ok("message group members remove <g> B", run_json(
        world.daemon, "message", "group", "members", "remove", world.group.group_id, "B"))
    show = run_json(world.daemon, "message", "group", "show", world.group.group_id)
    if "B" in member_ids(show):
        raise Expected("message group show <g>", "B no longer listed", {"remove": removed, "show": show})
    entry_id = _owner_send(world, "m3", "M3 after B was removed", mentions=("A", "C"))
    world.group.entries.append(entry_id)
    for name in ("A", "C"):
        row = _one_row(world, entry_id, name)
        world.group.rows[f"M3:{name}"] = row["message_id"]
    rows = rows_for_entry(world.daemon, world.group.group_id, entry_id)
    if rows.get(world.agents["B"]):
        raise Expected("durable rows of removed B for M3", "no row", rows[world.agents["B"]])
    if any(item.get("conversationId") == world.group.group_id and
           item.get("messageId") == group_row_id(world.group.group_id, entry_id, world.member("B"))
           for item in world.pending("B")):
        raise Expected("message.pending.list B", "no M3 row", world.pending("B"))
    refused = world.as_agent("B", "message", "history", "--conversation", world.group.group_id)
    if refused.get("ok") is not False or refused.get("code") != "CONVERSATION_NOT_MEMBER":
        raise Expected("message history --conversation <g> (as removed B)",
                       "ok:false CONVERSATION_NOT_MEMBER", refused)
    require_ok("message ack <M3 row> --from A", world.as_agent(
        "A", "message", "ack", world.group.rows["M3:A"], "--from", world.agents["A"]))
    tagged_reply_id = _agent_reply(
        world, "C", world.group.rows["M3:C"], "r3-mention-a", "C tags A on M3",
        mentions=("A",),
    )
    tagged_entry = _reply_entry(world, tagged_reply_id, "C", entry_id)
    if tagged_entry.get("mentions") != [world.member("A")]:
        raise Expected("message history: C's --mention reply", "mentions [A]", tagged_entry)
    woke_a = _one_row(world, tagged_reply_id, "A")
    _no_rows(world, tagged_reply_id, "B", "C", "owner")
    require_ok("message ack <C reply row> --from A", world.as_agent(
        "A", "message", "ack", woke_a["message_id"], "--from", world.agents["A"]))
    evidence("GC-07", entry=entry_id, removed=world.member("B"),
             taggedReply=tagged_reply_id, wokeA=woke_a["message_id"])


def gc08_restart(world: World) -> None:
    """GC-08: a restart between the owner's send and the agents' turn duplicates no row."""

    entry_id = _owner_send(world, "m4", "M4 before the restart", mentions=("A", "C"))
    world.group.entries.append(entry_id)
    before = {name: _one_row(world, entry_id, name)["message_id"] for name in ("A", "C")}
    done_before = daemon_log_count(world.daemon, CATCH_UP_DONE)
    snapshot = sorted(row["message_id"] for row in durable_rows(world.daemon, world.group.group_id))
    world.remember_pid()
    world.heartbeat.stop()
    world.daemon.stop()
    world.daemon.wait_stopped()
    world.daemon.start()
    world.heartbeat.reconnect()
    wait_for("the restart pass to finish", lambda: daemon_log_count(world.daemon, CATCH_UP_DONE),
             lambda count: count > done_before, timeout=RESTART_PASS_TIMEOUT)
    world.remember_pid()
    whole = sorted(row["message_id"] for row in durable_rows(world.daemon, world.group.group_id))
    if whole != snapshot:
        raise Expected("every durable group row after the restart pass", f"the same {len(snapshot)} ids",
                       {"before": snapshot, "after": whole})
    after = {name: _one_row(world, entry_id, name)["message_id"] for name in ("A", "C")}
    if after != before:
        raise Expected("durable rows for M4 after restart", f"unchanged {before}", after)
    for name in ("A", "C"):
        listed = [item for item in world.group_pending(name) if item.get("messageId") == before[name]]
        if len(listed) != 1:
            raise Expected(f"message.pending.list {name} after restart", "the M4 row exactly once",
                           world.group_pending(name))
    reply_id = _agent_reply(world, "A", before["A"], "r4", "A answers M4 after the restart")
    _reply_entry(world, reply_id, "A", entry_id)
    replies = [item for item in world.history()
               if (item.get("replyTo") or {}).get("messageId") == entry_id]
    if [item.get("messageId") for item in replies] != [reply_id]:
        raise Expected("message history: replies to M4", f"exactly [{reply_id}]", replies)
    require_ok("message ack <M4 row> --from C", world.as_agent(
        "C", "message", "ack", before["C"], "--from", world.agents["C"]))
    evidence("GC-08", entry=entry_id, rows=before, reply=reply_id)


def gc09_one_to_one(world: World) -> None:
    """GC-09: the group stays out of 1:1 conversations, threads and rows."""

    direct = require_ok("message send --from <owner> --to A", run_json(
        world.daemon, "message", "send", "--from", world.owner_member, "--to", "A",
        "--summary", "direct", "a 1:1 message"))
    direct_id = str(direct.get("messageId") or "")
    rows = wait_for("A's 1:1 row", lambda: world.pending("A"),
                    lambda items: any(item.get("messageId") == direct_id for item in items),
                    timeout=20.0)
    direct_row = next(item for item in rows if item.get("messageId") == direct_id)
    direct_conversation = str(direct_row.get("conversationId"))
    if direct_conversation.startswith("g."):
        raise Expected("A's 1:1 row", "a non-group conversation id", direct_row)
    readers = {"owner": run_json(world.daemon, "message", "conversation", "list"),
               "A": world.as_agent("A", "message", "conversation", "list")}
    for who, answer in readers.items():
        ids = {row.get("conversationId") for row in answer.get("conversations") or []}
        if answer.get("ok") is False or world.group.group_id in ids \
                or any(str(value).startswith("g.") for value in ids):
            raise Expected(f"message conversation list ({who})", "no group conversation", answer)
    listed = next((row for row in readers["owner"].get("conversations") or []
                   if direct_conversation in (row.get("conversationId"), row.get("legacyConversationId"))),
                  None)
    if listed is None:
        raise Expected("message conversation list (owner)",
                       f"lists the direct conversation {direct_conversation}", readers["owner"])
    page = run_json(world.daemon, "message", "history", "--conversation", str(listed["conversationId"]))
    leaked = [entry for entry in page.get("entries") or [] if entry.get("messageId") in world.history_ids]
    if page.get("ok") is False or leaked or direct_id not in {
            entry.get("messageId") for entry in page.get("entries") or []}:
        raise Expected("message history --conversation <direct c.>",
                       "the direct message and no group entry", page)
    evidence("GC-09", direct=direct_conversation, groupAbsent=True)


def gc10_history(world: World) -> None:
    """GC-10: the group's history holds every entry once, in the order written."""

    entries = world.history()
    keys = [(entry.get("createdAtMs"), entry.get("messageId")) for entry in entries]
    if keys != sorted(keys) or len({entry.get("messageId") for entry in entries}) != len(entries):
        raise Expected("message history --conversation <g>", "unique entries in (createdAtMs, id) order",
                       keys)
    interaction = [entry.get("messageId") for entry in entries
                   if entry.get("messageClass") == "interaction"]
    if interaction != world.history_ids:
        raise Expected("message history --conversation <g>: interaction entries",
                       str(world.history_ids), interaction)
    events = [(entry.get("system") or {}).get("event") for entry in entries
              if entry.get("messageClass") == "system"]
    expected_events = ["created", "member-added", "member-added", "member-added", "member-removed"]
    if events != expected_events:
        raise Expected("message history --conversation <g>: system entries", str(expected_events),
                       events)
    evidence("GC-10", entries=len(entries), interaction=interaction, system=events)


__all__ = [
    "World",
    "ensure_group",
    "gc01_create",
    "gc02_owner_send",
    "gc03_turns",
    "gc04_mention",
    "gc05_reply_needs_mention",
    "gc06_unmentioned",
    "gc07_remove",
    "gc08_restart",
    "gc09_one_to_one",
    "gc10_history",
    "sessions",
    "setup",
]
