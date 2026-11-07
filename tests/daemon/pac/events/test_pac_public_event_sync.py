"""Deterministic coverage for the E2E-020-C event/snapshot read boundary."""

from __future__ import annotations

import importlib.util
import copy

import pytest
from pathlib import Path
from types import SimpleNamespace

import hyprial.daemon.impl.pac.graphs.subscription as subscription
from hyprial.daemon.impl.pac.graphs.edits import activate_graph, add_node, create_graph
from hyprial.daemon.impl.pac.graphs.projection import Projection
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore


def _load_runner():
    path = (
        Path(__file__).resolve().parents[4] / 'contract/e2e/run-pac-public-events.py'
    )
    spec = importlib.util.spec_from_file_location("pac_public_events_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_consumer_finally_converges_when_clock_event_lands_between_reads(
    tmp_path, monkeypatch
):
    runner_dir = Path(__file__).resolve().parents[4] / 'contract/e2e'
    monkeypatch.syspath_prepend(str(runner_dir))
    monkeypatch.setenv("HYPRIAL_OWNER", "owner")
    runner = _load_runner()

    store = PacGraphStore(tmp_path / "pac-graph.sqlite3")
    try:
        graph = create_graph(store, name="sync-race", created_by="user:owner")["graphId"]
        add_node(
            store,
            tmp_path,
            graph_id=graph,
            node_id="clock",
            owner="user:owner",
            brief_ref="artifact#clock",
            kind="clock",
            deadline_ms=1,
            expect_version=1,
        )
        activate_graph(store, graph, actor="user:owner")
        baseline = Projection(store.path).public_snapshot(graph)
        consumer = runner.Consumer(baseline)
        reactor = PacReactor(store, clock=lambda: 2)

        class InterleavingPublicClient:
            """Use real public projections; only schedule the writer deterministically."""

            event_reads = 0
            snapshots = []

            def events(self, graph_id, after, journal_id):
                self.event_reads += 1
                page = subscription.events_since(
                    store.path, graph_id, after, journal_id=journal_id
                )
                if self.event_reads == 1:
                    reactor.tick_clocks(graph_id)
                return page["events"]

            def snapshot(self, graph_id):
                snapshot = Projection(store.path).public_snapshot(graph_id)
                self.snapshots.append(snapshot)
                return snapshot

        client = InterleavingPublicClient()
        fresh = runner.sync_consumer_snapshot(client, consumer, graph)

        assert baseline["notifications"] == []
        assert len(client.snapshots[0]["notifications"]) == 1
        assert client.event_reads == 2
        assert runner.comparable(consumer.state) == runner.comparable(fresh)
    finally:
        store.close()


@pytest.mark.parametrize("resident_won", [False, True])
def test_legacy_receipt_baseline_waits_for_current_clock_delivery(monkeypatch, resident_won):
    runner_dir = Path(__file__).resolve().parents[4] / 'contract/e2e'
    monkeypatch.syspath_prepend(str(runner_dir))
    runner = _load_runner()
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=runner.time.monotonic, sleep=lambda _: None))
    old = {"eventId": "old-v5", "edge": "a -> b", "messageId": None, "deliveredAt": None}
    clock = {"eventId": "overdue:fixture:v6:timer:50", "edge": "clock:timer",
             "messageId": None, "deliveredAt": None}

    class PublicClient:
        calls = []
        reads = 0

        def invoke(self, *args):
            self.calls.append(args)
            # In the race the resident already inserted the clock; a duplicate
            # public tick returns no newly planned notifications.
            return [{"overdue": [] if resident_won else [clock]}]

        def snapshot(self, graph):
            assert graph == "fixture"
            self.reads += 1
            current = copy.deepcopy(clock)
            if not resident_won or self.reads == 2:
                current.update(messageId="real-clock-receipt", deliveredAt=123)
            return {"version": 6, "cursor": self.reads + 2,
                    "notifications": [copy.deepcopy(old), current]}

    client = PublicClient()
    baseline = runner.legacy_receipt_bootstrap(client, "fixture")
    assert client.calls == []
    assert client.reads == 2
    assert baseline["notifications"][0] == old  # do not resend the old backlog
    assert baseline["notifications"][1]["messageId"] == "real-clock-receipt"


@pytest.mark.parametrize("event_id", ["overdue:fixture:v6:timer:50", "overdue:fixture:v5:timer:50"])
def test_legacy_receipt_baseline_rejects_missing_current_clock_delivery(monkeypatch, event_id):
    runner_dir = Path(__file__).resolve().parents[4] / 'contract/e2e'
    monkeypatch.syspath_prepend(str(runner_dir))
    runner = _load_runner()
    moments = iter([0, 21])
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: next(moments)))

    class PublicClient:
        def invoke(self, *args):
            return [{"overdue": []}]

        def snapshot(self, graph):
            return {"notifications": [{"eventId": event_id, "edge": "clock:timer",
                     "messageId": "old-receipt" if ":v5:" in event_id else None,
                     "deliveredAt": 123 if ":v5:" in event_id else None}]}

    with pytest.raises(AssertionError, match="v6 clock did not finish delivery"):
        runner.legacy_receipt_bootstrap(PublicClient(), "fixture")
