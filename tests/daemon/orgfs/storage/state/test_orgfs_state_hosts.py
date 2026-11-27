# Decisions (phase D, package T3, 2026-11-25):
# - The state hosts (``LocalOrgFs``, ``OrgFsRuntime``) are built from their
#   own mixins placed in front of retained legacy mixins (design §9, plan §3.3),
#   so a method name may appear twice among their bases; the rule for them is
#   that each group is free of duplicates and the host resolves every shared
#   name to its own mixin.
# - Package S (2026-11-25): the journal store, mesh and content purge are
#   deleted, and with them their alias, construction and purge-phase tests.
"""Responsibility extraction keeps one constructor and durable state host."""

import inspect

from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs

_STATE_MODULES = (
    "hyprial.daemon.impl.orgfs.document.coordinator",
    "hyprial.daemon.impl.orgfs.composition.state",
)


def _responsibility_methods(responsibility: type) -> list[str]:
    return [
        name for name, value in responsibility.__dict__.items()
        if callable(value) or isinstance(value, (property, staticmethod))
    ]


def test_state_hosts_own_construction_and_their_mixins_win_over_the_retained_ones():
    for host in (LocalOrgFs, OrgFsRuntime):
        assert "__init__" in host.__dict__
        own: dict[str, type] = {}
        retained: dict[str, type] = {}
        for responsibility in host.__bases__:
            assert "__init__" not in responsibility.__dict__
            assert "__getattr__" not in responsibility.__dict__
            group = own if responsibility.__module__.startswith(_STATE_MODULES) else retained
            for name in _responsibility_methods(responsibility):
                assert name not in group, (host, name)
                group[name] = responsibility
        assert own
        for name in own.keys() & retained.keys():
            assert inspect.getattr_static(host, name) is own[name].__dict__[name], (host, name)


def test_directory_budget_has_one_configuration_owner(monkeypatch, tmp_path):
    from hyprial.daemon.impl.orgfs.composition import directory

    monkeypatch.setattr(directory, "ORGFS_ANNOUNCE_BUFFER_LIMIT", 3)
    runtime = OrgFsRuntime(tmp_path, node_id="node-local", author="user:local")
    try:
        assert runtime._directory_capacity == 4
        assert runtime._directory_mailbox_capacity() == 4
    finally:
        runtime.close()


