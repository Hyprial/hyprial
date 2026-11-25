"""Retired work commands use the daemon's ordinary unknown-method response."""

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.ipc.router import _IpcDispatchMixin
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, ipc_errors


RETIRED_METHODS = (
    "work.acceptance", "work.add", "work.check", "work.done", "work.keywords",
    "work.link", "work.ls", "work.note", "work.repair", "work.resolve",
    "work.show", "work.status",
)


def test_work_protocol_is_absent_from_the_catalog():
    assert not any(name.startswith("work.") for name in METHOD_REGISTRY.names)


@pytest.mark.parametrize("method", (*RETIRED_METHODS, "work.unregistered"))
def test_retired_work_calls_are_unknown_methods(method):
    host = SimpleNamespace(
        _restore_done=SimpleNamespace(is_set=lambda: True),
        _inbox=object(), _harnesses=object(), _presence=object(),
    )
    with pytest.raises(DaemonRequestError) as error:
        _IpcDispatchMixin.handle(host, method, {})
    assert error.value.code == ipc_errors.METHOD_NOT_FOUND
