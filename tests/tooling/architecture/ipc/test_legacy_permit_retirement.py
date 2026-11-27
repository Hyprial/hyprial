"""Retiring a permit must expose missing declarations without opening fallback."""

import json
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.ipc import router
from hyprial.daemon.impl.request_catalog.migration import MIGRATED_METHODS
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.tooling.architecture.ipc.test_method_registration import BASELINE, GATE, ROOT


GROUPS = {
    'adapter': frozenset(['adapter.list', 'adapter.reload', 'adapter.start', 'adapter.status', 'adapter.stop']),
    'agent': frozenset(['agent.create', 'agent.home-census', 'agent.list', 'agent.provider.default', 'agent.provider.login', 'agent.provider.logout', 'agent.secret.grant', 'agent.secret.list', 'agent.secret.provider-write']),
    'identity': frozenset(['identity.users.add', 'identity.users.list']),
    'message': frozenset(['message.history', 'message.pending.batch', 'message.query', 'message.status']),
    'org': frozenset(['org.acl.grant', 'org.acl.ls', 'org.acl.revoke', 'org.create', 'org.delete', 'org.execute', 'org.fetch', 'org.invite', 'org.join', 'org.leave', 'org.list', 'org.network', 'org.publish', 'org.remove', 'org.repair']),
    'orgfs': frozenset(['orgfs.checkout', 'orgfs.create', 'orgfs.export', 'orgfs.import', 'orgfs.invite', 'orgfs.join', 'orgfs.ls', 'orgfs.members', 'orgfs.mkdir', 'orgfs.move', 'orgfs.read', 'orgfs.remove', 'orgfs.remove_member', 'orgfs.resolve', 'orgfs.restore', 'orgfs.space_purge', 'orgfs.spaces', 'orgfs.stat', 'orgfs.status', 'orgfs.trash', 'orgfs.watch', 'orgfs.write']),
    'routine': frozenset(['routine.list', 'routine.status']),
    'views': frozenset(['hosts', 'top.snapshot', 'workflow.list']),
}


@pytest.fixture(scope="module")
def scan():
    return GATE.collect(ROOT)


@pytest.mark.parametrize("group", GROUPS)
def test_retired_permits_refuse_missing_registry_bindings(group, scan, tmp_path, monkeypatch):
    names = GROUPS[group]
    registry = set(METHOD_REGISTRY.names)
    ledger = json.loads(BASELINE.read_text())
    assert names <= registry
    assert names <= set(scan["methods"])
    assert names <= MIGRATED_METHODS
    assert not (names & set(ledger["methods"]))

    previous = {**ledger, "methods": sorted(set(ledger["methods"]) | names)}
    assert set(previous["methods"]) - set(ledger["methods"]) == names
    old_path = tmp_path / "before.json"
    old_path.write_text(json.dumps(previous))
    monkeypatch.setattr(GATE, "collect", lambda _root: scan)
    without = registry - names
    before = GATE.check(ROOT, old_path, registry_names=without)
    after = GATE.check(ROOT, BASELINE, registry_names=without)
    assert before["status"] == "PASS", before
    assert after["status"] == "FAIL", after
    assert {(v["kind"], v.get("method")) for v in after["violations"]} == {
        ("unregistered-router-method", name) for name in names
    }


@pytest.mark.parametrize("origin", [None, "worker", "ipc"])
@pytest.mark.parametrize("method", sorted(set().union(*GROUPS.values())))
def test_missing_binding_never_reaches_legacy_handler(method, origin, monkeypatch):
    class RegistryWithout:
        def __contains__(self, candidate):
            return candidate != method and candidate in METHOD_REGISTRY

        def invoke(self, *_args, **_kwargs):
            pytest.fail("unexpected registry invocation")

    class Host:
        _restore_done = SimpleNamespace(is_set=lambda: True)
        _inbox = object()
        _harnesses = object()
        _presence = object()

        def __getattr__(self, name):
            pytest.fail(f"legacy/domain access: {name}")

    monkeypatch.setattr(router, "METHOD_REGISTRY", RegistryWithout())
    with pytest.raises(DaemonRequestError) as error:
        router._IpcDispatchMixin.handle(Host(), method, {}, _trusted_message_origin=origin)
    assert error.value.code == ipc_errors.METHOD_NOT_FOUND
