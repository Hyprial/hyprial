"""B-R1: a retained external source must not retain an old binding cursor."""

import pytest

from hyprial.daemon.impl.operations.session_ports import RegisterSessionCommand, SessionMutationCompleted
from hyprial.kernel import CHANNEL_PROTOCOL_VERSION
from tests.daemon.cells.observability.mount.test_mount import composed as composed


@pytest.mark.parametrize("mode", ["stateless", "subscribed"])
@pytest.mark.parametrize("first_read", ["snapshot", "observe", "open"])
def test_same_source_rebinding_invalidates_old_cursor(composed, mode, first_read):
    app, launcher, *_ = composed
    app.agents.create("terminal")
    actor = "agent:owner:node:terminal"
    domains = app._agent_session_domains
    domains.call_session(RegisterSessionCommand(
        "register-binding-epoch", actor, "/tmp", ("claude",), "claude-channel",
        "external-session", channel_confirmed=True,
        channel_protocol_version=CHANNEL_PROTOCOL_VERSION,
        channel_lease_token="test-binding-lease-01234567890123456789",
    ), SessionMutationCompleted)
    cells = domains.cells
    before = cells.snapshot(actor)
    opened = cells.open(actor)
    session = domains.session.read_session(actor)
    routes = app._routes.owners(actor)
    assert cells.snapshot(actor) == before
    assert cells.observe(actor, epoch=before.lifecycle.epoch,
                         after_revision=before.lifecycle.revision).resync_required is False

    # Keep the exact Session owner/source. Do NOT observe the released interval.
    domains.liveness.release(actor)
    binding = domains.liveness.bind(actor, harness="claude", runtime="interactive",
                                    session_id="external-session")
    assert binding.revision != before.binding_revision
    bound_projection = domains.agent.read_binding(actor)
    kwargs = {"epoch": before.lifecycle.epoch, "after_revision": before.lifecycle.revision}
    if mode == "subscribed":
        kwargs["observer_id"] = opened.observer_id
    if first_read == "snapshot":
        cells.snapshot(actor)
    elif first_read == "open":
        fresh = cells.open(actor)
        assert fresh.status == "ok"
        cells.cancel(fresh.observer_id)
    batch = cells.observe(actor, **kwargs)
    if mode == "stateless":
        assert batch.resync_required, batch
    else:
        assert batch.status == "closed" or batch.resync_required, batch

    after = cells.snapshot(actor)
    assert after.target == before.target
    assert after.diagnostic_incarnation == before.diagnostic_incarnation
    assert after.binding_revision == binding.revision
    assert after.lifecycle.epoch != before.lifecycle.epoch
    assert after.custody == before.custody == "external"
    assert cells.snapshot(actor) == after  # No epoch churn on unchanged reads.
    current = cells.open(actor)
    polled = cells.observe(actor, observer_id=current.observer_id,
                           epoch=current.initial.cursor.epoch,
                           after_revision=current.initial.cursor.revision)
    assert polled.status == "ok" and not polled.resync_required
    cells.cancel(current.observer_id)
    assert domains.session.read_session(actor) == session
    domains.session.assert_owner(actor, "external-session")
    assert domains.agent.read_binding(actor) == bound_projection
    assert app._routes.owners(actor) == routes
    assert launcher.starts == 0
