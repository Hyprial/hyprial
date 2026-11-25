"""A missing migrated binding cannot revive a legacy router branch."""

import threading

import pytest

from hyprial.daemon.impl.ipc import router
from hyprial.daemon.impl.ipc.router import _IpcDispatchMixin
from hyprial.kernel import DaemonRequestError, ipc_errors


class _WithoutOne:
    def __init__(self, registry, missing):
        self.registry = registry
        self.missing = missing

    def __contains__(self, name):
        return name != self.missing and name in self.registry

    def invoke(self, *args, **kwargs):
        return self.registry.invoke(*args, **kwargs)


class _Host(_IpcDispatchMixin):
    def __init__(self):
        self._restore_done = threading.Event()
        self._restore_done.set()
        self._inbox = self._harnesses = self._presence = object()

    def __getattr__(self, name):
        raise AssertionError(f"legacy handler unexpectedly reached: {name}")


@pytest.mark.parametrize("method", ["agent.cell.get", "agent.cell.update", "message.send", "progress.list", "targets", "autoupdate.status", "upgrade.steps.status", "upgrade.steps.run", "agent.resolve"])
def test_missing_one_migrated_binding_never_reaches_a_legacy_handler(monkeypatch, method):
    registry = router.METHOD_REGISTRY
    assert method in registry
    assert method in router.MIGRATED_METHODS
    monkeypatch.setattr(router, "METHOD_REGISTRY", _WithoutOne(registry, method))
    with pytest.raises(DaemonRequestError) as caught:
        _Host().handle(method, {}, _trusted_message_origin="worker")
    assert caught.value.code == ipc_errors.METHOD_NOT_FOUND


def test_upgrade_run_reaches_registered_handler_with_membership_migration(tmp_path, monkeypatch):
    from hyprial.daemon.impl.org.membership.migration import runtime as migration
    from hyprial.daemon.impl.request_catalog.upgrade import methods

    host = _Host()
    host.state_dir = tmp_path / "state"
    host.hyprial_home = tmp_path / "hyprial-home"
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    seen = []
    monkeypatch.setattr(migration, "run", lambda app: seen.append(("migration", app)) or {"ok": True})

    def run_steps(state, **kwargs):
        seen.append(("runner", state, kwargs["hyprial_home"], kwargs["home"]))
        assert kwargs["membership_migration"]() == {"ok": True}
        return {"steps": [], "pending": []}

    monkeypatch.setattr(methods, "run_upgrade_steps", run_steps)
    assert host.handle("upgrade.steps.run", {"apply": True}) == {"steps": [], "pending": []}
    assert seen == [("runner", host.state_dir, host.hyprial_home, tmp_path / "user-home"), ("migration", host)]
