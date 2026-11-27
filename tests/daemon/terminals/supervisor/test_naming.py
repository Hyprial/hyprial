import re

from hyprial.daemon.impl.terminals.supervisor.naming import (
    member_terminal_labels,
    member_terminal_name,
    parse_member_terminal_labels,
    reconcile_orphan_terminals,
)


def test_member_name_and_labels_are_deterministic_and_role_specific() -> None:
    agent_id = "用户/agent/with spaces and a very long suffix" * 4
    member = "成员:🚀/with another long name" * 4
    tui = member_terminal_name(agent_id, member, "tui")
    app = member_terminal_name(agent_id, member, "app-server")

    assert re.fullmatch(r"[a-z0-9._-]{1,32}", tui)
    assert re.fullmatch(r"[a-z0-9._-]{1,32}", app)
    assert tui != app
    labels = member_terminal_labels(agent_id, member, "tui", "digest-1")
    assert parse_member_terminal_labels(labels) == {
        "agent_id": agent_id,
        "member": member,
        "role": "tui",
        "spec_digest": "digest-1",
    }


def test_orphan_reconciliation_only_returns_inactive_labeled_terminals() -> None:
    active = [{"agent_id": "a1", "member": "m1"}]
    terminals = [
        {
            "address": "t1",
            "state": "live",
            "labels": member_terminal_labels("a1", "m1", "tui", "d1"),
        },
        {
            "address": "t2",
            "state": "live",
            "labels": member_terminal_labels("a2", "m2", "tui", "d2"),
        },
        {"address": "t3", "state": "live", "labels": {"other": "value"}},
    ]

    assert reconcile_orphan_terminals(terminals, active) == ("t2",)
