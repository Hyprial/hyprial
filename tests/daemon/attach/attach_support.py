"""Shared calls for the ``agent.attach`` / ``agent.detach`` tests.

Every call goes through ``METHOD_REGISTRY.invoke``: the declared params and
result schemas and the declared error codes are enforced exactly as the
daemon's request dispatcher enforces them.  The golden daemon runs with
``HYPRIAL_SENDER_AUTH_MODE=enforce``; an external harness reads through the
operator form (``from=user:<owner>`` with ``onBehalfOf``) here, which is the
authenticated local path an attached harness has without a session.
"""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.daemon.impl.cells.runtime.agents import CUSTODY_PARAM
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError
from tests.support.golden.app import OWNER_USER

__all__ = [
    "CUSTODY_PARAM",
    "attach",
    "detach",
    "operator",
    "refused",
]


def attach(app: Any, name: str, **extra: Any) -> dict[str, Any]:
    return METHOD_REGISTRY.invoke(app, "agent.attach", {"name": name, **extra})


def detach(app: Any, name: str, **extra: Any) -> dict[str, Any]:
    return METHOD_REGISTRY.invoke(app, "agent.detach", {"name": name, **extra})


def operator(actor: str) -> dict[str, str]:
    """The local operator acting for one attached agent."""

    return {"from": OWNER_USER, "onBehalfOf": actor}


def refused(method: str, app: Any, params: dict[str, Any]) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(app, method, params)
    return raised.value
