"""Exercise domain deletion independently of transport confirmation tests."""

from hyprial.daemon.impl.request_catalog.agent.entity import translated_agent_errors


def destroy_agent(app, name):
    with translated_agent_errors():
        if app.agents.get(name) is None:
            return app._cleanup_destroyed_agent_home(name)
        return app._destroy_agent(name)
