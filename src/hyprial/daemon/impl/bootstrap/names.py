"""Admission for the shared local agent/adapter name namespace.

Legacy collisions remain readable and restorable; these checks guard explicit
creation/start, not desired-state restore or identity recovery.
"""
from pathlib import Path

from hyprial.kernel import DaemonRequestError, ipc_errors


def collision_message(name: str) -> str:
    return (f"Local agent and adapter names must differ. {name!r} is already in use. "
            f"Use {name!r} for the agent and {name + '-feishu'!r} for the adapter.")


def adapter_names(home: Path) -> frozenset[str]:
    from .adapter_registration import _load_existing

    return frozenset(item.name for item in _load_existing(home / "channels.json").gateways)


def require_agent_name_available(application, name: str) -> None:
    names = adapter_names(application.hyprial_home)
    desired = application.desired_state.load()
    if name in names or any(s.name == name for s in desired.adapters):
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, collision_message(name))


def require_adapter_name_available(application, name: str) -> None:
    from hyprial.daemon.impl.harnesses.capabilities import DECLARED_HARNESSES

    if application.agents.get(name) is not None or any(
        s.name == name and s.harness in DECLARED_HARNESSES
        for s in application.desired_state.load().harnesses
    ):
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, collision_message(name))
