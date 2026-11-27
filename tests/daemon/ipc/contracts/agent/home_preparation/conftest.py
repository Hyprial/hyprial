"""Reuse the isolated local daemon fixture for the real IPC regression."""

from tests.daemon.ipc.contracts.agent.test_relations_specs import app as app
from tests.daemon.ipc.contracts.routine.test_routine_plan import (
    plan_socket as agent_socket,
)

__all__ = ["app", "agent_socket"]
