"""``hyprial agent attach``/``detach`` render the daemon facts, nothing more.

The commands are one request each; the text view states the agent URI,
custody, what changed and where the cell change stands.  The JSON view is
the daemon object itself (exercised end to end in
``tests/integration/cli/attach``).
"""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.agent.attach import render_attachment

BASE = {
    "ok": True,
    "actor": "agent:op:node:ext",
    "name": "ext",
    "agentId": "a.x",
    "cellId": "c.y",
    "custody": "external",
}


def test_a_new_attach_says_a_new_identity_was_registered() -> None:
    text = render_attachment({**BASE, "attached": True, "changed": True, "created": True,
                              "cell": {"state": "recorded", "revision": 1}})
    lines = text.splitlines()
    assert lines[0] == ("attached agent:op:node:ext   custody external   "
                        "new agent identity registered")
    assert lines[1] == "agent a.x   cell c.y   cell recorded"
    assert "'hyprial agent start' refuses it" in lines[2]
    assert "hyprial message batch --from ext" in lines[3]
    assert "observe sender auth" in lines[3]
    assert "--from user:<owner> --on-behalf-of ext" in lines[3]


def test_a_repeat_says_nothing_changed_and_a_deferred_cell_is_named() -> None:
    text = render_attachment({**BASE, "attached": True, "changed": False, "created": False,
                              "cell": {"state": "deferred", "revision": None}})
    assert "already attached; nothing changed" in text
    assert "cell deferred; reconciled when the cell runtime is reachable" in text


def test_skipped_and_refused_cells_are_named() -> None:
    for state, words in (("skipped", "owner is not the OrgFS author"),
                         ("refused", "not retried")):
        text = render_attachment({**BASE, "attached": False, "changed": False,
                                  "cell": {"state": state, "revision": None}})
        assert words in text.splitlines()[1]


def test_detach_has_no_read_hint() -> None:
    text = render_attachment({**BASE, "attached": False, "changed": True,
                              "cell": {"state": "recorded", "revision": 3}})
    assert text.splitlines()[0].startswith("detached agent:op:node:ext")
    assert "message batch" not in text and len(text.splitlines()) == 2
