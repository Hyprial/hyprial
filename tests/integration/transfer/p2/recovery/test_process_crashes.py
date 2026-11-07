"""TC013/014/015 protocol prerequisites, NOT real daemon E2E acceptance.

Pipe barriers produce repeatable abrupt process exits before/after synthetic
local effects. A missing journal fails a predicted behavior assertion at ready;
child crashes/import errors are diagnosed separately as harness failures.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys

import pytest


class Peer:
    def __init__(self, root, role, operation):
        self.root = root
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "PYTHONPATH", "SYSTEMROOT")}
        env.update(HOME=str(root), HYPRIAL_HOME=str(root / "home"),
                   HARNESS_STATE_DIR=str(root / "state"), HYPRIAL_NETWORK_ISOLATED="1")
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("process_peer.py")), str(root), role, operation],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )

    def read(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            assert selector.select(10), "protocol harness did not reach its pipe barrier"
        line = self.process.stdout.readline()
        assert line, f"protocol peer exited unexpectedly: {self.process.poll()}"
        return json.loads(line)

    def write(self, command, **params):
        self.process.stdin.write(json.dumps(dict(command=command, **params)) + "\n")
        self.process.stdin.flush()

    def call(self, command, **params):
        self.write(command, **params)
        return self.read()

    def crash(self):
        self.process.kill()
        self.process.wait(timeout=10)

    def close(self):
        if self.process.poll() is None:
            self.crash()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


@contextmanager
def peer(root, role="source", operation="op"):
    item = Peer(root, role, operation)
    try:
        ready = item.read()
        assert ready["event"] == "ready", "subprocess setup failed before protocol barrier"
        assert ready["supported"], "P2 lacks durable phase/intent authority after process restart"
        yield item
    finally:
        item.close()


def test_tc013_two_processes_only_winner_can_begin_effect(tmp_path):
    with peer(tmp_path, operation="one") as first, peer(tmp_path, operation="two") as second:
        # Both independent processes are ready before either is released.
        for item in (first, second):
            item.write("begin", step="quiesce", fingerprint="b" * 64)
        results = [first.read(), second.read()]
        assert sum(r["event"] == "intent" and r["execute"] for r in results) == 1
        assert sum(r == {"event": "rejected", "reason": "reserved"} for r in results) == 1
        winner = (first, second)[next(i for i, r in enumerate(results) if r["event"] == "intent")]
        assert winner.call("effect")["event"] == "effect"
    assert len(list(tmp_path.glob("*.effect"))) == 1


@pytest.mark.parametrize("crash_after_effect", [False, True])
def test_tc014_source_crash_retains_intent_and_never_reissues_ticket(tmp_path, crash_after_effect):
    with peer(tmp_path) as source:
        intent = source.call("begin", step="quiesce", fingerprint="b" * 64)
        assert intent["execute"]
        if crash_after_effect:
            assert source.call("effect")["event"] == "effect"
        source.crash()
    with peer(tmp_path) as restarted:
        state = restarted.call("read")
        assert state["state"]["phase"] == "quiesce-pending"
        assert state["blocked"] and not state["replacement_blocked"]
        replay = restarted.call("begin", step="quiesce", fingerprint="b" * 64)
        assert not replay["execute"], "unknown is not permission to repeat an effect"
        assert restarted.call("cancel") == {"event": "rejected", "reason": "phase-conflict"}
    assert (tmp_path / "op.effect").exists() == crash_after_effect


@pytest.mark.parametrize("activate_pending", [False, True])
def test_tc015_target_crash_does_not_promote_land_or_pending_activation(tmp_path, activate_pending):
    with peer(tmp_path, "target") as target:
        assert target.call("begin", step="land", fingerprint="c" * 64)["execute"]
        target.call("effect")
        assert target.call("finish", receipt="land-ok", token="target-token")["state"]["phase"] == "landed"
        if activate_pending:
            assert target.call("begin", step="activate", fingerprint="d" * 64)["execute"]
        target.crash()
    with peer(tmp_path, "target") as restarted:
        state = restarted.call("read")["state"]
        assert state["phase"] == ("activate-pending" if activate_pending else "landed")
        assert state["phase"] != "active"
    assert (tmp_path / "op.effect").read_text().splitlines() == ["synthetic-effect"]


def test_tc014_explicit_override_audit_survives_crash_without_unlock(tmp_path):
    with peer(tmp_path) as source:
        source.call("begin", step="quiesce", fingerprint="b" * 64)
        result = source.call("override")
        assert result["state"]["needs_attention"]
        source.crash()
    with peer(tmp_path) as restarted:
        assert restarted.call("read")["blocked"]
        audit = restarted.call("audit")["audit"]
        assert len(audit) == 1
        assert audit[0]["principal"] == "synthetic-operator"
        assert audit[0]["action"] == "manual-restore"
        assert audit[0]["recorded_at_ms"] > 0


def test_two_process_replay_of_same_id_has_only_one_execution_ticket(tmp_path):
    with peer(tmp_path) as first, peer(tmp_path) as second:
        for item in (first, second):
            item.write("begin", step="quiesce", fingerprint="b" * 64)
        results = [first.read(), second.read()]
        assert all(r["event"] == "intent" for r in results)
        assert sum(r["execute"] for r in results) == 1
        assert results[0]["state"] == results[1]["state"]


def test_tc014_quiesced_result_remains_fenced_after_process_restart(tmp_path):
    with peer(tmp_path) as source:
        source.call("begin", step="quiesce", fingerprint="b" * 64)
        source.call("effect")
        result = source.call("finish", receipt="stop-receipt", token="source-incarnation")
        assert result["state"]["phase"] == "quiesced"
        source.crash()
    with peer(tmp_path) as restarted:
        state = restarted.call("read")
        assert state["state"]["phase"] == "quiesced"
        assert state["state"]["receipt"] == "stop-receipt"
        assert state["blocked"]
