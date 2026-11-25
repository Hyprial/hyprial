"""The group wake rule (2026-11-21 ruling; slice GC1): a pure function table."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.conversations.groups import ReplyTo, wake_members
from tests.daemon.conversations.groups.group_support import (
    OWNER,
    REVIEWER,
    WORKER,
    added,
    member,
    message,
)

TESTER = "a.testeraaaaaaaaaaaaaaaaaaaa"
OUTSIDER = "a.outsideraaaaaaaaaaaaaaaaaa"
MEMBERS = (member(OWNER, manager=True), member(WORKER), member(REVIEWER), member(TESTER))


def _reply(sender: str) -> ReplyTo:
    return ReplyTo(message_id="m0", sender=sender, excerpt="earlier")


@pytest.mark.parametrize(
    ("author", "mentions", "reply_to", "expected"),
    [
        (OWNER, (), None, ()),
        (OWNER, (REVIEWER,), None, (REVIEWER,)),
        (OWNER, (REVIEWER, WORKER), None, (WORKER, REVIEWER)),
        (OWNER, (), _reply(WORKER), ()),
        (OWNER, (OWNER,), None, ()),
        (WORKER, (), None, ()),
        (WORKER, (REVIEWER,), None, (REVIEWER,)),
        (WORKER, (TESTER, REVIEWER), None, (REVIEWER, TESTER)),
        (WORKER, (OWNER,), None, ()),
        (WORKER, (OUTSIDER,), None, ()),
        (WORKER, (WORKER,), None, ()),
        (WORKER, (), _reply(REVIEWER), ()),
        (WORKER, (), _reply(OWNER), ()),
        (WORKER, (), _reply(WORKER), ()),
        (WORKER, (), _reply(OUTSIDER), ()),
        (WORKER, (REVIEWER,), _reply(REVIEWER), (REVIEWER,)),
        (WORKER, (TESTER,), _reply(REVIEWER), (TESTER,)),
        (OUTSIDER, (REVIEWER,), None, ()),
        (OUTSIDER, (), _reply(REVIEWER), ()),
    ],
)
def test_wake_table(author, mentions, reply_to, expected) -> None:
    entry = message("m1", author, at=10, mentions=mentions, reply_to=reply_to)
    assert wake_members(entry, MEMBERS) == expected


def test_the_human_author_never_wakes_itself_and_wakes_no_user() -> None:
    entry = message("m1", OWNER, at=10, mentions=(OWNER,))
    assert OWNER not in wake_members(entry, MEMBERS)


def test_system_entries_wake_nobody() -> None:
    assert wake_members(added(TESTER, at=10), MEMBERS) == ()
