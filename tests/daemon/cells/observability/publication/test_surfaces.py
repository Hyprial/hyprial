"""Read-only public surfaces and the one-request shared publication cut."""

from hyprial.daemon.impl.operations.top import build_actor_row
from tests.daemon.cells.observability.mount.test_mount import composed as composed
from tests.shell.diagnostics.test_ps_status_snapshot import FakeInbox, StubPresence


def test_ps_is_additive_bounded_and_does_not_fetch_or_clear_inbox(composed, monkeypatch):
    app, launcher, harness, *_ = composed
    app._inbox = FakeInbox()
    app._presence = StubPresence()
    for index in range(8):
        app.agents.create(f"unstarted-{index}")
    calls = 0
    read = harness.projection.read_run_facts

    def group_read():
        nonlocal calls
        calls += 1
        return read()

    def forbidden(*args, **kwargs):
        raise AssertionError("status cannot consume business state")

    monkeypatch.setattr(harness.projection, "read_run_facts", group_read)
    monkeypatch.setattr(app._inbox, "ack", forbidden)
    monkeypatch.setattr(app._inbox, "submit", forbidden)
    app._inbox.pending_messages = forbidden
    app._inbox.fetch = forbidden
    app._inbox.clear_notices = forbidden
    for _ in range(2):
        rows = app.handle("ps", {})["agents"]
        assert len(rows) == 8
        assert len({row["runState"]["revision"] for row in rows}) == 1
        assert all(row["runState"]["state"] == "unknown" for row in rows)
        assert all("entityToken" not in row for row in rows)
    assert calls == 2 and launcher.starts == 0
    assert app._inbox.pending == []


def test_top_receives_same_projection_without_rederiving_run_state(composed):
    app, *_ = composed
    app._presence = StubPresence()
    app.agents.create("unstarted")
    with app._worker_status_snapshot() as cut:
        status = app._actor_status_snapshot()[0]
        assert status["runState"] == cut.run_states[status["actor"]]
        top = build_actor_row(state_dir=app.state_dir, owner=app.owner, node_id=app.node_id,
                              status=status, pending=(0, None), now_ms=100)
        assert top["runState"] == status["runState"]
        assert top["processState"] == "not_applicable"
        assert top["runState"]["state"] == "unknown"
