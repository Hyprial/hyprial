"""Phase 7's public runner seam must execute the extracted custody assertion."""

import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("observed", [(0, 1), (1, 0)])
def test_mailbox_hold_assertion_checks_actual_counts(monkeypatch, observed):
    runner = runpy.run_path(str(
        Path(__file__).resolve().parents[4]
        / "contract/e2e/run-two-machine-roundtrip.py"
    ))
    assertion = runner["assert_holds"]
    namespace = assertion.__globals__
    calls = []

    def wait_for(read, predicate, *, timeout, what):
        calls.append((timeout, what))
        assert predicate(read()), "hold counts did not settle"

    def exec_json(node, command):
        assert (node, command) == ("mailbox", "ps")
        return {"outboxCount": observed[0], "custodyCount": observed[1]}

    monkeypatch.setitem(namespace, "exec_json", exec_json)
    monkeypatch.setitem(namespace, "wait_for", wait_for)
    if observed == (0, 1):
        assertion("phase7", "mailbox", outbox=0, custody=1, timeout=0.5)
    else:
        with pytest.raises(AssertionError, match="hold counts did not settle"):
            assertion("phase7", "mailbox", outbox=0, custody=1, timeout=0.5)
    assert calls == [(0.5, "phase7 mailbox settles at outbox/custody (0, 1)")]
