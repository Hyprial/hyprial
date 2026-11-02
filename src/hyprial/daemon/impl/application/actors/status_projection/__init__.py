"""Shared canonical actor rows for top and targets; no new liveness authority."""
from hyprial.daemon.impl.status import build_actor_status_snapshot


def actor_status_snapshot(app):
    assert app._harnesses is not None
    assert app._presence is not None
    snapshot = app._current_worker_snapshot()
    desired = (snapshot.desired if snapshot is not None and snapshot.desired is not None
               else app.desired_state.load())
    sessions = {session.actor: session for session in desired.interactive_sessions}
    if snapshot is not None:
        connectors = snapshot.statuses
        run_states = snapshot.run_states
    else:
        report = getattr(app._harnesses, "status", None)
        connectors = report() if callable(report) else ()
        run_states = app._cell_run_state_rows()

    def status_for_actor(actor, online):
        session = sessions.get(actor)
        if (session is not None and session.source == "claude-channel"
                and session.channel_lease_digest is not None
                and not app._channel_alive(session, app._clock())):
            return "offline"
        agent = app.agents.get(actor)
        if agent is not None:
            return app._registered_agent_status(agent, desired)
        return app._agent_target_status(actor, online, desired)

    return build_actor_status_snapshot(
        owner=app.owner, node_id=app.node_id, connectors=connectors,
        interactive_sessions=({"actor": session.actor, "runtime": session.runtime, "source": session.source}
                              for session in desired.interactive_sessions),
        registered_actors=(agent.uri for agent in app.agents.list()),
        presence_actors=app._presence.online_actors(), status_for_actor=status_for_actor,
        run_states=run_states,
    )
